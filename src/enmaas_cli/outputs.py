"""Local output protection and bounded inspection using the standard library."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

MARKER_DIR = ".enmaas-output"
MARKER_VERSION = 1
MAX_INSPECT_CHARS = 2000
_MAX_RECORD_BYTES = 4096
_TRUNCATION_NOTICE = "[truncated]\n"


def state_root() -> Path:
    """Return the configured run directory without creating it."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "enmaas" / "runs"


def make_state_dir_private(directory: Path) -> None:
    """Remove group and other access from the state directory. Never raises.

    A version before 0.8.0 created the directory readable by every user on
    the machine, and creating it private now does not change one that
    already exists. Every command calls this once when it starts, and so
    does every site that creates the state directory or reads a file in it.
    A directory that is missing is not created.

    Only a real directory this user owns is changed. O_NOFOLLOW refuses a
    link at the last component, so a link to some other directory leaves
    that directory alone. The check and the change use one descriptor, so
    a directory swapped in between them cannot receive the change. Owner
    bits are only ever removed, never added, so a directory its owner made
    read-only stays read-only.

    A failure is ignored without a message: a file system that is read-only
    or has no modes would otherwise print a warning on every command, and
    the command itself has lost nothing.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode) and _too_open(info):
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) & 0o700)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def make_file_private(descriptor: int) -> None:
    """Remove group and other access from an open state file. Never raises.

    For the usage ledger, the open-run records, their lock file and a `map`
    index, each of which an earlier version created readable by every user
    on the machine. Never for an answer file: its owner chose where it goes
    and who reads it.

    Only a regular file this user owns is changed, and only through the
    descriptor the caller already checked, never by path. Owner bits are
    only ever removed, so a file its owner made read-only stays read-only.
    A file with more than one hard link is not changed: the mode belongs to
    the file, so the change would reach another name of the same file, in a
    place this tool knows nothing about.
    A failure is ignored for the same reason as in make_state_dir_private.
    """
    try:
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode) and info.st_nlink <= 1 and _too_open(info):
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) & 0o600)
    except OSError:
        pass


def _too_open(info: os.stat_result) -> bool:
    """Whether this user owns the file and its group or other users have any access."""
    return info.st_uid == os.getuid() and bool(info.st_mode & 0o077)


def marker_path(path: Path) -> Path:
    """Return the exact marker for the resolved output path."""
    resolved = path.resolve()
    digest = hashlib.sha256(os.fsencode(resolved.name)).hexdigest()
    return resolved.parent / MARKER_DIR / f"{digest}.json"


def _record_state(path: Path) -> str | None:
    marker = marker_path(path)
    try:
        descriptor = os.open(marker, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return "protected"
            raw = os.read(descriptor, _MAX_RECORD_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _MAX_RECORD_BYTES:
            return "protected"
        record = json.loads(raw)
    except FileNotFoundError:
        return "protected" if marker.is_symlink() else None
    except (OSError, ValueError, RecursionError):
        return "protected"
    if (
        not isinstance(record, dict)
        or set(record) != {"version", "basename", "state"}
        or type(record["version"]) is not int
        or record["version"] != MARKER_VERSION
        or record["basename"] != path.name
        or record["state"] not in ("protected", "allowed")
    ):
        return "protected"
    return str(record["state"])


def _write_record(path: Path, state: str) -> None:
    marker = marker_path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=marker.parent,
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(
                {
                    "version": MARKER_VERSION,
                    "basename": path.name,
                    "state": state,
                },
                handle,
            )
            handle.write("\n")
        os.replace(temporary, marker)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _link_refusal(path: Path) -> OSError:
    return OSError(f"refusing to write through a symbolic link: {path}")


def write_output(path: Path, text: str) -> None:
    """Write an answer without following a symbolic link at the final path.

    A plain write follows a link, so a pre-existing output name that points
    at another writable file truncated that file. The mode applies only when
    the file is created; an existing file keeps the mode its owner chose.

    Only a regular file is written. Another process could read an answer
    sent to a FIFO and stop this one before its spend is recorded. The open
    is nonblocking, so a FIFO cannot hold it, and the type is checked on the
    open descriptor.

    A file with more than one hard link is refused, because another name
    for the same file could be any file its owner can write. The open does
    not truncate: truncating at open time would empty that other file before
    any check ran. The file is truncated only after both checks pass on the
    open descriptor.
    """
    if path.is_symlink():
        raise _link_refusal(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        # The link can appear between the check above and the open.
        if exc.errno == errno.ELOOP:
            raise _link_refusal(path) from None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(
                f"refusing to write an answer to a path that is not a regular file: {path}"
            )
        if info.st_nlink > 1:
            raise OSError(
                f"refusing to write an answer through a file with more than one hard link: {path}"
            )
        os.ftruncate(descriptor, 0)
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(text.encode("utf-8"))


def protect(path: Path) -> None:
    """Atomically mark one resolved output path as protected."""
    # Resolving a link would write the marker for its target, which then
    # names a file this tool never wrote.
    if path.is_symlink():
        raise _link_refusal(path)
    resolved = path.resolve()
    try:
        _write_record(resolved, "protected")
    except OSError as exc:
        raise OSError(f"cannot protect output {resolved}: {exc}") from exc


def is_protected(path: Path) -> bool:
    """Check the exact marker or the older default ask layout."""
    resolved = path.resolve()
    state = _record_state(resolved)
    if state is not None:
        return state == "protected"
    return (
        resolved.name in {"response.md", "thinking.md"}
        and resolved.parent.parent == state_root().resolve()
    )


def allow_read(path: Path) -> None:
    """Grant a deliberate exception for one existing protected file."""
    resolved = path.resolve(strict=True)
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError("allow-read requires one regular file")
    if _record_state(resolved) == "allowed":
        return
    if not is_protected(resolved):
        raise ValueError(f"file is not a protected enmaas output: {resolved}")
    _write_record(resolved, "allowed")


def inspect_file(path: Path, *, max_chars: int = MAX_INSPECT_CHARS) -> str:
    """Return a bounded prefix, including any notice and final newline."""
    if not 1 <= max_chars <= MAX_INSPECT_CHARS:
        raise ValueError(f"max-chars must be between 1 and {MAX_INSPECT_CHARS}")
    # Nonblocking open lets the regular-file check reject pipes without waiting.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("inspect requires one regular file")
        # UTF-8 uses at most four bytes per character. One extra character
        # detects truncation without reading the rest of a large response.
        data = os.read(descriptor, (max_chars + 1) * 4)
    finally:
        os.close(descriptor)
    text = data.decode("utf-8", errors="replace")
    complete = text if text.endswith("\n") else text + "\n"
    if len(complete) <= max_chars:
        return complete
    notice = _TRUNCATION_NOTICE
    if max_chars < len(notice):
        return notice[: max_chars - 1] + "\n"
    return text[: max_chars - len(notice)] + notice
