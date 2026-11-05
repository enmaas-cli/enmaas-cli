"""Local output protection and bounded inspection using the standard library."""

from __future__ import annotations

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


def protect(path: Path) -> None:
    """Atomically mark one resolved output path as protected."""
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
