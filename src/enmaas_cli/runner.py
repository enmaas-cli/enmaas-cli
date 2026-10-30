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
from .client import GatewayError, call
from .dialect import (
    Dialect,
    EmptyResponseError,
    build_request,
    default_max_tokens,
    parse_response,
    resolve_dialect,
)
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


def resume_skips(outdir: Path) -> set[str]:
    """IDs recorded ok AND having an output file on disk."""
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
    aborted: bool
    input_tokens: int
    output_tokens: int
    elapsed: float
    outdir: Path
    failures: list[tuple[str, str]]

    @property
    def exit_code(self) -> int:
        if self.aborted:
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
) -> MapOutcome:
    items = load_items(items_path)
    template = template_path.read_text(encoding="utf-8")
    validate_all(template, items)

    schema = load_schema(schema_file) if schema_file is not None else None

    outdir.mkdir(parents=True, exist_ok=True)
    skips = resume_skips(outdir) if resume else set()
    pending = [i for i in items if str(i["id"]) not in skips]

    resolved = resolve_dialect(model, dialect)
    tokens = max_tokens or default_max_tokens(thinking=thinking)
    breaker = CircuitBreaker()
    semaphore = asyncio.Semaphore(concurrency)
    index = outdir / "results.jsonl"
    lock = asyncio.Lock()
    aborted = False
    started = time.monotonic()
    counts = {"ok": 0, "failed": 0, "attempted": 0, "in": 0, "out": 0}
    failures: list[tuple[str, str]] = []

    async def process(item: dict[str, Any]) -> None:
        nonlocal aborted
        item_id = str(item["id"])
        async with semaphore:
            if aborted:
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
            try:
                data = await call(client, base_url, request, timeout=timeout, retries=retries)
                completion = parse_response(data, resolved)
                if schema is not None:
                    validate_against_schema(completion.text, schema)
                path = outdir / f"{item_id}.md"
                path.write_text(completion.text, encoding="utf-8")
                counts["in"] += completion.usage.input_tokens or 0
                counts["out"] += completion.usage.output_tokens or 0
                row = {
                    "id": item_id,
                    "status": "ok",
                    "path": str(path),
                    "tokens": {
                        "in": completion.usage.input_tokens,
                        "out": completion.usage.output_tokens,
                    },
                    "error": None,
                }
                counts["ok"] += 1
                ok = True
            except (GatewayError, EmptyResponseError, ValidationError, OSError) as exc:
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
                    handle.write(json.dumps(row) + "\n")
                breaker.record(ok=ok)
                if breaker.should_abort():
                    aborted = True

    await asyncio.gather(*(process(item) for item in pending))

    return MapOutcome(
        ok=counts["ok"],
        failed=counts["failed"],
        skipped=len(items) - len(pending),
        attempted=counts["attempted"],
        total=len(items),
        aborted=aborted,
        input_tokens=counts["in"],
        output_tokens=counts["out"],
        elapsed=time.monotonic() - started,
        outdir=outdir,
        failures=failures,
    )
