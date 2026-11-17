"""Usage ledger for measuring cost savings. Metadata only, no content."""

from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import resource
import secrets
import stat
from collections.abc import Callable
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from enmaas_cli.auth import printable
from enmaas_cli.errors import ExitCode
from enmaas_cli.outputs import make_file_private, make_state_dir_private
from enmaas_cli.pricing import (
    OPUS_REFERENCE,
    ModelPrice,
    cost_for,
    opus_equivalent,
)
from enmaas_cli.tokens import MAX_RECORDED, Usage

if TYPE_CHECKING:  # pragma: no cover
    # Types only: both modules reach this one through their own imports.
    from enmaas_cli.ask import AskResult
    from enmaas_cli.runner import MapOutcome


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
    # Optional because rows written before this field existed have none;
    # rolling_spend falls back to deriving one for those.
    charge: int | None = None


def recorded_event(
    *,
    command: str,
    model: str,
    dialect: str,
    usage: Usage,
    elapsed: float,
    status: Literal["ok", "error"],
    error_kind: str | None,
    charge: int,
) -> UsageEvent:
    """The event for a run this CLI did not make, from what `record` was told."""
    return UsageEvent(
        timestamp=datetime.now(UTC),
        command=command,
        model=model,
        dialect=dialect,
        input_tokens=usage.input_tokens,
        cached_tokens=usage.cached_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        elapsed=elapsed,
        status=status,
        schema_used=False,
        error_kind=error_kind,
        items_total=None,
        items_ok=None,
        items_failed=None,
        charge=charge,
    )


def ask_event(result: AskResult, *, schema_used: bool, charge: int) -> UsageEvent:
    """The event for one `ask`. `charge` is the metered cost of its reply."""
    usage = result.usage
    return UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model=result.model,
        dialect=result.dialect,
        input_tokens=usage.input_tokens if usage else None,
        cached_tokens=usage.cached_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        reasoning_tokens=usage.reasoning_tokens if usage else None,
        elapsed=result.elapsed,
        status="ok" if result.exit_code == ExitCode.OK else "error",
        schema_used=schema_used,
        error_kind=result.error_kind,
        items_total=None,
        items_ok=None,
        items_failed=None,
        charge=charge,
    )


# The error kind a map event records for each reason a run can stop early.
# Each reason has its own kind, so a budget stop is never filed as a
# breaker trip. A run with failures that did not stop is PartialFailure.
_MAP_STOP_KINDS = {
    "breaker": "CircuitBreakerError",
    "budget": "TokenBudgetExceeded",
    "interrupted": "Interrupted",
    "index": "IndexWriteError",
}


def map_event(outcome: MapOutcome, *, model: str, dialect: str, schema_used: bool) -> UsageEvent:
    """The event for one `map` invocation."""
    error_kind = None
    if outcome.abort_reason is not None or outcome.failed > 0:
        error_kind = _MAP_STOP_KINDS.get(outcome.abort_reason or "", "PartialFailure")
    return UsageEvent(
        timestamp=datetime.now(UTC),
        command="map",
        model=model,
        dialect=dialect,
        input_tokens=outcome.input_tokens,
        cached_tokens=outcome.cached_tokens,
        output_tokens=outcome.output_tokens,
        reasoning_tokens=outcome.reasoning_tokens,
        elapsed=outcome.elapsed,
        status="error" if outcome.abort_reason is not None else "ok",
        schema_used=schema_used,
        error_kind=error_kind,
        items_total=outcome.total,
        items_ok=outcome.ok,
        items_failed=outcome.failed,
        # The sum of each request's metered charge. The token counts above
        # count every reply too, failures included, but the charge cannot
        # be derived from them: it is clamped per request, and clamping the
        # sums gives a different number.
        charge=outcome.charged,
    )


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


# Text fields every event carries. Printed by `enmaas usage` and used as keys.
_TEXT_FIELDS = ("command", "model", "dialect", "status")
# The longest text field a row may hold. A writer cuts to this length, and a
# reader refuses a longer one, so a row stays cheap to read and print.
MAX_TEXT_CHARS = 1024


def _is_disabled() -> bool:
    """Check if usage logging is disabled via environment variable."""
    return bool(os.environ.get("ENMAAS_NO_USAGE_LOG", ""))


class NotRegularFile(OSError):
    """The ledger path is a link, a FIFO or anything else that is not a regular file."""


def _open_for_append(ledger: Path) -> int:
    """Open the ledger to append one row, creating it and its directory if missing.

    The state directory is created private, mode 0o700, and a new ledger
    mode 0o600: both name the user's models and what they cost. An existing
    directory or ledger from an earlier version loses its group and other
    access here, when this user owns it.

    Defends against a planted file at the ledger path. O_NOFOLLOW refuses a
    link, so a row cannot be sent to the link's target. O_NONBLOCK makes a
    FIFO with no reader fail at once instead of blocking the command, and
    the type check on the descriptor refuses a FIFO that has a reader.
    """
    ledger.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The ledger's parent is made private, so pass a ledger inside the state directory.
    make_state_dir_private(ledger.parent)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(ledger, flags, 0o600)
    except OSError:
        # A link fails here with ELOOP and a FIFO with no reader with ENXIO.
        # Both are named as what they are, so the warning can say which path
        # to fix instead of an error class that does not point at it.
        try:
            planted = not stat.S_ISREG(os.lstat(ledger).st_mode)
        except OSError:
            planted = False
        if planted:
            raise NotRegularFile(f"{ledger} is not a regular file") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(f"{ledger} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    make_file_private(fd)
    return fd


# The room a budgeted command proves before it sends a request. Far more
# than one row, small enough to cost nothing.
LEDGER_HEADROOM = 64 * 1024


def can_append(ledger: Path) -> bool:
    """Whether one row could be appended now, by the same rules append_event uses.

    Proves three things: the ledger opens for append, its directory has room
    for a row, and the process file size limit lets the ledger grow by
    LEDGER_HEADROOM bytes. An open alone is not enough: on a full disk or at
    a size limit the ledger still opens, and every budgeted run would spend
    and record nothing. Creates the ledger and its directory when they are
    missing, and writes no row. A budget can only bind when its spend is
    written down, so a budgeted command proves this before it sends a
    request.

    Not proved: room can still run out while the request is in flight. The
    write then fails, and the command warns that the run was not recorded.
    """
    try:
        fd = _open_for_append(ledger)
        try:
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
    except OSError:
        return False
    soft, _ = resource.getrlimit(resource.RLIMIT_FSIZE)
    if soft != resource.RLIM_INFINITY and size + LEDGER_HEADROOM > soft:
        return False
    return _has_room(ledger.parent)


def _has_room(directory: Path) -> bool:
    """Whether LEDGER_HEADROOM bytes can be written to a new file in `directory`.

    The probe file is created exclusively and without following a link, so
    it never touches a file that is already there, and it is always removed.
    """
    probe = directory / f".usage-room-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        return False
    try:
        try:
            left = memoryview(bytes(LEDGER_HEADROOM))
            while left:
                written = os.write(fd, left)
                if written <= 0:
                    return False
                left = left[written:]
        finally:
            os.close(fd)
    except OSError:
        return False
    finally:
        # The write is already proved or refused, so a failed removal does
        # not change the answer.
        with contextlib.suppress(OSError):
            os.unlink(probe)
    return True


class LedgerWrite(NamedTuple):
    """What became of one ledger row."""

    recorded: bool
    # Why a write that was tried failed, safe to print on one line. None
    # when the row was written, or when ENMAAS_NO_USAGE_LOG meant no write
    # was tried.
    failure: str | None


def append_event(ledger: Path, event: UsageEvent) -> bool:
    """Write one event as a JSON line. Returns whether it was recorded."""
    return write_event(ledger, event).recorded


def write_event(ledger: Path, event: UsageEvent) -> LedgerWrite:
    """Write one event as a JSON line, and say why when it was not written.

    Failure never breaks the call: the work is already done and paid for
    by the time this runs, so raising here would turn a bookkeeping
    problem into a lost result.

    But it does report. A ledger that silently refuses writes is
    indistinguishable from one showing no spend, so a caller enforcing a
    budget needs to know the ceiling just stopped tracking, and a user
    without one needs to know rows stopped. A link or a FIFO at the ledger
    path is refused the same way, and the reason names the path. Any other
    reason is the error class only: the message of an OS error can quote
    more than the path.
    """
    if _is_disabled():
        return LedgerWrite(recorded=False, failure=None)

    # Convert datetime to ISO string for JSON
    data = asdict(event)
    data["timestamp"] = event.timestamp.isoformat()
    # Every count is written as an integer from 0 to MAX_RECORDED, the
    # range the strict reader accepts. Otherwise a float such as 10.0, or
    # a sum above the reader's limit, would make the next budgeted run
    # refuse this tool's own ledger as corrupt.
    for name in _COUNT_FIELDS:
        data[name] = _recorded(data.get(name))
    # The same rules the reader checks, so this tool's own rows always read
    # back: text cut to MAX_TEXT_CHARS, and an elapsed time that is a finite
    # number of zero or more.
    for name in (*_TEXT_FIELDS, "error_kind"):
        value = data.get(name)
        if isinstance(value, str):
            data[name] = value[:MAX_TEXT_CHARS]
    elapsed = data.get("elapsed")
    if not _is_elapsed(elapsed):
        data["elapsed"] = 0.0

    # try/except around the whole `with`, not contextlib.suppress inside
    # it. A buffered write only reaches the disk on close, so the close
    # must be inside the guarded region; combining suppress and open in
    # one `with` statement would catch the close error and then carry on
    # to report success, which is the dropped spend this return value
    # exists to surface.
    try:
        with os.fdopen(_open_for_append(ledger), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data) + "\n")
    except NotRegularFile:
        return LedgerWrite(False, f"{printable(str(ledger))} is not a regular file")
    except OSError as exc:
        return LedgerWrite(False, type(exc).__name__)
    return LedgerWrite(recorded=True, failure=None)


def _recorded(value: object) -> int | None:
    """A count as the ledger stores it: None, or an integer from 0 to MAX_RECORDED."""
    if value is None:
        return None
    if isinstance(value, float):
        # NaN compares false with everything, so it is caught first.
        if value != value:
            return 0
        value = int(max(0.0, min(value, float(MAX_RECORDED))))
    if not isinstance(value, int):
        return None
    return max(0, min(int(value), MAX_RECORDED))


class RowError(ValueError):
    """One ledger row failed a check.

    The text is one of a fixed set chosen here, never a value or a key
    taken from the row, so a forged row cannot put its content into a
    message.
    """


_FIELDS = frozenset(field.name for field in fields(UsageEvent))
_REQUIRED = frozenset(
    field.name
    for field in fields(UsageEvent)
    if field.default is MISSING and field.default_factory is MISSING
)


def _is_elapsed(value: object) -> bool:
    # bool first: it subclasses int.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def _is_text(value: object) -> bool:
    return isinstance(value, str) and len(value) <= MAX_TEXT_CHARS


def _is_timestamp(value: object) -> bool:
    """An ISO time with a time zone. A naive one raises when compared with the window cutoff."""
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    return moment.tzinfo is not None


def _is_count(value: object) -> bool:
    # bool is refused: it subclasses int, so True would pass as 1. Bounded
    # so that sums stay printable.
    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_RECORDED


# The checks a row must pass, in order, each with the reason a failure
# reports. A row is a JSON object with string keys. Text is used as a
# dictionary key and printed, and elapsed is summed, so a value of any other
# type would raise in `enmaas usage`.
_ROW_RULES: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = (
    ("unknown field", lambda row: set(row) <= _FIELDS),
    ("missing field", lambda row: set(row) >= _REQUIRED),
    ("bad timestamp", lambda row: _is_timestamp(row["timestamp"])),
    ("bad text field", lambda row: all(_is_text(row[name]) for name in _TEXT_FIELDS)),
    ("bad text field", lambda row: row["error_kind"] is None or _is_text(row["error_kind"])),
    ("bad elapsed", lambda row: _is_elapsed(row["elapsed"])),
    ("bad count", lambda row: all(_is_count(row.get(name)) for name in _COUNT_FIELDS)),
)


def _event_from_row(data: object) -> UsageEvent:
    """Parse one ledger row, rejecting what must not be trusted.

    Validation matters here because a budget reads these rows, not only a
    report. A negative count subtracts from recorded spend, a
    string count breaks the arithmetic downstream, and a timezone-naive
    timestamp raises when compared against an aware window cutoff. A
    budget that read any of those would be wrong rather than merely
    incomplete. Every failure raises RowError.
    """
    if not isinstance(data, dict):
        raise RowError("not an object")
    for reason, rule in _ROW_RULES:
        if not rule(data):
            raise RowError(reason)
    row = dict(data)
    timestamp = datetime.fromisoformat(row.pop("timestamp"))
    return UsageEvent(timestamp=timestamp, **row)


def _open_for_read(ledger: Path) -> int | None:
    """Open the ledger to read it. None when it does not exist.

    Raises NotRegularFile for a link, a FIFO or any other path that is not a
    regular file. A FIFO would block the read until some other process
    wrote to it, which would hang every command that reads the ledger. The open
    is nonblocking and the type is checked on the descriptor, so nothing
    waits. A link is refused because the writer never writes through one.

    A ledger and a state directory from an earlier version lose their group
    and other access here too, so `enmaas usage` alone makes them private.
    """
    # The ledger's parent is made private, so pass a ledger inside the state directory.
    make_state_dir_private(ledger.parent)
    try:
        fd = os.open(ledger, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise NotRegularFile(f"{ledger} is not a regular file") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(f"{ledger} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    make_file_private(fd)
    return fd


def _read_ledger(ledger: Path) -> bytes | None:
    """The whole ledger, or None when it does not exist. Raises OSError."""
    fd = _open_for_read(ledger)
    if fd is None:
        return None
    chunks: list[bytes] = []
    try:
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def load_events(ledger: Path) -> list[UsageEvent]:
    """Load all valid events, tolerating corrupt rows or a missing file.

    Deliberately forgiving: `enmaas usage` should report what it can
    rather than crash. Enforcement uses load_events_strict instead.
    """
    return load_events_counted(ledger).events


class LedgerRead(NamedTuple):
    """What the forgiving reader found."""

    events: list[UsageEvent]
    # Rows that could not be read.
    skipped: int
    # True when the ledger path is not a regular file, so nothing was read.
    not_regular: bool


def load_events_counted(ledger: Path) -> LedgerRead:
    """The valid events and how many rows were skipped. Never raises on ledger content."""
    events: list[UsageEvent] = []
    skipped = 0
    try:
        raw = _read_ledger(ledger)
    except NotRegularFile:
        return LedgerRead([], 0, True)
    except OSError:
        return LedgerRead([], 0, False)
    if raw is None:
        return LedgerRead([], 0, False)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return LedgerRead([], 0, False)
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(_event_from_row(json.loads(stripped)))
        except (ValueError, RecursionError):
            # RowError and JSONDecodeError are ValueErrors. A deeply
            # nested row raises RecursionError in the decoder.
            skipped += 1
    return LedgerRead(events, skipped, False)


def load_events_strict(ledger: Path) -> tuple[list[UsageEvent], list[str]]:
    """Read the ledger, reporting what load_events deliberately hides.

    A forgiving reader and a strict reader cannot be the same function.
    Reporting should degrade; enforcement must not. A budget that mistook
    an unreadable ledger for one showing zero spend would never bind.

    A missing file is not a problem: a fresh install has spent nothing. A
    path that is not a regular file is one problem that names the path.
    """
    try:
        raw = _read_ledger(ledger)
    except NotRegularFile:
        return [], [f"{printable(str(ledger))} is not a regular file"]
    except OSError as exc:
        # The class name only. The message of a read error can quote bytes
        # of the file.
        return [], [f"cannot be opened ({type(exc).__name__})"]
    if raw is None:
        return [], []
    try:
        # Bytes then decode, so invalid UTF-8 is reportable rather than
        # escaping as an exception that is not an OSError.
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], ["not UTF-8 text"]

    # Each problem is "line N: <reason>", with a reason from a fixed set, so
    # no value or key from a row reaches the message.
    events: list[UsageEvent] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except ValueError:
            problems.append(f"line {number}: not JSON")
            continue
        except RecursionError:
            problems.append(f"line {number}: nested too deeply")
            continue
        try:
            events.append(_event_from_row(data))
        except RowError as exc:
            problems.append(f"line {number}: {exc}")
    return events, problems


def summarize_usage(events: list[UsageEvent]) -> UsageSummary:
    """Aggregate events into totals by model and by command."""
    total_calls = len(events)
    total_input_tokens = sum(e.input_tokens or 0 for e in events)
    total_output_tokens = sum(e.output_tokens or 0 for e in events)
    total_cached_tokens = sum(e.cached_tokens or 0 for e in events)
    total_reasoning_tokens = sum(e.reasoning_tokens or 0 for e in events)
    total_elapsed = sum(e.elapsed for e in events)

    return UsageSummary(
        total_calls=total_calls,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cached_tokens=total_cached_tokens,
        total_reasoning_tokens=total_reasoning_tokens,
        total_elapsed=total_elapsed,
        by_model=_breakdown(events, lambda event: event.model),
        by_command=_breakdown(events, lambda event: event.command),
    )


def _breakdown(
    events: list[UsageEvent], key: Callable[[UsageEvent], str]
) -> dict[str, dict[str, int | float]]:
    """Totals per value of `key`, in the order each value first appears."""
    totals: dict[str, dict[str, int | float]] = {}
    for event in events:
        stats = totals.setdefault(
            key(event),
            {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "elapsed": 0.0,
            },
        )
        stats["calls"] += 1
        stats["input_tokens"] += event.input_tokens or 0
        stats["output_tokens"] += event.output_tokens or 0
        stats["cached_tokens"] += event.cached_tokens or 0
        stats["reasoning_tokens"] += event.reasoning_tokens or 0
        stats["elapsed"] += event.elapsed
    return totals


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
            # Ledger text is printed through printable(): a row is not
            # trusted, and a newline or escape in it would forge output.
            lines.append(
                f"  {printable(model):<40}  {calls:>4} calls  "
                f"{inp:>10,} in  {out:>10,} out  {elapsed:>8.1f}s"
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
                f"  {printable(command):<10}  {calls:>4} calls  "
                f"{inp:>10,} in  {out:>10,} out  {elapsed:>8.1f}s"
            )

    return "\n".join(lines)


def format_summary_json(
    summary: UsageSummary,
    prices: dict[str, ModelPrice] | None = None,
    *,
    skipped_rows: int = 0,
) -> str:
    """Format summary as JSON for machine consumption.

    `skipped_rows` counts the ledger rows that could not be read, so a
    caller that reads only stdout still learns the totals are partial.
    """
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
        "skipped_rows": skipped_rows,
    }
    return json.dumps(obj, indent=2)


def compute_costs(
    summary: UsageSummary, table: dict[str, ModelPrice] | None = None
) -> tuple[float, list[str], float]:
    """Return (spend, unpriced models, Opus-equivalent).

    One computation behind both the text and the JSON renderings. Two
    copies would drift apart, the way a summary of this ledger kept by
    hand goes stale.
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


def _cost_lines(summary: UsageSummary, table: dict[str, ModelPrice]) -> list[str]:
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
            f"  unpriced:       {', '.join(printable(m) for m in unpriced)} "
            "(no rate configured, so not counted above)"
        )
    lines.append(f"Opus-equivalent:  ${reference:,.2f}")
    lines.append(f"  reference:      {OPUS_REFERENCE.label}")
    lines.append("")
    return lines
