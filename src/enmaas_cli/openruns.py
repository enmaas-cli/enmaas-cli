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
that only makes a budget stricter. Reads use bounded time and memory and
never print stored content. Incomplete indexes and unreadable record files
retain their records and block budgeted work until accounting is restored.
The model and dialect of a record are written to the ledger, so the model
must be printable and short and the dialect must be a known one. No content
of an index is written, printed or returned; only the sum of its charge rows.
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
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

from .outputs import make_file_private, make_state_dir_private
from .runner import _charge_row_parts
from .tokens import MAX_RECORDED
from .usage import MAX_TEXT_CHARS, UsageEvent, _is_disabled, append_event

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
# Bytes of index read in one pass, by settle or by open_charge, shared by
# every record the pass handles, so a command's cost does not grow with the
# record count. A command makes both passes, so it reads at most twice this.
MAX_READ_BYTES = 64 << 20
READ_CHUNK_BYTES = 64 << 10
# The digits a charge and an item count can have in a charge row. A charge
# is at most MAX_RECORDED, which has 19 digits. A request never carries more
# items than a run can hold in memory.
CHARGE_DIGITS = 19
ITEMS_DIGITS = 10
MAX_PATH_CHARS = 4096
# The lock is held only for a short read and write, so a longer wait means
# the holder is stuck. Giving up keeps a stuck process from stopping others.
LOCK_WAIT_SECONDS = 5.0
LOCK_RETRY_SECONDS = 0.05
# The model is written to the ledger and printed by `enmaas usage`, so it
# must be printable on one terminal line, and no longer than the ledger
# reader takes.
MAX_MODEL_CHARS = MAX_TEXT_CHARS
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
            f"warning: {path} {reason}; records were retained; "
            "restore accounting before budgeted work can resume",
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


def _is_model(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_MODEL_CHARS and value.isprintable()


def _is_dialect(value: object) -> bool:
    return _is_text(value, 64) and value in DIALECTS


def _is_outdir(value: object) -> bool:
    return _is_text(value, MAX_PATH_CHARS) and os.path.isabs(value)


def _is_offset(value: object) -> bool:
    return _is_int(value) and value >= 0


def _is_pid(value: object) -> bool:
    return _is_int(value) and 0 < value <= MAX_PID


def _is_started(value: object) -> bool:
    """An aware ISO time no later than CLOCK_SKEW after now."""
    if not _is_text(value, 64):
        return False
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    if moment.utcoffset() is None:
        return False
    return moment - datetime.now(UTC) <= CLOCK_SKEW


# The rule each field of a record must pass. A record that fails any one is
# refused as a whole, and fields not named here are ignored.
_FIELD_RULES: dict[str, Callable[[object], bool]] = {
    "run_id": lambda value: _is_text(value, 64),
    "model": _is_model,
    "dialect": _is_dialect,
    "outdir": _is_outdir,
    "offset": _is_offset,
    "pid": _is_pid,
    "started": _is_started,
}


def _parse(line: str) -> OpenRun | None:
    """One record, or None when any field has the wrong type or shape."""
    try:
        data: Any = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    if not all(rule(data.get(name)) for name, rule in _FIELD_RULES.items()):
        return None
    return OpenRun(**{name: data[name] for name in _FIELD_RULES})


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
    """Every record, or OSError when any part of the file cannot be read or parsed.

    Raises _TooLarge when the file is over MAX_FILE_BYTES or holds more than
    MAX_RECORDS records. Accepting only part of it would hide the rest,
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
            raise OSError("the open-run record file is not a regular file")
        _require_owner(info)
        # A record file from an earlier version can be open to other users.
        make_file_private(fd)
        if info.st_size > MAX_FILE_BYTES:
            raise _TooLarge(TOO_MANY_BYTES)
        raw = _read_bounded(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        raise _TooLarge(TOO_MANY_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OSError("the open-run record file is not valid UTF-8") from None
    runs: list[OpenRun] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        run = _parse(line)
        if run is None:
            raise OSError("the open-run record file contains an invalid record")
        runs.append(run)
        if len(runs) > MAX_RECORDS:
            raise _TooLarge(TOO_MANY_RECORDS)
    return runs


# The names _temporary gives, and how old a leftover one must be before
# settle removes it.
TEMPORARY_NAME = re.compile(re.escape(f".{OPEN_RUNS_FILE}.") + r"[0-9a-f]{16}\.tmp")
LEFTOVER_AFTER = timedelta(hours=1)


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
    # A directory or a lock file from an earlier version can be open to other
    # users, so both are made private here as well.
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    make_state_dir_private(state_dir)
    fd = os.open(state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _require_owner(os.fstat(fd))
        make_file_private(fd)
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
        # A record the loader would refuse protects nothing, so it is refused
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


class ChargePattern(NamedTuple):
    """How to find one run's charge rows in its index."""

    # Matches a whole charge row of the run, with the charge as group 1.
    regex: re.Pattern[bytes]
    # The longest line the regex can match. A longer line is never one.
    longest: int


def charge_pattern(run_id: str) -> ChargePattern:
    """The pattern of a whole charge row that _charge_row writes for `run_id`.

    Built from the writer's own text, so the two cannot drift. The run id is
    in the pattern, so a row of another run, or of no run, does not match,
    and no line is ever parsed as JSON.
    """
    head, middle, tail = (part.encode("utf-8") for part in _charge_row_parts(run_id))
    regex = re.compile(
        b"^"
        + re.escape(head)
        + rb"(\d{1,%d})" % CHARGE_DIGITS
        + re.escape(middle)
        + rb"\d{1,%d}" % ITEMS_DIGITS
        + re.escape(tail)
        + b"$",
        re.MULTILINE,
    )
    longest = len(head) + CHARGE_DIGITS + len(middle) + ITEMS_DIGITS + len(tail)
    return ChargePattern(regex, longest)


def _at_eof(fd: int) -> bool:
    """Check the open file's position without reading beyond the byte budget."""
    return os.lseek(fd, 0, os.SEEK_CUR) >= os.fstat(fd).st_size


def _reference_charges(
    fd: int, budget: int, progress: _Progress, pattern: ChargePattern
) -> Iterator[int]:
    """The charge of every line that is a whole charge row, read in chunks up to `budget` bytes.

    A plain line splitter with the same pattern, kept as the reference the
    scan below is tested against. It holds any line whole, so it is not
    used on an index.
    """
    pending = b""
    while progress.used < budget:
        chunk = os.read(fd, min(READ_CHUNK_BYTES, budget - progress.used))
        if not chunk:
            progress.at_end = True
            break
        progress.used += len(chunk)
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            if match := pattern.regex.fullmatch(line):
                yield int(match.group(1))
    progress.at_end = progress.at_end or _at_eof(fd)
    # A last line with no newline is used only at the end of the file. A
    # line cut by the budget is not.
    if pending and progress.at_end and (match := pattern.regex.fullmatch(pending)):
        yield int(match.group(1))


def _scan_charges(
    fd: int, budget: int, progress: _Progress, pattern: ChargePattern
) -> Iterator[int]:
    """The charge of every whole charge row of the pattern's run.

    Reads in chunks up to `budget` bytes. Each chunk is scanned once by the
    pattern, so a line that is not a charge row of this run costs no Python
    work and is never parsed. Memory stays flat: one chunk, and the start of
    a line that crosses into the next chunk, kept only while it is short
    enough to be a row.
    """
    pending = b""
    # True inside a line longer than any row, which is skipped.
    skipping = False
    while progress.used < budget:
        chunk = os.read(fd, min(READ_CHUNK_BYTES, budget - progress.used))
        if not chunk:
            progress.at_end = True
            break
        progress.used += len(chunk)
        last = chunk.rfind(b"\n")
        if last < 0:
            if not skipping:
                pending += chunk
                if len(pending) > pattern.longest:
                    pending = b""
                    skipping = True
            continue
        first = chunk.find(b"\n")
        # The line ending at the first newline began in an earlier chunk.
        fits = not skipping and len(pending) + first <= pattern.longest
        if fits and (match := pattern.regex.fullmatch(pending + chunk[:first])):
            yield int(match.group(1))
        for match in pattern.regex.finditer(chunk, first + 1, last):
            yield int(match.group(1))
        pending = chunk[last + 1 :]
        skipping = len(pending) > pattern.longest
        if skipping:
            pending = b""
    # The final read may have used exactly the budget. Check this descriptor
    # before using a final row without a newline, without reading more bytes.
    progress.at_end = progress.at_end or _at_eof(fd)
    if not skipping and progress.at_end and (match := pattern.regex.fullmatch(pending)):
        yield int(match.group(1))


def _sum_charges(charges: Iterator[int]) -> int:
    """The sum of the charges, each taken only when it is at most MAX_RECORDED.

    No writer of this tool writes a larger one, and the bound keeps each
    term small enough to print.
    """
    return sum(charge for charge in charges if charge <= MAX_RECORDED)


def charge_since(run: OpenRun, budget: int | None = None) -> IndexRead:
    """Sum the charge rows this run wrote after its offset.

    The offset is where reading starts. A line counts only when it is a
    whole charge row as the runner writes it for the record's run id.

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
        pattern = charge_pattern(run.run_id)
        charge = _sum_charges(_scan_charges(fd, limit, progress, pattern))
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
    could not be read stays too, until it is older than STALE_AFTER. An index
    cut short by the read limit keeps its record even when it is stale.
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
    """Settle the dead runs. The lock is never held while an index is read.

    Reading an index can take far longer than another command waits for the
    lock, and a map that gave up waiting would run with no record. So:

    1. Under the lock, load the records and pick the dead ones.
    2. With no lock held, read their indexes.
    3. Under the lock, load again. For each picked record still present,
       prepare the record file as it will be once every event is taken,
       append the events, then commit. A record another command settled in
       between is no longer present and is skipped, so it is charged once.

    The prepared file is written before any append. If that fails, nothing
    is appended. If an append is refused, the prepared file is discarded and
    the records whose event was not taken are kept.

    The window that remains: a stop between the appends and the commit
    counts those runs twice, once.
    """
    settling = _pick_dead(state_dir)
    if settling is None:
        return
    _read_dead(settling)
    with _locked(state_dir):
        if settling.decided:
            _apply(state_dir, ledger, settling.decided, settling.now)
        _remove_leftovers(state_dir)


@dataclass
class _Settling:
    """What one settle has decided so far."""

    now: datetime
    # Records to remove, each with what it owes. 0 means remove with no event.
    decided: list[tuple[OpenRun, int]] = field(default_factory=list)
    dead: list[OpenRun] = field(default_factory=list)
    # Whether the dropped-record warning was printed, so it is printed once.
    warned: bool = False

    def drop(self, run: OpenRun, kind: str) -> None:
        """Remove a record with no event, with a warning that names `kind`."""
        self.warned = _warn_dropped(kind, self.warned)
        self.decided.append((run, 0))


def _pick_dead(state_dir: Path) -> _Settling | None:
    """Under the lock, load the records and pick the dead ones. None when there are none."""
    with _locked(state_dir):
        runs = _load(state_dir)
        settling = _Settling(now=datetime.now(UTC))
        for run in runs:
            # One record that cannot be handled must not stop the command or
            # come back on every later one, so it is dropped with a warning.
            try:
                if _is_dead(run, settling.now):
                    settling.dead.append(run)
            except Exception as exc:
                settling.drop(run, type(exc).__name__)
        if not settling.dead and not settling.decided:
            # Nothing to settle, so this is the last step under the lock.
            _remove_leftovers(state_dir)
            return None
    return settling


def _read_dead(settling: _Settling) -> None:
    """With no lock held, read what each dead run charged, smallest index first."""
    remaining = MAX_READ_BYTES
    for run in _smallest_first(settling.dead):
        if remaining <= 0:
            # Out of budget: the next command reads it.
            break
        try:
            read = charge_since(run, remaining)
            remaining -= read.used
            if read.failed is not None:
                # The spend may be real, so the record waits for a later
                # command. A record that never becomes readable is dropped
                # once it is stale, so it cannot stay for good.
                if _is_stale(run, settling.now):
                    settling.drop(run, read.failed)
                continue
            if not read.complete:
                # A partial charge cannot become a final ledger event. Keep
                # the record until its whole index can be accounted for.
                remaining = 0
                continue
            settling.decided.append((run, read.charge))
        except Exception as exc:
            settling.drop(run, type(exc).__name__)


def _remove_leftovers(state_dir: Path) -> None:
    """Remove temporary record files older than LEFTOVER_AFTER. Holds the lock.

    A process stopped between writing a temporary file and putting it in
    place leaves it behind. A younger one may belong to a write in progress
    in another process, so it is left alone.
    """
    cutoff = time.time() - LEFTOVER_AFTER.total_seconds()
    with contextlib.suppress(OSError), os.scandir(state_dir) as entries:
        for entry in entries:
            if TEMPORARY_NAME.fullmatch(entry.name) is None:
                continue
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)


def _apply(
    state_dir: Path, ledger: Path, decided: list[tuple[OpenRun, int]], now: datetime
) -> None:
    """Remove the decided records still present and append their events. Holds the lock."""
    current = _load(state_dir)
    pending = list(decided)
    # By identity: two forged records can be equal, and each is matched once.
    gone: set[int] = set()
    owed: list[tuple[OpenRun, int]] = []
    for entry in current:
        for position, (run, charge) in enumerate(pending):
            if run == entry:
                gone.add(id(entry))
                if charge > 0:
                    owed.append((entry, charge))
                del pending[position]
                break
    if not gone:
        return
    temporary = _prepare(state_dir, [run for run in current if id(run) not in gone])
    try:
        refused = {
            id(run)
            for run, charge in owed
            if not append_event(ledger, _killed_event(run, charge, now))
        }
    except BaseException:
        _discard(temporary)
        raise
    if not refused:
        _commit(state_dir, temporary)
        return
    _discard(temporary)
    _store(state_dir, [run for run in current if id(run) not in gone or id(run) in refused])


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
    unread. A partial charge is returned with complete=False, even when
    that record had the whole read budget.

    Read without the lock: the file is only ever replaced whole, so a reader
    sees one complete version or the other.
    """
    if _is_disabled():
        return OpenCharge(0, True)
    # No lock is taken here, so this read makes the directory private itself.
    make_state_dir_private(state_dir)
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
    remaining = MAX_READ_BYTES
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
        remaining -= read.used
        total += read.charge
        if not read.complete:
            complete = False
    return OpenCharge(min(total, MAX_RECORDED), complete)
