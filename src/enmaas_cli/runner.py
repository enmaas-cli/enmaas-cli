"""Bulk fan-out orchestration for `enmaas map`."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from .auth import redact
from .batching import PER_ITEM_MARKER, build_batch_prompt, split_batch_response, split_template
from .budget import TokenBudget, charge_from_rows, metered
from .client import CallSettings, call
from .dialect import (
    DEFAULT_MAX_TOKENS,
    Dialect,
    build_request,
    parse_response,
    resolve_dialect,
    thinking_off_payload,
)
from .errors import CallError, where
from .outputs import protect, write_output
from .schema import ValidationError, load_schema, validate_against_schema
from .tokens import MAX_RECORDED, Usage

# Why a run stopped before its last item.
AbortReason = Literal["breaker", "budget", "interrupted", "index"]
# Which ceiling stopped a run.
BudgetName = Literal["per_run", "rolling"]


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


def _internal_message(exc: BaseException) -> str:
    """Where an unexpected error happened. Never its text, which is unreviewed."""
    return f"internal error ({where(exc)})"


def _charge_row(charge: int, *, items: int, run: str | None = None) -> str:
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

    `run` names the invocation that wrote the row, when it has an open-run
    record. Two runs can share one output directory, and an offset alone
    cannot tell their rows apart.

    The charge is clamped to the range from 0 to MAX_RECORDED, which every
    reader of the index accepts.
    """
    row: dict[str, Any] = {
        "type": "request",
        "charge": max(0, min(charge, MAX_RECORDED)),
        "items": items,
    }
    if run is not None:
        row["run"] = run
    return json.dumps(row)


def _charge_row_parts(run: str) -> tuple[str, str, str]:
    """The text of this run's charge row around its two numbers.

    A row _charge_row writes for `run` is head + charge + middle + items +
    tail. Taken from the row it writes with both numbers 0, split at the
    first two zeros: no text before the two numbers holds a digit, and the
    run id comes after them. A reader builds its pattern from these parts,
    so the reader and the writer cannot drift apart.
    """
    head, rest = _charge_row(0, items=0, run=run).split("0", 1)
    middle, tail = rest.split("0", 1)
    return head, middle, tail


def _require_own_regular(fd: int, index: Path) -> None:
    """Refuse an open index that is not a regular file owned by this user.

    Checked on the descriptor, never by path. A FIFO would let another
    process hold the write, and a file someone else owns could be read or
    changed by them. A hard link is accepted: a backup tool can make one.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{index} is not a regular file; enmaas writes its index only to one")
    if info.st_uid != os.getuid():
        raise OSError(f"{index} is owned by another user; enmaas writes its index only to yours")


def _append_index(index: Path, text: str) -> None:
    """Append rows to the index, never through a symbolic link.

    A new index is mode 0o600: it names every item and what each request
    cost. An existing one keeps its mode. O_NOFOLLOW makes a link swapped in
    during the run fail the write instead of sending the rows to the link's
    target. O_NONBLOCK keeps a FIFO swapped in from blocking the open, so
    the check after it can refuse it.
    """
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(index, flags, 0o600)
    try:
        _require_own_regular(fd, index)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _check_index(index: Path) -> None:
    """Refuse an index that cannot be written safely, before anything is sent.

    A link, a path that is not a regular file and a file another user owns
    are all refused.
    """
    try:
        info = os.lstat(index)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise OSError(
            f"{index} is not a regular file; enmaas does not write its index through a link"
        )
    fd = os.open(index, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _require_own_regular(fd, index)
    finally:
        os.close(fd)


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
    # never happened. None when the run did not stop early.
    abort_reason: AbortReason | None
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
    # clamps. Defaulted so existing constructions still work.
    spent: int = 0
    # Which ceiling stopped the run: "per_run", "rolling" or None.
    # abort_reason only says "budget", which would make a rolling stop
    # look like the per-run flag and send the user to raise the wrong
    # number.
    tripped_budget: BudgetName | None = None
    # Exact tokens this invocation's requests cost, failures
    # included. What the ledger records, so the rolling window and
    # the charge rows cannot disagree.
    charged: int = 0
    # The rolling accumulator's total: window spend already recorded
    # plus this invocation's charges. Reporting
    # rolling.spent + outcome.spent instead double-counts a resumed
    # job, because outcome.spent already carries the prior runs.
    rolling_spent: int | None = None
    # The group size the run used, after defaulting. Carried so the
    # receipt can say what was chosen when the caller did not choose.
    batch: int = 1
    # Items failed by an error no handler expected. Already counted in
    # `failed`; kept apart so the user can see it is a fault to report.
    internal_errors: int = 0
    # Why the index could not be written, when abort_reason is "index".
    index_error: str | None = None

    @property
    def exit_code(self) -> int:
        # The run could not record its work, which is an error, not a stop.
        if self.abort_reason == "index":
            return 1
        if self.abort_reason is not None:
            return 3
        if self.failed:
            return 2
        return 0


@dataclass(frozen=True)
class MapJob:
    """What one `map` invocation was asked to do."""

    model: str
    items_path: Path
    template_path: Path
    outdir: Path
    concurrency: int
    thinking: bool
    max_tokens: int | None
    dialect: Dialect | None
    resume: bool
    schema_file: Path | None = None
    schema_name: str | None = None
    batch: int | None = None
    token_budget: int | None = None
    rolling_budget: TokenBudget | None = None
    run_id: str | None = None


@dataclass
class Tally:
    """The counters of one invocation.

    `charged` is the budget's running total: the sum of each request's
    metered charge, failures included.

    The four token counters add up every usage block the gateway returned,
    whether or not the answer was usable, because every such reply was
    billed. The budget still keeps its own total: a charge is clamped per
    request, and clamping the sums gives a different number.
    """

    ok: int = 0
    failed: int = 0
    attempted: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    charged: int = 0
    internal: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def add(self, result: SendResult) -> None:
        """Count one request's rows and the usage its reply reported."""
        if result.usage is not None:
            self.input_tokens += result.usage.input_tokens or 0
            self.output_tokens += result.usage.output_tokens or 0
            self.cached_tokens += result.usage.cached_tokens or 0
            self.reasoning_tokens += result.usage.reasoning_tokens or 0
        for row in result.rows:
            if row["status"] == "ok":
                self.ok += 1
            else:
                self.failed += 1
                self.failures.append((row["id"], row["error"]))
        self.internal += result.internal


@dataclass(frozen=True)
class SendResult:
    """What one request produced, ready to be recorded.

    `rows` holds one index row per item, in item order. `usage` is the usage
    the reply reported, or None when no reply arrived. `charge` is the
    metered cost of the request, failures included.
    `internal` counts the rows failed by an error no handler expected.
    """

    rows: list[dict[str, Any]]
    usage: Usage | None
    charge: int
    internal: int = 0


@dataclass(frozen=True)
class _Plan:
    """A job after its inputs are read and checked, before any request."""

    job: MapJob
    items: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    template: str
    batch: int
    preamble: str
    per_item: str
    schema: dict[str, Any] | None
    dialect: Dialect
    max_tokens: int


@dataclass
class _RunState:
    """Everything `record` reads and changes while a run is in progress."""

    index: Path
    run_id: str | None
    key: str
    budget: TokenBudget | None
    rolling_budget: TokenBudget | None
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    tally: Tally = field(default_factory=Tally)
    # The first cause wins, apart from "index" and "interrupted", which end
    # the run however it was stopping.
    abort_reason: AbortReason | None = None
    tripped_budget: BudgetName | None = None
    index_error: str | None = None


def _error_row(item_id: str, message: str) -> dict[str, Any]:
    return {"id": item_id, "status": "error", "path": None, "tokens": None, "error": message}


def _failed(
    ids: list[str], message: str, usage: Usage | None, *, internal: bool = False
) -> SendResult:
    """A request that failed as a whole. Every item in it fails with its message.

    Attributing the error to each item keeps the breaker counting items. A
    reply that arrived was billed, so its usage is still counted and
    charged.
    """
    return SendResult(
        rows=[_error_row(item_id, message) for item_id in ids],
        usage=usage,
        charge=metered(usage) if usage is not None else 0,
        internal=len(ids) if internal else 0,
    )


async def send_single(
    client: httpx.AsyncClient, settings: CallSettings, plan: _Plan, item: dict[str, Any]
) -> SendResult:
    """Send one item in its own request and write its answer."""
    item_id = str(item["id"])
    request = build_request(
        model=plan.job.model,
        prompt=render(plan.template, item),
        dialect=plan.dialect,
        max_tokens=plan.max_tokens,
        thinking=plan.job.thinking,
        system=None,
        key=settings.key,
        schema=plan.schema,
        schema_name=plan.job.schema_name,
    )
    usage: Usage | None = None
    try:
        data = await call(
            client,
            settings.base_url,
            request,
            timeout=settings.timeout,
            retries=settings.retries,
            secret=settings.key,
        )
        completion = parse_response(data, plan.dialect)
        # Kept before validation and the file write. A large answer a
        # validator rejects still cost its tokens.
        usage = completion.usage
        if plan.schema is not None:
            validate_against_schema(completion.text, plan.schema)
        path = plan.job.outdir / f"{item_id}.md"
        write_output(path, completion.text)
    except (CallError, ValidationError, OSError) as exc:
        if isinstance(exc, CallError):
            # A failed request can still have been billed, for example
            # reasoning that ate the budget and returned nothing. Count
            # what the reply reported.
            usage = exc.usage
        return _failed([item_id], redact(str(exc), settings.key), usage)
    except Exception as exc:
        # One unexpected error fails this item only, so the rest of the run
        # and its charge rows survive. `usage` keeps what it held when the
        # error was raised.
        return _failed([item_id], _internal_message(exc), usage, internal=True)
    row = {
        "id": item_id,
        "status": "ok",
        "path": str(path),
        "tokens": {
            "in": completion.usage.input_tokens,
            "out": completion.usage.output_tokens,
        },
        # Only present when the answer had to be altered to be writable. A
        # bulk run prints no per-item receipt, so without this the repair
        # would leave no trace.
        **({"replaced": completion.replaced} if completion.replaced else {}),
        "error": None,
    }
    return SendResult(rows=[row], usage=usage, charge=metered(completion.usage))


def _batch_item(
    plan: _Plan, item_id: str, text: str | None, *, size: int, replaced: int, key: str
) -> tuple[dict[str, Any], bool]:
    """Check and write one answer of a batched reply.

    Returns the item's row and whether it failed on an unexpected error.
    """
    if text is None:
        # Not ok. A missing output file that nothing flags would be skipped
        # forever by --resume.
        return _error_row(item_id, f"no answer for {item_id} in the batched reply"), False
    try:
        if plan.schema is not None:
            validate_against_schema(text, plan.schema)
        path = plan.job.outdir / f"{item_id}.md"
        write_output(path, text)
    except (ValidationError, OSError) as exc:
        return _error_row(item_id, redact(str(exc), key)), False
    except Exception as exc:
        return _error_row(item_id, _internal_message(exc)), True
    row = {
        "id": item_id,
        "status": "ok",
        "path": str(path),
        "tokens": None,
        "batch_size": size,
        # Request-level, not item-level: one reply covered the whole group
        # and was repaired before it could be split, so the count cannot be
        # attributed to a single item.
        **({"replaced_in_request": replaced} if replaced else {}),
        "error": None,
    }
    return row, False


async def send_batch(
    client: httpx.AsyncClient, settings: CallSettings, plan: _Plan, group: list[dict[str, Any]]
) -> SendResult:
    """Send several items in one request. Answers are matched back by id."""
    ids = [str(i["id"]) for i in group]
    request = build_request(
        model=plan.job.model,
        prompt=build_batch_prompt(plan.preamble, plan.per_item, group),
        dialect=plan.dialect,
        max_tokens=plan.max_tokens * len(group),
        thinking=plan.job.thinking,
        system=None,
        key=settings.key,
        schema=None,
        schema_name=None,
    )
    usage: Usage | None = None
    try:
        data = await call(
            client,
            settings.base_url,
            request,
            timeout=settings.timeout,
            retries=settings.retries,
            secret=settings.key,
        )
        completion = parse_response(data, plan.dialect)
        # Before split_batch_response, so a reply this group cannot be split
        # out of is still counted and charged.
        usage = completion.usage
        answers = split_batch_response(completion.text, ids)
    except (CallError, ValidationError, OSError) as exc:
        if isinstance(exc, CallError):
            usage = exc.usage
        return _failed(ids, redact(str(exc), settings.key), usage)
    except Exception as exc:
        # An unexpected error fails this group only. `usage` keeps what it
        # held when the error was raised.
        return _failed(ids, _internal_message(exc), usage, internal=True)
    rows: list[dict[str, Any]] = []
    internal = 0
    for item_id in ids:
        row, unexpected = _batch_item(
            plan,
            item_id,
            answers.get(item_id),
            size=len(group),
            replaced=completion.replaced,
            key=settings.key,
        )
        rows.append(row)
        internal += int(unexpected)
    return SendResult(rows=rows, usage=usage, charge=metered(completion.usage), internal=internal)


def _index_failed(run: _RunState, exc: OSError, charge: int) -> None:
    """End the run in order after the index could not be written.

    The request was billed, so its charge counts toward the ledger event
    and both budgets. Requests still waiting for the semaphore see the
    abort reason and return without sending, because a request that cannot
    be recorded must not be sent.
    """
    run.abort_reason = "index"
    run.tripped_budget = None
    if run.index_error is None:
        run.index_error = redact(str(exc), run.key)
    for limit in (run.budget, run.rolling_budget):
        if limit is not None:
            limit.record(charge)


def _charge_budgets(run: _RunState, charge: int) -> None:
    """Add a charge to both budgets. Either one is sufficient to stop the run.

    The `abort_reason is None` guard makes the first cause win. Without it
    a run that tripped the breaker and then went over budget would report
    "budget", hiding the more actionable failure. Nothing here awaits, so
    no other request can run between these statements.
    """
    limits: tuple[tuple[BudgetName, TokenBudget | None], ...] = (
        ("per_run", run.budget),
        ("rolling", run.rolling_budget),
    )
    for name, limit in limits:
        if limit is not None:
            limit.record(charge)
            if limit.should_abort() and run.abort_reason is None:
                run.abort_reason = "budget"
                run.tripped_budget = name


def record(run: _RunState, result: SendResult) -> None:
    """Record one request: index rows, counters, breaker, budgets, the stop reason.

    Synchronous on purpose. Nothing awaits between the reply arriving and
    these rows being written, so a cancel cannot separate an answer file
    from its rows. An await added here would break that.
    """
    run.tally.add(result)
    run.tally.charged += result.charge
    try:
        # One append per request, charge row first. If the process dies
        # mid-write, an orphaned charge row over-counts, which is safe, while
        # an item row whose charge never landed lets a resumed run spend that
        # budget twice. One charge row per request, not per item: a group is
        # one call, and batched item rows carry "tokens": None.
        _append_index(
            run.index,
            _charge_row(result.charge, items=len(result.rows), run=run.run_id)
            + "\n"
            + "".join(json.dumps(row) + "\n" for row in result.rows),
        )
    except OSError as exc:
        _index_failed(run, exc, result.charge)
        return
    for row in result.rows:
        run.breaker.record(ok=row["status"] == "ok")
    if run.breaker.should_abort() and run.abort_reason is None:
        run.abort_reason = "breaker"
    _charge_budgets(run, result.charge)


def _pending(job: MapJob, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items still to send, each answer file marked protected first."""
    skips = resume_skips(job.outdir) if job.resume else set()
    pending = [i for i in items if str(i["id"]) not in skips]
    for item in pending:
        target = job.outdir / f"{item['id']}.md"
        # A link here would stop the whole run before any request. Leave it
        # unprotected; write_output refuses it later and only that item fails.
        if not target.is_symlink():
            protect(target)
    return pending


def _warn_thinking_off(job: MapJob, dialect: Dialect) -> None:
    """Print the note about turning reasoning off once per run.

    build_request computes the same note per request, but a bulk run would
    print it once per group.
    """
    if job.thinking:
        return
    _, note = thinking_off_payload(dialect, job.model)
    if note is not None:
        print(f"warning: {note}", file=sys.stderr)


def _prepare(job: MapJob) -> _Plan:
    """Read and check every input before any request is sent."""
    items = load_items(job.items_path)
    template = job.template_path.read_text(encoding="utf-8")
    validate_all(template, items)
    batch = job.batch
    if batch is None:
        # The marker is the template author's declaration that the template
        # is batch-safe. Without it, one item per call is the only shape
        # that is known to work.
        batch = 10 if PER_ITEM_MARKER in template else 1
    # Split once, up front. A template that cannot be batched should fail
    # before any request, not after the first group has been paid for.
    preamble, per_item = split_template(template) if batch > 1 else ("", "")
    schema = load_schema(job.schema_file) if job.schema_file is not None else None
    job.outdir.mkdir(parents=True, exist_ok=True)
    # Before resume reads the index and before any request is sent.
    _check_index(job.outdir / "results.jsonl")
    pending = _pending(job, items)
    dialect = resolve_dialect(job.model, job.dialect)
    _warn_thinking_off(job, dialect)
    return _Plan(
        job=job,
        items=items,
        pending=pending,
        template=template,
        batch=batch,
        preamble=preamble,
        per_item=per_item,
        schema=schema,
        dialect=dialect,
        max_tokens=job.max_tokens or DEFAULT_MAX_TOKENS,
    )


def _start(job: MapJob, key: str, prior_spend: int) -> _RunState:
    """The state of a run before its first request, both ceilings checked.

    The per-run budget is seeded from the charge rows of earlier
    invocations, so the ceiling belongs to the job rather than to one
    command. Resetting it per run would make the cap bypassable by
    pressing up-arrow and Enter. Checking both ceilings here keeps an
    already-exhausted budget from paying for one request per concurrency
    slot before noticing.
    """
    budget = TokenBudget(job.token_budget, spent=prior_spend) if job.token_budget else None
    run = _RunState(
        index=job.outdir / "results.jsonl",
        run_id=job.run_id,
        key=key,
        budget=budget,
        rolling_budget=job.rolling_budget,
    )
    limits: tuple[tuple[BudgetName, TokenBudget | None], ...] = (
        ("per_run", budget),
        ("rolling", job.rolling_budget),
    )
    for name, limit in limits:
        if limit is not None and limit.should_abort():
            run.abort_reason = "budget"
            run.tripped_budget = name
            break
    return run


async def _schedule(
    client: httpx.AsyncClient, settings: CallSettings, plan: _Plan, run: _RunState
) -> None:
    """Send every pending item, at most `concurrency` requests at a time.

    The send mode comes from the run's batch setting, never from the size
    of a group: a last group of one in a batched run is still batched.
    """
    semaphore = asyncio.Semaphore(plan.job.concurrency)
    size = plan.batch

    async def send(group: list[dict[str, Any]]) -> None:
        async with semaphore:
            if run.abort_reason is not None:
                return
            run.tally.attempted += len(group)
            if size > 1:
                result = await send_batch(client, settings, plan, group)
            else:
                result = await send_single(client, settings, plan, group[0])
            record(run, result)

    if size > 1:
        groups = [plan.pending[n : n + size] for n in range(0, len(plan.pending), size)]
    else:
        groups = [[item] for item in plan.pending]
    try:
        await asyncio.gather(*(send(group) for group in groups))
    except asyncio.CancelledError:
        # The stop arrived by signal. Rows already written are complete,
        # because recording never awaits, so the counts are final here.
        # Requests still in flight are dropped and not recorded. This reason
        # replaces any earlier one, because the process is about to end by
        # the signal.
        run.abort_reason = "interrupted"
        run.tripped_budget = None


def _outcome(plan: _Plan, run: _RunState, prior_spend: int, elapsed: float) -> MapOutcome:
    tally = run.tally
    rolling = run.rolling_budget
    return MapOutcome(
        ok=tally.ok,
        failed=tally.failed,
        skipped=len(plan.items) - len(plan.pending),
        attempted=tally.attempted,
        total=len(plan.items),
        abort_reason=run.abort_reason,
        tripped_budget=run.tripped_budget,
        charged=tally.charged,
        rolling_spent=rolling.spent if rolling is not None else None,
        # Job-wide either way. Without a ceiling there is no accumulator to
        # read, so add this invocation's charge to what earlier ones left in
        # the index rather than reporting only what this run spent.
        spent=run.budget.spent if run.budget is not None else prior_spend + tally.charged,
        input_tokens=tally.input_tokens,
        output_tokens=tally.output_tokens,
        cached_tokens=tally.cached_tokens,
        reasoning_tokens=tally.reasoning_tokens,
        elapsed=elapsed,
        outdir=plan.job.outdir,
        failures=tally.failures,
        batch=plan.batch,
        internal_errors=tally.internal,
        index_error=run.index_error if run.abort_reason == "index" else None,
    )


async def run_job(client: httpx.AsyncClient, settings: CallSettings, job: MapJob) -> MapOutcome:
    """Set up a map run, send its items and report the outcome."""
    plan = _prepare(job)
    # Read once and reused: it seeds the per-run budget and feeds the
    # reported total, so a resumed run without a ceiling still tells the
    # truth about the job.
    prior_spend = spent_tokens(job.outdir) if job.resume else 0
    run = _start(job, settings.key, prior_spend)
    started = time.monotonic()
    await _schedule(client, settings, plan, run)
    return _outcome(plan, run, prior_spend, time.monotonic() - started)


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
    batch: int | None = None,
    token_budget: int | None = None,
    rolling_budget: TokenBudget | None = None,
    run_id: str | None = None,
) -> MapOutcome:
    """run_job with its settings and job given as keyword arguments."""
    settings = CallSettings(base_url=base_url, key=key, timeout=timeout, retries=retries)
    job = MapJob(
        model=model,
        items_path=items_path,
        template_path=template_path,
        outdir=outdir,
        concurrency=concurrency,
        thinking=thinking,
        max_tokens=max_tokens,
        dialect=dialect,
        resume=resume,
        schema_file=schema_file,
        schema_name=schema_name,
        batch=batch,
        token_budget=token_budget,
        rolling_budget=rolling_budget,
        run_id=run_id,
    )
    return await run_job(client, settings, job)
