"""Usage ledger for measuring cost savings. Metadata only, no content."""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from enmaas_cli.pricing import (
    OPUS_REFERENCE,
    ModelPrice,
    cost_for,
    opus_equivalent,
)


@dataclass(frozen=True)
class UsageEvent:
    """One recorded call. Never contains prompts, responses, or file paths."""

    timestamp: datetime
    command: str  # "ask" or "map"
    model: str
    dialect: str  # "anthropic" or "openai"
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    elapsed: float
    status: str  # "ok" or "error"
    schema_used: bool
    error_kind: str | None  # Class name only, never full message
    items_total: int | None  # For map command
    items_ok: int | None
    items_failed: int | None
    # Exact metered tokens this event cost, summed per request at the
    # point each one completed. Not derivable from the counts above: a
    # map event aggregates them, and max(0, sum_in - sum_cached) is not
    # the sum of the per-request clamps. Measured at 102 versus 12 on two
    # requests of (10 in, 100 cached) and (100 in, 0 cached).
    #
    # Also the only field that sees requests which consumed tokens and
    # then failed, since the counters above record successes only.
    #
    # Optional because rows written before this field existed have none;
    # rolling_spend falls back to deriving one for those.
    charge: int | None = None


@dataclass(frozen=True)
class UsageSummary:
    """Aggregate statistics from events."""

    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int
    total_reasoning_tokens: int
    total_elapsed: float
    by_model: dict[str, dict[str, int | float]]
    by_command: dict[str, dict[str, int | float]]


_COUNT_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "reasoning_tokens",
    "charge",
)


def _is_disabled() -> bool:
    """Check if usage logging is disabled via environment variable."""
    return bool(os.environ.get("ENMAAS_NO_USAGE_LOG", ""))


def append_event(ledger: Path, event: UsageEvent) -> bool:
    """Write one event as a JSON line. Returns whether it was recorded.

    Failure never breaks the call: the work is already done and paid for
    by the time this runs, so raising here would turn a bookkeeping
    problem into a lost result.

    But it does report. A ledger that silently refuses writes is
    indistinguishable from one showing no spend, so a caller enforcing a
    budget needs to know the ceiling just stopped tracking.
    """
    if _is_disabled():
        return False

    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    # Convert datetime to ISO string for JSON
    data = asdict(event)
    data["timestamp"] = event.timestamp.isoformat()
    # Coerce counts on the way out. A gateway reporting 10.0 instead of
    # 10 would otherwise be written verbatim, and the strict reader
    # would then reject this tool's own ledger as corrupt on the next
    # budgeted run.
    for name in _COUNT_FIELDS:
        value = data.get(name)
        if isinstance(value, float):
            data[name] = max(0, int(value))

    # try/except around the whole `with`, not contextlib.suppress inside
    # it. A buffered write only reaches the disk on close, so the close
    # must be inside the guarded region; combining suppress and open in
    # one `with` statement would catch the close error and then carry on
    # to report success, which is the dropped spend this return value
    # exists to surface.
    try:
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data) + "\n")
    except OSError:
        return False
    return True




def _event_from_row(data: object) -> UsageEvent:
    """Parse one ledger row, rejecting what must not be trusted.

    Validation matters here in a way it did not when this was purely a
    reporting path. A negative count subtracts from recorded spend, a
    string count breaks the arithmetic downstream, and a timezone-naive
    timestamp raises when compared against an aware window cutoff. A
    budget that read any of those would be wrong rather than merely
    incomplete.
    """
    if not isinstance(data, dict):
        raise TypeError(f"expected a JSON object, got {type(data).__name__}")

    row = dict(data)
    timestamp = datetime.fromisoformat(row.pop("timestamp"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp is timezone-naive")

    for name in _COUNT_FIELDS:
        count = row.get(name)
        if count is None:
            continue
        # bool first: it subclasses int, so True would pass as 1.
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"{name} must be a nonnegative integer, got {count!r}")

    return UsageEvent(timestamp=timestamp, **row)


def load_events(ledger: Path) -> list[UsageEvent]:
    """Load all valid events, tolerating corrupt final line or missing file.

    Deliberately forgiving: `enmaas usage` should report what it can
    rather than crash. Enforcement uses load_events_strict instead.
    """
    if not ledger.exists():
        return []

    events: list[UsageEvent] = []
    with contextlib.suppress(OSError, UnicodeDecodeError):
        content = ledger.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # AttributeError is in the set because a row of JSON `null`
            # reaches .pop on a None and used to crash this function.
            with contextlib.suppress(
                json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError
            ):
                events.append(_event_from_row(json.loads(stripped)))
    return events


def load_events_strict(ledger: Path) -> tuple[list[UsageEvent], list[str]]:
    """Read the ledger, reporting what load_events deliberately hides.

    A forgiving reader and a strict reader cannot be the same function.
    Reporting should degrade; enforcement must not. A budget that mistook
    an unreadable ledger for one showing zero spend would never bind.

    A missing file is not a problem: a fresh install has spent nothing.
    """
    if not ledger.exists():
        return [], []

    try:
        # Bytes then decode, so invalid UTF-8 is reportable rather than
        # escaping as an exception that is not an OSError.
        text = ledger.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"{ledger} could not be read: {exc}"]

    events: list[UsageEvent] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(_event_from_row(json.loads(stripped)))
        except (
            json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError
        ) as exc:
            problems.append(f"{ledger} line {number}: {type(exc).__name__}: {exc}")
    return events, problems


def summarize_usage(events: list[UsageEvent]) -> UsageSummary:
    """Aggregate events into totals by model and by command."""
    total_calls = len(events)
    total_input_tokens = sum(e.input_tokens or 0 for e in events)
    total_output_tokens = sum(e.output_tokens or 0 for e in events)
    total_cached_tokens = sum(e.cached_tokens or 0 for e in events)
    total_reasoning_tokens = sum(e.reasoning_tokens or 0 for e in events)
    total_elapsed = sum(e.elapsed for e in events)

    by_model: dict[str, dict[str, int | float]] = {}
    by_command: dict[str, dict[str, int | float]] = {}

    for event in events:
        # By model
        if event.model not in by_model:
            by_model[event.model] = {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "elapsed": 0.0,
            }
        model_stats = by_model[event.model]
        model_stats["calls"] += 1
        model_stats["input_tokens"] += event.input_tokens or 0
        model_stats["output_tokens"] += event.output_tokens or 0
        model_stats["cached_tokens"] += event.cached_tokens or 0
        model_stats["reasoning_tokens"] += event.reasoning_tokens or 0
        model_stats["elapsed"] += event.elapsed

        # By command
        if event.command not in by_command:
            by_command[event.command] = {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "elapsed": 0.0,
            }
        cmd_stats = by_command[event.command]
        cmd_stats["calls"] += 1
        cmd_stats["input_tokens"] += event.input_tokens or 0
        cmd_stats["output_tokens"] += event.output_tokens or 0
        cmd_stats["cached_tokens"] += event.cached_tokens or 0
        cmd_stats["reasoning_tokens"] += event.reasoning_tokens or 0
        cmd_stats["elapsed"] += event.elapsed

    return UsageSummary(
        total_calls=total_calls,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cached_tokens=total_cached_tokens,
        total_reasoning_tokens=total_reasoning_tokens,
        total_elapsed=total_elapsed,
        by_model=by_model,
        by_command=by_command,
    )


def format_summary(
    summary: UsageSummary,
    *,
    since_days: int | None = None,
    prices: dict[str, ModelPrice] | None = None,
) -> str:
    """Format summary as human-readable text."""
    lines = []

    if since_days is not None:
        lines.append(f"Usage summary (last {since_days} days)")
    else:
        lines.append("Usage summary (all time)")
    lines.append("")

    if summary.total_calls == 0:
        lines.append("No calls recorded")
        return "\n".join(lines)

    lines.append(f"Total calls:      {summary.total_calls:,}")
    lines.append(f"Total input:      {summary.total_input_tokens:,} tokens")
    if summary.total_cached_tokens > 0:
        lines.append(f"  (cached):       {summary.total_cached_tokens:,} tokens")
    lines.append(f"Total output:     {summary.total_output_tokens:,} tokens")
    if summary.total_reasoning_tokens > 0:
        lines.append(f"  (reasoning):    {summary.total_reasoning_tokens:,} tokens")
    lines.append(f"Total elapsed:    {summary.total_elapsed:.1f}s")
    lines.append("")
    lines.extend(_cost_lines(summary, prices or {}))

    if summary.by_model:
        lines.append("By model:")
        for model, stats in sorted(summary.by_model.items()):
            calls = int(stats["calls"])
            inp = int(stats["input_tokens"])
            out = int(stats["output_tokens"])
            elapsed = float(stats["elapsed"])
            lines.append(
                f"  {model:<40}  {calls:>4} calls  {inp:>10,} in  {out:>10,} out  {elapsed:>8.1f}s"
            )
        lines.append("")

    if summary.by_command:
        lines.append("By command:")
        for command, stats in sorted(summary.by_command.items()):
            calls = int(stats["calls"])
            inp = int(stats["input_tokens"])
            out = int(stats["output_tokens"])
            elapsed = float(stats["elapsed"])
            lines.append(
                f"  {command:<10}  {calls:>4} calls  "
                f"{inp:>10,} in  {out:>10,} out  {elapsed:>8.1f}s"
            )

    return "\n".join(lines)


def format_summary_json(
    summary: UsageSummary, prices: dict[str, ModelPrice] | None = None
) -> str:
    """Format summary as JSON for machine consumption."""
    spend, unpriced, reference = compute_costs(summary, prices)
    obj = {
        "total_calls": summary.total_calls,
        "total_input_tokens": summary.total_input_tokens,
        "total_output_tokens": summary.total_output_tokens,
        "total_cached_tokens": summary.total_cached_tokens,
        "total_reasoning_tokens": summary.total_reasoning_tokens,
        "total_elapsed": summary.total_elapsed,
        "by_model": summary.by_model,
        "by_command": summary.by_command,
        "spend_usd": spend,
        "unpriced_models": unpriced,
        "opus_equivalent_usd": reference,
        "opus_reference": OPUS_REFERENCE.label,
    }
    return json.dumps(obj, indent=2)


def compute_costs(
    summary: UsageSummary, table: dict[str, ModelPrice] | None = None
) -> tuple[float, list[str], float]:
    """Return (spend, unpriced models, Opus-equivalent).

    One computation behind both the text and the JSON renderings. Two
    copies would drift, which is how a hand-maintained summary of this
    very ledger went stale in the first place.
    """
    table = table or {}
    spend = 0.0
    unpriced: list[str] = []
    total_in = 0
    total_out = 0
    for model, stats in sorted(summary.by_model.items()):
        tin = int(stats["input_tokens"])
        tout = int(stats["output_tokens"])
        total_in += tin
        total_out += tout
        cost = cost_for(model, tin, tout, table)
        if cost is None:
            unpriced.append(model)
        else:
            spend += cost
    return spend, unpriced, opus_equivalent(total_in, total_out)


def _cost_lines(
    summary: UsageSummary, table: dict[str, ModelPrice]
) -> list[str]:
    """Spend and the reference comparison, kept clearly apart.

    Spend is what the gateway charged, and is known only for models with a
    rate. The Opus-equivalent is what the same tokens would have cost had
    this session generated them, which is the figure that makes delegating
    worth measuring. Adding the two would be meaningless.
    """
    spend, unpriced, reference = compute_costs(summary, table)

    lines = [f"Spend:            ${spend:,.2f}"]
    if unpriced:
        lines.append(
            f"  unpriced:       {', '.join(unpriced)} "
            f"(no rate configured, so not counted above)"
        )
    lines.append(f"Opus-equivalent:  ${reference:,.2f}")
    lines.append(f"  reference:      {OPUS_REFERENCE.label}")
    lines.append("")
    return lines
