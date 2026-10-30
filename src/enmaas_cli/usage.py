"""Usage ledger for measuring cost savings. Metadata only, no content."""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


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


def _is_disabled() -> bool:
    """Check if usage logging is disabled via environment variable."""
    return bool(os.environ.get("ENMAAS_NO_USAGE_LOG", ""))


def append_event(ledger: Path, event: UsageEvent) -> None:
    """Write one event as a JSON line. Failure is silent, never breaks the call."""
    if _is_disabled():
        return

    with contextlib.suppress(OSError):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError), ledger.open("a", encoding="utf-8") as f:
            # Convert datetime to ISO string for JSON
            data = asdict(event)
            data["timestamp"] = event.timestamp.isoformat()
            f.write(json.dumps(data) + "\n")


def load_events(ledger: Path) -> list[UsageEvent]:
    """Load all valid events, tolerating corrupt final line or missing file."""
    if not ledger.exists():
        return []

    events: list[UsageEvent] = []
    with contextlib.suppress(OSError):
        content = ledger.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError, ValueError):
                data = json.loads(stripped)
                # Parse timestamp back to datetime
                ts_str = data.pop("timestamp")
                timestamp = datetime.fromisoformat(ts_str)
                events.append(UsageEvent(timestamp=timestamp, **data))
    return events


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


def format_summary(summary: UsageSummary, *, since_days: int | None = None) -> str:
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


def format_summary_json(summary: UsageSummary) -> str:
    """Format summary as JSON for machine consumption."""
    obj = {
        "total_calls": summary.total_calls,
        "total_input_tokens": summary.total_input_tokens,
        "total_output_tokens": summary.total_output_tokens,
        "total_cached_tokens": summary.total_cached_tokens,
        "total_reasoning_tokens": summary.total_reasoning_tokens,
        "total_elapsed": summary.total_elapsed,
        "by_model": summary.by_model,
        "by_command": summary.by_command,
    }
    return json.dumps(obj, indent=2)
