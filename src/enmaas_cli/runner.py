"""Bulk fan-out orchestration for `enmaas map`."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .auth import redact
from .batching import build_batch_prompt, split_batch_response, split_template
from .budget import TokenBudget, charge_from_rows, metered
from .client import GatewayError, call
from .dialect import (
    Dialect,
    EmptyResponseError,
    build_request,
    default_max_tokens,
    parse_response,
    resolve_dialect,
)
from .outputs import protect
from .schema import ValidationError, load_schema, validate_against_schema

# ValidationError moved to .schema; re-exported so existing importers keep working.
__all__ = ["ValidationError", "load_schema", "run_map", "validate_against_schema"]


class CircuitBreaker:
    """Tracks the outcomes of recently completed items.

    Isolated failures should not kill a healthy run, and a systemic failure
    should not burn through hundreds of doomed requests.

    Two independent signals, because they detect different things. The rate
    window answers "is this run unhealthy on average". The consecutive count
    answers "is this thing dead right now". Systemic breakage, such as a
    revoked key or a gateway outage, produces an unbroken run of failures,
    and waiting for a full window to fill up before noticing is wasteful
    when each failure can take 300 seconds.

    Args:
        window: How many recently completed items to consider for the rate.
        threshold: Failure fraction above which the run should abort.
        consecutive: How many back-to-back failures mean total breakage.
    """

    def __init__(self, window: int = 20, threshold: float = 0.5, consecutive: int = 6) -> None:
        self.window = window
        self.threshold = threshold
        self.consecutive = consecutive
        self._recent: deque[bool] = deque(maxlen=window)

    def record(self, *, ok: bool) -> None:
        """Record one completed item. True means it succeeded."""
        self._recent.append(ok)

    def should_abort(self) -> bool:
        """Return True when the run should stop scheduling new items.

        Either signal is sufficient.

        The consecutive check runs first and returns early, so a healthy run
        never pays for the window sum. It needs no separate counter: because
        ``consecutive`` is smaller than ``window``, the tail is always still
        in ``_recent``.

        The rate check waits for a full window, so a five-item run cannot
        abort on rate alone, and uses a strict comparison, so an even 50/50
        run keeps going.
        """
        recent = list(self._recent)

        if (
            self.consecutive > 0
            and len(recent) >= self.consecutive
            and not any(recent[-self.consecutive :])
        ):
            return True

        if len(recent) < self.window:
            return False

        failures = sum(1 for ok in recent if not ok)
        return failures / self.window > self.threshold


def load_items(path: Path) -> list[dict[str, Any]]:
    """Read items.jsonl, rejecting problems up front rather than mid-run."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{path} line {number}: not valid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValidationError(f"{path} line {number}: expected a JSON object")
        if "id" not in row:
            raise ValidationError(f"{path} line {number}: missing required 'id' field")
        item_id = str(row["id"])
        if item_id in seen:
            raise ValidationError(f"{path} line {number}: duplicate id {item_id!r}")
        if item_id in {"", ".", ".."} or Path(item_id).name != item_id:
            raise ValidationError(f"{path} line {number}: id {item_id!r} is not a safe filename")
        seen.add(item_id)
        items.append(row)
    return items


def render(template: str, item: dict[str, Any]) -> str:
    """Substitute item fields into the template."""
    try:
        return template.format(**item)
    except KeyError as exc:
        field_name = exc.args[0]
        raise ValidationError(
            f"item {item.get('id')!r} has no field {field_name!r} required by the template"
        ) from exc


def validate_all(template: str, items: list[dict[str, Any]]) -> None:
    """Render every item before any request, so a bad template fails once."""
    for item in items:
        render(template, item)


def _charge_row(charge: int, *, items: int) -> str:
    """One JSONL row recording what a single request cost.

    Written beside the per-item rows because the budget cannot be rebuilt
    from those: single-item rows omit the cached count, and batch rows carry
    `"tokens": None` outright. This row is the budget's source of truth; the
    per-item `tokens` object stays a human-readable record.

    It has no `id` and no `status`, so `resume_skips` ignores it.

    Known undercount: `call` retries internally and only the attempt that
    returned is visible here. An attempt that generated tokens and then
    failed in transport is not charged, because a non-200 carries no usage
    body for us to read. That spend is unknowable from the client side
    rather than merely unimplemented.
    """
    return json.dumps({"type": "request", "charge": charge, "items": items})


def spent_tokens(outdir: Path) -> int:
    """Metered tokens already charged to this job, read from results.jsonl.

    Seeds a resumed run's budget. Without it a ceiling could be bypassed by
    rerunning the command, which is the same false assurance as a budget
    flag that silently does nothing.

    Forgiving about damaged rows: this reads an artefact the tool wrote
    itself, not user-supplied state. A row that cannot be parsed is a row
    whose charge was not recorded.
    """
    index = outdir / "results.jsonl"
    if not index.exists():
        return 0
    rows: list[dict[str, Any]] = []
    for line in index.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return charge_from_rows(rows)


def resume_skips(outdir: Path) -> set[str]:
    """IDs recorded ok, having an output file, and not repaired.

    A repaired item succeeded, so its output stays on disk and nothing
    is lost. But it is deliberately *not* skipped: the saved text
    differs from what the model sent, and treating it as finished would
    mean a rerun could never obtain a clean version of it. Offering the
    retry costs a request, which --token-budget exists to bound.
    """
    index = outdir / "results.jsonl"
    if not index.exists():
        return set()
    skips: set[str] = set()
    for line in index.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if row.get("replaced") or row.get("replaced_in_request"):
            continue
        item_id = str(row.get("id", ""))
        if row.get("status") == "ok" and (outdir / f"{item_id}.md").exists():
            skips.add(item_id)
    return skips


@dataclass
class MapOutcome:
    ok: int
    failed: int
    skipped: int
    attempted: int
    total: int
    # Not a bool. A second way to abort arrived with token budgets, and a
    # receipt saying only "ABORTED" sends people hunting for failures that
    # never happened. One of None, "breaker", "budget".
    abort_reason: str | None
    input_tokens: int
    output_tokens: int
    # Carried because caching is the largest cost lever on bulk work, and it
    # cannot be tuned while the command that does the bulk work throws the
    # measurement away.
    cached_tokens: int
    reasoning_tokens: int
    elapsed: float
    outdir: Path
    failures: list[tuple[str, str]]
    # The per-run budget's running total, job-wide when resumed. Not
    # derivable from input_tokens/cached_tokens above: clamping after
    # aggregation gives a different answer than summing per-request
    # clamps, and those counters exclude requests that failed after
    # consuming tokens. Defaulted so existing constructions still work.
    spent: int = 0
    # Which ceiling stopped the run: "per_run", "rolling" or None.
    # abort_reason only says "budget", which would make a rolling stop
    # look like the per-run flag and send the user to raise the wrong
    # number.
    tripped_budget: str | None = None
    # Exact tokens this invocation's requests cost, failures
    # included. What the ledger records, so the rolling window and
    # the charge rows cannot disagree.
    charged: int = 0
    # The rolling accumulator's total: window spend already recorded
    # plus this invocation's charges. Reporting
    # rolling.spent + outcome.spent instead double-counts a resumed
    # job, because outcome.spent already carries the prior runs.
    rolling_spent: int | None = None

    @property
    def exit_code(self) -> int:
        if self.abort_reason is not None:
            return 3
        if self.failed:
            return 2
        return 0


async def run_map(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    model: str,
    items_path: Path,
    template_path: Path,
    outdir: Path,
    concurrency: int,
    retries: int,
    timeout: float,
    thinking: bool,
    max_tokens: int | None,
    dialect: Dialect | None,
    resume: bool,
    schema_file: Path | None = None,
    schema_name: str | None = None,
    batch: int = 1,
    token_budget: int | None = None,
    rolling_budget: TokenBudget | None = None,
) -> MapOutcome:
    items = load_items(items_path)
    template = template_path.read_text(encoding="utf-8")
    validate_all(template, items)
    # Split once, up front. A template that cannot be batched should fail
    # before any request, not after the first group has been paid for.
    preamble, per_item = split_template(template) if batch > 1 else ("", "")

    schema = load_schema(schema_file) if schema_file is not None else None

    outdir.mkdir(parents=True, exist_ok=True)
    skips = resume_skips(outdir) if resume else set()
    pending = [i for i in items if str(i["id"]) not in skips]
    for item in pending:
        protect(outdir / f"{item['id']}.md")

    resolved = resolve_dialect(model, dialect)
    tokens = max_tokens or default_max_tokens(thinking=thinking)
    breaker = CircuitBreaker()
    semaphore = asyncio.Semaphore(concurrency)
    index = outdir / "results.jsonl"
    lock = asyncio.Lock()
    abort_reason: str | None = None
    # Seeded from the charge rows of earlier invocations, so the ceiling
    # belongs to the job rather than to one command. Resetting it per run
    # would make the cap bypassable by pressing up-arrow and Enter.
    # Read once and reuse: it also feeds the reported total below, so a
    # resumed run without a ceiling still tells the truth about the job.
    prior_spend = spent_tokens(outdir) if resume else 0
    budget = TokenBudget(token_budget, spent=prior_spend) if token_budget else None
    # Both ceilings, checked before anything is sent. Omitting the rolling
    # one here would let an already-exhausted budget still pay for one
    # request per concurrency slot before noticing.
    tripped_budget: str | None = None
    for name, limit in (("per_run", budget), ("rolling", rolling_budget)):
        if limit is not None and limit.should_abort():
            abort_reason = "budget"
            tripped_budget = name
            break
    started = time.monotonic()
    # "charge" is the budget's running total, counted uniformly: every
    # request that consumed tokens, including ones that then failed.
    #
    # in/out/cached/reasoning are not that, and are not consistent either.
    # The batched path adds them before per-item validation while the
    # single-item path adds them after, so a schema-rejected response is
    # counted by one and not the other. That predates budgets and is left
    # alone here; it is the reason the budget keeps its own total rather
    # than deriving one from these.
    counts = {"ok": 0, "failed": 0, "attempted": 0, "in": 0, "out": 0,
              "cached": 0, "reasoning": 0, "charge": 0}
    failures: list[tuple[str, str]] = []

    async def process(item: dict[str, Any]) -> None:
        nonlocal abort_reason, tripped_budget
        item_id = str(item["id"])
        async with semaphore:
            if abort_reason is not None:
                return
            counts["attempted"] += 1
            request = build_request(
                model=model,
                prompt=render(template, item),
                dialect=resolved,
                max_tokens=tokens,
                thinking=thinking,
                system=None,
                key=key,
                schema=schema,
                schema_name=schema_name,
            )
            row: dict[str, Any]
            # Charged tokens, tracked separately from `counts` because this
            # must include requests that cost money and then failed.
            charge = 0
            try:
                data = await call(client, base_url, request, timeout=timeout, retries=retries)
                completion = parse_response(data, resolved)
                # Charge before validation and the file write. A large
                # response a validator rejects still cost its tokens, and
                # the batch path below already accounts at this point.
                charge = metered(completion.usage)
                if schema is not None:
                    validate_against_schema(completion.text, schema)
                path = outdir / f"{item_id}.md"
                path.write_text(completion.text, encoding="utf-8")
                counts["in"] += completion.usage.input_tokens or 0
                counts["out"] += completion.usage.output_tokens or 0
                counts["cached"] += completion.usage.cached_tokens or 0
                counts["reasoning"] += completion.usage.reasoning_tokens or 0
                row = {
                    "id": item_id,
                    "status": "ok",
                    "path": str(path),
                    "tokens": {
                        "in": completion.usage.input_tokens,
                        "out": completion.usage.output_tokens,
                    },
                    # Only present when the response had to be altered to
                    # be writable. A bulk run prints no per-item receipt,
                    # so without this the repair would leave no trace.
                    **({"replaced": completion.replaced} if completion.replaced else {}),
                    "error": None,
                }
                counts["ok"] += 1
                ok = True
            except (GatewayError, EmptyResponseError, ValidationError, OSError) as exc:
                if isinstance(exc, EmptyResponseError):
                    # Reasoning ate the budget and returned nothing. The
                    # gateway charged for it regardless.
                    charge = metered(exc.usage)
                message = redact(str(exc), key)
                row = {
                    "id": item_id,
                    "status": "error",
                    "path": None,
                    "tokens": None,
                    "error": message,
                }
                failures.append((item_id, message))
                counts["failed"] += 1
                ok = False

            async with lock:
                with index.open("a", encoding="utf-8") as handle:
                    # Charge first. If the process dies mid-write, the
                    # asymmetry matters: an orphaned charge row over-counts,
                    # which is safe, while an item row whose charge never
                    # landed lets a resumed run spend that budget twice.
                    handle.write(_charge_row(charge, items=1) + "\n")
                    handle.write(json.dumps(row) + "\n")
                counts["charge"] += charge
                breaker.record(ok=ok)
                if breaker.should_abort() and abort_reason is None:
                    abort_reason = "breaker"
                # Both budgets, one block, written identically in both
                # execution paths so they cannot drift. Either is
                # sufficient to stop the run, the way the breaker carries
                # two independent signals.
                #
                # The `abort_reason is None` guard makes first cause win.
                # Without it a run that tripped the breaker and then went
                # over budget would report "budget", hiding the more
                # actionable failure. This is not a data race: asyncio
                # coroutines do not interleave between these statements,
                # and the lock is already held. It is overwrite policy.
                for name, limit in (("per_run", budget), ("rolling", rolling_budget)):
                    if limit is not None:
                        limit.record(charge)
                        if limit.should_abort() and abort_reason is None:
                            abort_reason = "budget"
                            tripped_budget = name

    async def process_batch(group: list[dict[str, Any]]) -> None:
        """One call covering several items, answers matched back by id."""
        nonlocal abort_reason, tripped_budget
        ids = [str(i["id"]) for i in group]
        async with semaphore:
            if abort_reason is not None:
                return
            counts["attempted"] += len(group)
            request = build_request(
                model=model,
                prompt=build_batch_prompt(preamble, per_item, group),
                dialect=resolved,
                max_tokens=tokens * len(group),
                thinking=thinking,
                system=None,
                key=key,
                schema=None,
                schema_name=None,
            )
            rows: list[dict[str, Any]] = []
            charge = 0
            try:
                data = await call(
                    client, base_url, request, timeout=timeout, retries=retries
                )
                completion = parse_response(data, resolved)
                # Before split_batch_response, so a reply this group cannot
                # be split out of is still charged.
                charge = metered(completion.usage)
                answers = split_batch_response(completion.text, ids)
                counts["in"] += completion.usage.input_tokens or 0
                counts["out"] += completion.usage.output_tokens or 0
                counts["cached"] += completion.usage.cached_tokens or 0
                counts["reasoning"] += completion.usage.reasoning_tokens or 0
            except (GatewayError, EmptyResponseError, ValidationError, OSError) as exc:
                # The call failed, so every item in it failed. Attributing
                # the error to each one keeps the breaker counting items.
                if isinstance(exc, EmptyResponseError):
                    charge = metered(exc.usage)
                message = redact(str(exc), key)
                answers = {}
                for item_id in ids:
                    rows.append({"id": item_id, "status": "error", "path": None,
                                 "tokens": None, "error": message})
                    failures.append((item_id, message))
                    counts["failed"] += 1

            for item_id in ids:
                if any(r["id"] == item_id for r in rows):
                    continue
                text = answers.get(item_id)
                if text is None:
                    # Not ok. A missing output file that nothing flags would
                    # be skipped forever by --resume.
                    message = f"no answer for {item_id} in the batched reply"
                    rows.append({"id": item_id, "status": "error", "path": None,
                                 "tokens": None, "error": message})
                    failures.append((item_id, message))
                    counts["failed"] += 1
                    continue
                try:
                    if schema is not None:
                        validate_against_schema(text, schema)
                    path = outdir / f"{item_id}.md"
                    path.write_text(text, encoding="utf-8")
                except (ValidationError, OSError) as exc:
                    message = redact(str(exc), key)
                    rows.append({"id": item_id, "status": "error", "path": None,
                                 "tokens": None, "error": message})
                    failures.append((item_id, message))
                    counts["failed"] += 1
                    continue
                rows.append({"id": item_id, "status": "ok", "path": str(path),
                             "tokens": None, "batch_size": len(group),
                             # Request-level, not item-level: one reply
                             # covered the whole group and was repaired
                             # before it could be split, so the count
                             # cannot be attributed to a single item.
                             **({"replaced_in_request": completion.replaced}
                                if completion.replaced else {}),
                             "error": None})
                counts["ok"] += 1

            async with lock:
                with index.open("a", encoding="utf-8") as handle:
                    # One charge row per request, not per item: the group
                    # was one call. Per-item rows carry "tokens": None here,
                    # which is why the budget cannot be rebuilt from them.
                    # Written first, so a crash over-counts rather than
                    # letting a resumed run respend the group's budget.
                    handle.write(_charge_row(charge, items=len(group)) + "\n")
                    for row in rows:
                        handle.write(json.dumps(row) + "\n")
                counts["charge"] += charge
                for row in rows:
                    breaker.record(ok=row["status"] == "ok")
                if breaker.should_abort() and abort_reason is None:
                    abort_reason = "breaker"
                # Both budgets, one block, written identically in both
                # execution paths so they cannot drift. Either is
                # sufficient to stop the run, the way the breaker carries
                # two independent signals.
                #
                # The `abort_reason is None` guard makes first cause win.
                # Without it a run that tripped the breaker and then went
                # over budget would report "budget", hiding the more
                # actionable failure. This is not a data race: asyncio
                # coroutines do not interleave between these statements,
                # and the lock is already held. It is overwrite policy.
                for name, limit in (("per_run", budget), ("rolling", rolling_budget)):
                    if limit is not None:
                        limit.record(charge)
                        if limit.should_abort() and abort_reason is None:
                            abort_reason = "budget"
                            tripped_budget = name

    if batch > 1:
        groups = [pending[n:n + batch] for n in range(0, len(pending), batch)]
        await asyncio.gather(*(process_batch(g) for g in groups))
    else:
        await asyncio.gather(*(process(item) for item in pending))

    return MapOutcome(
        ok=counts["ok"],
        failed=counts["failed"],
        skipped=len(items) - len(pending),
        attempted=counts["attempted"],
        total=len(items),
        abort_reason=abort_reason,
        tripped_budget=tripped_budget,
        charged=counts["charge"],
        rolling_spent=rolling_budget.spent if rolling_budget is not None else None,
        # Job-wide either way. Without a ceiling there is no accumulator to
        # read, so add this invocation's charge to what earlier ones left
        # in the index rather than reporting only what this run spent.
        spent=budget.spent if budget is not None else prior_spend + counts["charge"],
        input_tokens=counts["in"],
        output_tokens=counts["out"],
        cached_tokens=counts["cached"],
        reasoning_tokens=counts["reasoning"],
        elapsed=time.monotonic() - started,
        outdir=outdir,
        failures=failures,
    )
