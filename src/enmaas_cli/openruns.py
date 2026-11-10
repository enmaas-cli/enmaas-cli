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
that only makes a budget stricter. It can never lower spend, print file
content, block a later command for good, or cost more than a bounded amount
of time and memory. The model and dialect of a record are written to the
ledger, and both are checked against a strict shape first. No content of an
index is written, printed or returned; only the sum of its charge rows.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

from .budget import charge_from_rows
from .tokens import MAX_RECORDED
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
# Records honored in one file. A run writes one record, so more than this
# many at once is not normal use, and each record costs a probe and a read.
MAX_RECORDS = 256
# Bytes of index read in one call to settle or open_charge, shared by every
# record it handles, so a command's cost does not grow with the record count.
MAX_READ_BYTES = 64 << 20
# A charge row is short. A longer line is skipped unparsed, so one huge line
# cannot cost memory or parse time.
MAX_LINE_BYTES = 64 << 10
READ_CHUNK_BYTES = 64 << 10
REQUEST_MARKER = b'"request"'
MAX_PATH_CHARS = 4096
# The lock is held only for a short read and write, so a longer wait means
# the holder is stuck. Giving up keeps a stuck process from stopping others.
LOCK_WAIT_SECONDS = 5.0
LOCK_RETRY_SECONDS = 0.05
# The model is written to the ledger and printed by `enmaas usage`, so only
# characters that are safe on one terminal line are taken.
MODEL_PATTERN = re.compile(r"[A-Za-z0-9._:/@+-]{1,256}")
# os.kill takes a C int. A larger pid would raise OverflowError, not OSError.
MAX_PID = (1 << 31) - 1
DIALECTS = frozenset({"anthropic", "openai"})
INDEX_NAME = "results.jsonl"

BEGIN_WARNING = (
    "warning: the open-run record could not be written; "
    "if this run is killed its spend is not counted"
)
FULL_WARNING = (
    "warning: the open-run record could not be written because {path} {reason}; "
    "if this run is killed its spend is not counted"
)
# Names the exception class only. Its text could hold content read from a file.
DROPPED_WARNING = (
    "warning: an open-run record could not be read and was dropped; "
    "its spend is not counted ({kind})"
)


TOO_MANY_RECORDS = "holds too many records"
TOO_MANY_BYTES = "is too large"


class _TooLarge(OSError):
    """The record file holds more bytes or records than the loader takes.

    `reason` completes a sentence that starts with the file's path.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Paths already reported as too large. A process runs one command, so this
# keeps the warning to one line per command.
_reported_too_large: set[str] = set()


def _warn_too_large(state_dir: Path, reason: str) -> None:
    path = str(state_dir / OPEN_RUNS_FILE)
    if path not in _reported_too_large:
        _reported_too_large.add(path)
        print(
            f"warning: {path} {reason} and was ignored; delete it if you did not create it",
            file=sys.stderr,
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
    if not _is_text(run_id, 64):
        return None
    if not isinstance(model, str) or MODEL_PATTERN.fullmatch(model) is None:
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

    Raises _TooLarge when the file is over MAX_FILE_BYTES or holds more than
    MAX_RECORDS valid records. Reading only part of it would hide the rest,
    and a later write would drop them for good. O_NONBLOCK keeps a planted
    FIFO from blocking the open, so the fstat check below can refuse it.
    """
    try:
        fd = os.open(state_dir / OPEN_RUNS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return []
        _require_owner(info)
        if info.st_size > MAX_FILE_BYTES:
            raise _TooLarge(TOO_MANY_BYTES)
        raw = _read_bounded(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        raise _TooLarge(TOO_MANY_BYTES)
    runs: list[OpenRun] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        run = _parse(line)
        if run is not None:
            runs.append(run)
            if len(runs) > MAX_RECORDS:
                raise _TooLarge(TOO_MANY_RECORDS)
    return runs


def _temporary(state_dir: Path) -> tuple[Path, int]:
    path = state_dir / f".{OPEN_RUNS_FILE}.{secrets.token_hex(8)}.tmp"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return path, fd


def _discard(temporary: Path | None) -> None:
    if temporary is not None:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _prepare(state_dir: Path, runs: list[OpenRun]) -> Path | None:
    """Write the next version of the data file beside it, without replacing it yet.

    Returns the temporary file, or None when no record is left. Then it
    proves the directory can be written by creating and removing an empty
    temporary file, so the unlink in _commit is not the first write to fail.

    Refuses a list the loader would refuse, so a write can never turn a
    readable file into an ignored one.
    """
    if not runs:
        path, fd = _temporary(state_dir)
        os.close(fd)
        os.unlink(path)
        return None
    text = "".join(json.dumps(asdict(run)) + "\n" for run in runs)
    if len(runs) > MAX_RECORDS:
        raise _TooLarge(TOO_MANY_RECORDS)
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise _TooLarge(TOO_MANY_BYTES)
    temporary, fd = _temporary(state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _discard(temporary)
        raise
    return temporary


def _commit(state_dir: Path, temporary: Path | None) -> None:
    """Put a prepared version in place in one step, or remove the data file."""
    path = state_dir / OPEN_RUNS_FILE
    if temporary is None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return
    try:
        os.replace(temporary, path)
    except BaseException:
        _discard(temporary)
        raise


def _store(state_dir: Path, runs: list[OpenRun]) -> None:
    """Replace the data file in one step, or remove it when no record is left."""
    _commit(state_dir, _prepare(state_dir, runs) if runs else None)


def _require_owner(info: os.stat_result) -> None:
    """Refuse a file another user owns. Checked on the open descriptor, never by path.

    A file planted by someone else could hold records, or hold the lock, that
    this user never wrote.
    """
    if info.st_uid != os.getuid():
        raise OSError("an open-run file is owned by another user")


def _acquire(fd: int) -> None:
    """Take the exclusive lock, or raise OSError after LOCK_WAIT_SECONDS."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise OSError("the open-run lock is held by another process") from None
            time.sleep(LOCK_RETRY_SECONDS)


@contextlib.contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    """Hold the exclusive lock that every read-modify-write of the file takes."""
    # Private, because the files inside name the user's output directories.
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    fd = os.open(state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _require_owner(os.fstat(fd))
        _acquire(fd)
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


def _tail_size(run: OpenRun) -> int:
    """Bytes of the run's index after its offset, 0 when it cannot be sized."""
    try:
        return max(0, _index_size(run.outdir) - run.offset)
    except OSError:
        return 0


def _smallest_first(runs: list[OpenRun]) -> list[OpenRun]:
    """Records in order of the index they still have to read, smallest first.

    The read budget is shared, so a large planted index read first could
    use it ahead of a small real run. One stat per record.
    """
    return sorted(runs, key=_tail_size)


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
    except _TooLarge as exc:
        path = state_dir / OPEN_RUNS_FILE
        print(FULL_WARNING.format(path=path, reason=exc.reason), file=sys.stderr)
        return None
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
    # False when the budget ran out before the end of the index, or the
    # index could not be read.
    complete: bool
    # The class name of the error when the index exists as a regular file
    # but could not be read, such as a permission error. None otherwise.
    failed: str | None = None


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
    """Sum the charge rows this run wrote after its offset.

    The offset is where reading starts. A row counts only when its `run`
    field is the record's run id.

    Reads at most `budget` bytes, MAX_READ_BYTES by default, as a stream, and
    never holds the rows. The index is opened without following a link and
    must be a regular file, so a forged record cannot point this at a device
    or a FIFO. Only the sum leaves this function, never the content of a row.
    The sum is clamped to MAX_RECORDED, the limit of every recorded value.

    A missing index or directory, a link, or a path that is not a regular
    file gives 0 and a complete read. A deleted directory is the documented
    undercount, and the others are forged. Any other error, such as a
    permission error, gives an incomplete read that names the error class,
    because the spend may be real and only out of reach for now.
    """
    limit = MAX_READ_BYTES if budget is None else budget
    path = os.path.join(run.outdir, INDEX_NAME)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return IndexRead(0, 0, True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return IndexRead(0, 0, True)
    except OSError as exc:
        # A link swapped in after the lstat, or a FIFO or socket in its place.
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            return IndexRead(0, 0, True)
        return IndexRead(0, 0, False, type(exc).__name__)
    progress = _Progress()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size < run.offset:
            return IndexRead(0, 0, True)
        os.lseek(fd, run.offset, os.SEEK_SET)
        charge = charge_from_rows(_charge_rows(fd, limit, progress, run.run_id))
    except OSError as exc:
        return IndexRead(0, progress.used, False, type(exc).__name__)
    finally:
        os.close(fd)
    return IndexRead(min(charge, MAX_RECORDED), progress.used, progress.at_end)


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
        # A sum of many rows can pass the limit the ledger reader takes.
        charge=min(charge, MAX_RECORDED),
    )


def settle(state_dir: Path, ledger: Path) -> None:
    """Write the ledger event for every dead run and remove its record.

    A record is removed only when there was nothing to charge or the ledger
    took the event. A record whose event could not be written stays, so the
    next run tries again rather than losing the spend. A record whose index
    could not be read stays too, until it is older than STALE_AFTER.
    """
    if _is_disabled():
        return
    try:
        _settle(state_dir, ledger)
    except _TooLarge as exc:
        _warn_too_large(state_dir, exc.reason)
    except OSError:
        # Nothing was settled. The records stay for the next command.
        pass


def _is_stale(run: OpenRun, now: datetime) -> bool:
    return now - datetime.fromisoformat(run.started) > STALE_AFTER


def _settle(state_dir: Path, ledger: Path) -> None:
    """Settle under the lock. The order makes a failed write never charge a run twice.

    1. Decide which records are dead and what each owes. Nothing is written.
    2. Prepare the record file as it will be once every event is taken. If
       that fails, return before anything is appended.
    3. Append the events.
    4. All taken: commit the prepared file. Otherwise discard it and store
       the list with the records whose event was not taken.

    The window that remains: a stop between step 3 and the commit counts
    those runs twice, once.
    """
    with _locked(state_dir):
        runs = _load(state_dir)
        now = datetime.now(UTC)
        # Records to remove, by identity: two forged records can be equal.
        settled: list[OpenRun] = []
        owed: list[tuple[OpenRun, int]] = []
        warned = False
        dead: list[OpenRun] = []
        for run in runs:
            # One record that cannot be handled must not stop the command or
            # come back on every later one, so it is dropped with a warning.
            try:
                if _is_dead(run, now):
                    dead.append(run)
            except Exception as exc:
                warned = _warn_dropped(type(exc).__name__, warned)
                settled.append(run)
        budget = MAX_READ_BYTES
        remaining = budget
        for run in _smallest_first(dead):
            if remaining <= 0:
                # Out of budget: the next command reads it.
                break
            try:
                read = charge_since(run, remaining)
                fresh = remaining == budget
                remaining -= read.used
                if read.failed is not None:
                    # The spend may be real, so the record waits for a later
                    # command. A record that never becomes readable is
                    # dropped once it is stale, so it cannot stay for good.
                    if _is_stale(run, now):
                        warned = _warn_dropped(read.failed, warned)
                        settled.append(run)
                    continue
                if not read.complete and not fresh:
                    # Cut short by what earlier records used. The next command
                    # reads it with a full budget, so the charge is counted
                    # whole. Nothing more is read now.
                    remaining = 0
                    continue
                # A record that had the full budget and still did not finish
                # is settled with what was read. Keeping it would block every
                # later command on the same read.
                settled.append(run)
                if read.charge > 0:
                    owed.append((run, read.charge))
            except Exception as exc:
                warned = _warn_dropped(type(exc).__name__, warned)
                settled.append(run)
        if not settled:
            return
        gone = {id(run) for run in settled}
        kept = [run for run in runs if id(run) not in gone]
        temporary = _prepare(state_dir, kept)
        try:
            refused = [
                run
                for run, charge in owed
                if not append_event(ledger, _killed_event(run, charge, now))
            ]
        except BaseException:
            _discard(temporary)
            raise
        if not refused:
            _commit(state_dir, temporary)
            return
        _discard(temporary)
        back = gone - {id(run) for run in refused}
        _store(state_dir, [run for run in runs if id(run) not in back])


def _warn_dropped(kind: str, warned: bool) -> bool:
    """Print the dropped-record warning once per call. Returns that it was printed."""
    if not warned:
        print(DROPPED_WARNING.format(kind=kind), file=sys.stderr)
    return True


class OpenCharge(NamedTuple):
    """What the records still present have spent, and whether all of it was read."""

    charge: int
    # False when some spend could not be read, so the sum may be short.
    complete: bool


def open_charge(state_dir: Path) -> OpenCharge:
    """What every run with a record still present has spent, from its own index rows.

    Called after settle, so a dead record still here is one settle could not
    handle. Its spend counts all the same, dead or alive.

    The read is incomplete when the record file could not be loaded, an
    index could not be read, or the shared read budget ran out with records
    unread. A record that had the whole budget and still did not reach the
    end of its index counts what was read, as settle does.

    Read without the lock: the file is only ever replaced whole, so a reader
    sees one complete version or the other.
    """
    if _is_disabled():
        return OpenCharge(0, True)
    try:
        runs = _load(state_dir)
    except _TooLarge as exc:
        _warn_too_large(state_dir, exc.reason)
        return OpenCharge(0, False)
    except OSError:
        return OpenCharge(0, False)
    total = 0
    complete = True
    # One budget for every record, read smallest index first.
    budget = MAX_READ_BYTES
    remaining = budget
    for run in _smallest_first(runs):
        if remaining <= 0:
            complete = False
            break
        try:
            read = charge_since(run, remaining)
        except Exception:
            # Nothing is dropped here. The check refuses instead.
            complete = False
            continue
        fresh = remaining == budget
        remaining -= read.used
        total += read.charge
        if read.failed is not None or (not read.complete and not fresh):
            complete = False
    return OpenCharge(min(total, MAX_RECORDED), complete)
