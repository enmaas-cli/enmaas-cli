"""Open-run records, so the spend of a map run that never finished is still counted.

A map run writes one record when it starts and removes it after its ledger
event is written. A run that is killed leaves its record behind. The next
`ask` or `map` finds it, sums the charge rows the dead run left in its index,
and writes the ledger event that run could not.

The records live in their own file beside the ledger, not in it. The ledger
is append-only history of spend that is known; a record must be removed once
it is settled, and while its run is alive its spend is only an estimate read
from the index.

Every record is treated as untrusted. A forged one can add spend, because
that only makes a budget stricter. It can never lower spend, and nothing read
from a record or an index is ever printed or returned.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import stat
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

from .budget import charge_from_rows
from .usage import UsageEvent, _is_disabled, append_event

OPEN_RUNS_FILE = "open-runs.jsonl"
LOCK_FILE = "open-runs.lock"
# A process id can be reused, so a record whose process looks alive is still
# settled once it is this old. Otherwise it could stay open forever.
STALE_AFTER = timedelta(hours=48)
# A record that starts later than this after now is refused. A run writes its
# own start time, so only clock skew between processes can put it ahead, and
# a forged future time would keep a record from ever turning stale.
CLOCK_SKEW = timedelta(minutes=5)
MAX_FILE_BYTES = 1 << 20
# Bytes of index read in one call to settle or live_charge, shared by every
# record it handles, so a command's cost does not grow with the record count.
MAX_READ_BYTES = 64 << 20
# A charge row is short. A longer line is skipped unparsed, so one huge line
# cannot cost memory or parse time.
MAX_LINE_BYTES = 64 << 10
READ_CHUNK_BYTES = 64 << 10
REQUEST_MARKER = b'"request"'
MAX_PATH_CHARS = 4096
MAX_TEXT_CHARS = 1024
# os.kill takes a C int. A larger pid would raise OverflowError, not OSError.
MAX_PID = (1 << 31) - 1
DIALECTS = frozenset({"anthropic", "openai"})
INDEX_NAME = "results.jsonl"

BEGIN_WARNING = (
    "warning: the open-run record could not be written; "
    "if this run is killed its spend is not counted"
)
# Names the exception class only. Its text could hold content read from a file.
DROPPED_WARNING = (
    "warning: an open-run record could not be read and was dropped; "
    "its spend is not counted ({kind})"
)


@dataclass(frozen=True)
class OpenRun:
    run_id: str
    # os.path.abspath of the map output directory.
    outdir: str
    # Byte length of the index when the run started, 0 if it was absent.
    # Rows before it belong to earlier runs, which recorded their own spend.
    offset: int
    # UTC ISO timestamp.
    started: str
    pid: int
    model: str
    dialect: str


def _is_int(value: object) -> TypeGuard[int]:
    # bool first: it subclasses int, so True would pass as 1.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object, limit: int) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= limit and "\x00" not in value


def _parse(line: str) -> OpenRun | None:
    """One record, or None when any field has the wrong type or shape."""
    try:
        data: Any = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    run_id = data.get("run_id")
    outdir = data.get("outdir")
    offset = data.get("offset")
    started = data.get("started")
    pid = data.get("pid")
    model = data.get("model")
    dialect = data.get("dialect")
    if not _is_text(run_id, 64) or not _is_text(model, MAX_TEXT_CHARS):
        return None
    if not _is_text(dialect, 64) or dialect not in DIALECTS:
        return None
    if not _is_text(outdir, MAX_PATH_CHARS) or not os.path.isabs(outdir):
        return None
    if not _is_int(offset) or offset < 0:
        return None
    if not _is_int(pid) or not 0 < pid <= MAX_PID:
        return None
    if not _is_text(started, 64):
        return None
    try:
        moment = datetime.fromisoformat(started)
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return None
    if moment - datetime.now(UTC) > CLOCK_SKEW:
        return None
    return OpenRun(
        run_id=run_id,
        outdir=outdir,
        offset=offset,
        started=started,
        pid=pid,
        model=model,
        dialect=dialect,
    )


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _load(state_dir: Path) -> list[OpenRun]:
    """Every valid record. Raises OSError when the file exists but cannot be read.

    O_NONBLOCK keeps a planted FIFO from blocking the open, so the fstat
    check below can refuse it.
    """
    try:
        fd = os.open(state_dir / OPEN_RUNS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        raw = _read_bounded(fd, MAX_FILE_BYTES)
    finally:
        os.close(fd)
    runs: list[OpenRun] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        run = _parse(line)
        if run is not None:
            runs.append(run)
    return runs


def _store(state_dir: Path, runs: list[OpenRun]) -> None:
    """Replace the data file in one step, or remove it when no record is left."""
    path = state_dir / OPEN_RUNS_FILE
    if not runs:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return
    text = "".join(json.dumps(asdict(run)) + "\n" for run in runs)
    temporary = state_dir / f".{OPEN_RUNS_FILE}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


@contextlib.contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    """Hold the exclusive lock that every read-modify-write of the file takes."""
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing the descriptor releases the lock.
        os.close(fd)


def _index_size(outdir: str) -> int:
    try:
        info = os.stat(os.path.join(outdir, INDEX_NAME), follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def begin(state_dir: Path, *, outdir: Path, model: str, dialect: str) -> OpenRun | None:
    """Record a map run before it sends anything. None when it could not be recorded."""
    if _is_disabled():
        return None
    try:
        absolute = os.path.abspath(outdir)
        run = OpenRun(
            run_id=secrets.token_hex(8),
            outdir=absolute,
            offset=_index_size(absolute),
            started=datetime.now(UTC).isoformat(),
            pid=os.getpid(),
            model=model,
            dialect=dialect,
        )
        # A record the loader would skip protects nothing, so it is refused
        # here with the same warning as a failed write.
        if _parse(json.dumps(asdict(run))) != run:
            raise OSError("the open-run record is not valid")
        with _locked(state_dir):
            runs = _load(state_dir)
            runs.append(run)
            _store(state_dir, runs)
    except OSError:
        print(BEGIN_WARNING, file=sys.stderr)
        return None
    return run


def end(state_dir: Path, run: OpenRun | None) -> None:
    """Remove a run's record once its own ledger event is written."""
    if run is None or _is_disabled():
        return
    with contextlib.suppress(OSError), _locked(state_dir):
        runs = _load(state_dir)
        remaining = [r for r in runs if r.run_id != run.run_id]
        if len(remaining) != len(runs):
            _store(state_dir, remaining)


class IndexRead(NamedTuple):
    """What one index read found."""

    charge: int
    # Bytes read, taken from the caller's read budget.
    used: int
    # False when the budget ran out before the end of the index.
    complete: bool


@dataclass
class _Progress:
    used: int = 0
    at_end: bool = False


def _lines(fd: int, budget: int, progress: _Progress) -> Iterator[bytes]:
    """Lines of at most MAX_LINE_BYTES, read in chunks up to `budget` bytes.

    Memory stays flat: only one chunk and one partial line are held. A
    longer line is dropped as it streams past, never parsed.
    """
    pending = b""
    skipping = False
    while progress.used < budget:
        chunk = os.read(fd, min(READ_CHUNK_BYTES, budget - progress.used))
        if not chunk:
            progress.at_end = True
            break
        progress.used += len(chunk)
        start = 0
        while (newline := chunk.find(b"\n", start)) >= 0:
            # A line held whole in this chunk without the marker is passed
            # over without a copy. Most index lines are item rows.
            wanted = bool(pending) or chunk.find(REQUEST_MARKER, start, newline) >= 0
            if wanted and not skipping and len(pending) + newline - start <= MAX_LINE_BYTES:
                yield pending + chunk[start:newline]
            pending = b""
            skipping = False
            start = newline + 1
        if not skipping:
            pending += chunk[start:]
            if len(pending) > MAX_LINE_BYTES:
                pending = b""
                skipping = True
    # A last line with no newline is parsed only at the end of the file. A
    # line cut by the budget is not.
    if pending and not skipping and progress.at_end:
        yield pending


def _charge_rows(
    fd: int, budget: int, progress: _Progress, run_id: str
) -> Iterator[dict[str, Any]]:
    """The charge rows tagged with `run_id`.

    A row without the tag, or with another run's, is never counted. Another
    run on the same directory writes its own ledger event for its rows.
    """
    for line in _lines(fd, budget, progress):
        # Cheap filter first: only a line that names the row type is parsed.
        if REQUEST_MARKER not in line:
            continue
        try:
            parsed = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            continue
        if isinstance(parsed, dict) and parsed.get("run") == run_id:
            yield parsed


def charge_since(run: OpenRun, budget: int | None = None) -> IndexRead:
    """Sum the charge rows this run wrote after its offset. Any failure gives 0.

    The offset is where reading starts. A row counts only when its `run`
    field is the record's run id.

    Reads at most `budget` bytes, MAX_READ_BYTES by default, as a stream, and
    never holds the rows. The index is opened without following a link and
    must be a regular file, so a forged record cannot point this at a device
    or a FIFO. Only the sum leaves this function, never the content of a row.
    """
    limit = MAX_READ_BYTES if budget is None else budget
    try:
        fd = os.open(
            os.path.join(run.outdir, INDEX_NAME),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError:
        return IndexRead(0, 0, True)
    progress = _Progress()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size < run.offset:
            return IndexRead(0, 0, True)
        os.lseek(fd, run.offset, os.SEEK_SET)
        charge = charge_from_rows(_charge_rows(fd, limit, progress, run.run_id))
    except OSError:
        return IndexRead(0, progress.used, True)
    finally:
        os.close(fd)
    return IndexRead(charge, progress.used, progress.at_end)


def _is_dead(run: OpenRun, now: datetime) -> bool:
    """Whether a record's run can no longer write its own ledger event.

    A record from this process is dead: this process is about to check the
    budget, so it is not inside that run. A permission error from the probe
    means the process exists. Any other error keeps the record, because
    keeping it can only count spend, never lose it.
    """
    if run.pid == os.getpid():
        return True
    if now - datetime.fromisoformat(run.started) > STALE_AFTER:
        return True
    try:
        os.kill(run.pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _killed_event(run: OpenRun, charge: int, now: datetime) -> UsageEvent:
    return UsageEvent(
        timestamp=now,
        command="map",
        model=run.model,
        dialect=run.dialect,
        input_tokens=None,
        cached_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        elapsed=0.0,
        status="error",
        schema_used=False,
        error_kind="Killed",
        items_total=None,
        items_ok=None,
        items_failed=None,
        charge=charge,
    )


def settle(state_dir: Path, ledger: Path) -> None:
    """Write the ledger event for every dead run and remove its record.

    A record is removed only when there was nothing to charge or the ledger
    took the event. A record whose event could not be written stays, so the
    next run tries again rather than losing the spend.
    """
    if _is_disabled():
        return
    with contextlib.suppress(OSError), _locked(state_dir):
        runs = _load(state_dir)
        now = datetime.now(UTC)
        kept: list[OpenRun] = []
        warned = False
        budget = MAX_READ_BYTES
        remaining = budget
        for run in runs:
            # One record that cannot be handled must not stop the command or
            # come back on every later one, so it is dropped with a warning.
            try:
                if remaining <= 0 or not _is_dead(run, now):
                    # Out of budget: the next command reads it.
                    kept.append(run)
                    continue
                read = charge_since(run, remaining)
                fresh = remaining == budget
                remaining -= read.used
                if not read.complete and not fresh:
                    # Cut short by what earlier records used. The next command
                    # starts with this record and a full budget, so the charge
                    # is counted whole. Nothing more is read now.
                    remaining = 0
                    kept.append(run)
                    continue
                # A record that had the full budget and still did not finish
                # is settled with what was read. Keeping it would block every
                # later command on the same read.
                charge = read.charge
                if charge > 0 and not append_event(ledger, _killed_event(run, charge, now)):
                    kept.append(run)
            except Exception as exc:
                warned = _warn_dropped(exc, warned)
        if len(kept) != len(runs):
            _store(state_dir, kept)


def _warn_dropped(exc: Exception, warned: bool) -> bool:
    """Print the dropped-record warning once per call. Returns that it was printed."""
    if not warned:
        print(DROPPED_WARNING.format(kind=type(exc).__name__), file=sys.stderr)
    return True


def live_charge(state_dir: Path) -> int:
    """What runs still in progress have spent so far, from their own index rows.

    Read without the lock: the file is only ever replaced whole, so a reader
    sees one complete version or the other.
    """
    if _is_disabled():
        return 0
    try:
        runs = _load(state_dir)
    except OSError:
        return 0
    now = datetime.now(UTC)
    total = 0
    warned = False
    # One budget for every record. Records past it count as zero here.
    remaining = MAX_READ_BYTES
    for run in runs:
        if remaining <= 0:
            break
        try:
            if not _is_dead(run, now):
                read = charge_since(run, remaining)
                remaining -= read.used
                total += read.charge
        except Exception as exc:
            warned = _warn_dropped(exc, warned)
    return total
