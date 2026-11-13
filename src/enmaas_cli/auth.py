"""Resolve the EnMaaS API key and keep it out of logs."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REDACTION = "***REDACTED***"
ENV_VAR = "ENMAAS_KEY"
DEFAULT_RELATIVE_PATH = Path(".secrets") / "enmaas"
MAX_KEY_FILE_BYTES = 4096


class AuthError(Exception):
    """The key could not be resolved."""


@dataclass(frozen=True)
class Key:
    """A resolved API key plus where it came from.

    ``__repr__`` is overridden so the value never reaches a traceback.
    """

    value: str
    source: str
    warning: str | None

    def __repr__(self) -> str:
        return f"Key(source={self.source!r}, warning={self.warning!r})"


def _unreadable(exc: OSError, path: Path) -> AuthError:
    return AuthError(f"key file is not readable ({exc.strerror or exc}): {path}")


def _read_bounded(descriptor: int) -> bytes:
    data = b""
    while len(data) <= MAX_KEY_FILE_BYTES:
        chunk = os.read(descriptor, MAX_KEY_FILE_BYTES + 1 - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _read_key_file(path: Path) -> str:
    # A directory, a file without read permission, or binary content would
    # otherwise escape as a traceback, because callers only catch AuthError.
    # A named pipe or a device would block the read forever, and a large
    # file would be read whole into memory, so only a small regular file
    # is accepted. The nonblocking open returns at once even for a pipe.
    try:
        if not path.exists():
            raise AuthError(f"key file does not exist: {path}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            mode = os.fstat(descriptor).st_mode
            if stat.S_ISDIR(mode):
                raise AuthError(f"key file is a directory: {path}")
            if not stat.S_ISREG(mode):
                raise AuthError(f"key file is not a regular file: {path}")
            raw = _read_bounded(descriptor)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_KEY_FILE_BYTES:
            raise AuthError(f"key file is too large to be a key: {path}")
        value = raw.decode("utf-8").strip()
    except IsADirectoryError:
        raise AuthError(f"key file is a directory: {path}") from None
    except UnicodeDecodeError:
        raise AuthError(f"key file is not UTF-8 text: {path}") from None
    except OSError as exc:
        raise _unreadable(exc, path) from None
    if not value:
        raise AuthError(f"key file is empty: {path}")
    return value


def _permission_warning(path: Path) -> str | None:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        # The file can vanish or lose permissions between the read and this call.
        raise _unreadable(exc, path) from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{path} is mode {oct(mode & 0o777)[2:]}, readable beyond the owner; "
            f"run: chmod 600 {path}"
        )
    return None


def _require_sendable(value: str, source: str) -> str:
    """Return the key when it can be sent in a header, or raise AuthError.

    A key goes in an HTTP header, which takes ASCII only. A key with any
    other character, whitespace or a control character raised inside every
    request instead of once here. The message never holds the key or the
    character, because either is part of the secret.
    """
    if value.isascii() and value.isprintable() and not any(ch.isspace() for ch in value):
        return value
    raise AuthError(f"the key from {source} holds a character that cannot be sent in a header")


def _from_file(path: Path) -> Key:
    return Key(
        value=_require_sendable(_read_key_file(path), str(path)),
        source=str(path),
        warning=_permission_warning(path),
    )


def _expand_configured(value: str | Path) -> Path:
    # A "~user" path for a user that does not exist raises RuntimeError.
    # Expanding only here keeps that failure from blocking --key-file or
    # $ENMAAS_KEY, which win over the config.
    try:
        return Path(value).expanduser()
    except RuntimeError:
        raise AuthError(f"key_file in config cannot be expanded: {value}") from None


def resolve_key(
    *,
    key_file: Path | None,
    env: Mapping[str, str],
    home: Path,
    config_key_file: str | Path | None = None,
) -> Key:
    """Resolve a key from the first source that is set.

    The order is --key-file, then $ENMAAS_KEY, then key_file from the
    config file, then ~/.secrets/enmaas. A configured file that is missing
    or cannot be read is an error. Falling through to the default path
    would quietly use a different key from the one the config names. The
    configured path has "~" expanded only when that source is the one used.
    """
    if key_file is not None:
        return _from_file(key_file)

    from_env = env.get(ENV_VAR, "").strip()
    if from_env:
        return Key(value=_require_sendable(from_env, ENV_VAR), source=ENV_VAR, warning=None)

    if config_key_file is not None:
        return _from_file(_expand_configured(config_key_file))

    default = home / DEFAULT_RELATIVE_PATH
    if not default.exists():
        raise AuthError(
            f"no EnMaaS key found: pass --key-file, set ${ENV_VAR}, or create {default}"
        )
    return _from_file(default)


REDACTION_OMITTED = "[diagnostic omitted: safe redaction work limit]"
MAX_REDACTION_WORK = 10_000_000


class _RedactionLimit(Exception):
    """The deterministic safe-redaction work allowance was exhausted."""


class _RedactionWork:
    def __init__(self, key_chars: int) -> None:
        self.remaining = MAX_REDACTION_WORK
        self.digits = 1 + key_chars // sys.int_info.bits_per_digit

    def charge(self, units: int) -> None:
        self.remaining -= units
        if self.remaining < 0:
            raise _RedactionLimit

    def step(self, operations: int) -> None:
        self.charge(operations * self.digits)


def _key_masks(key: str, work: _RedactionWork) -> dict[str, int]:
    masks: dict[str, int] = {}
    for index, char in enumerate(key):
        work.charge(3 * (1 + index // sys.int_info.bits_per_digit))
        masks[char] = masks.get(char, 0) | (1 << index)
    return masks


def _forward(
    text: str,
    masks: dict[str, int],
    start: int,
    end: int,
    pending: list[int],
    work: _RedactionWork,
    *,
    keep: bool = False,
) -> tuple[list[int], list[int]]:
    work.step(7)
    pending = pending.copy()
    prefixes = []
    for pos in range(start, end):
        work.step(10)
        active = pending[pos % 7] | 1
        pending[pos % 7] = 0
        if keep:
            prefixes.append(active)
        for char, width in _json_edges(text, pos):
            pending[(pos + width) % 7] |= (active & masks.get(char, 0)) << 1
    return prefixes, pending


class _SecretMatches:
    """Find complete paths through raw and JSON-escape character edges."""

    def __init__(self, text: str, key: str, work: _RedactionWork) -> None:
        groups, tail = divmod(len(key), sys.int_info.bits_per_digit)
        mask_work = 3 * (
            sys.int_info.bits_per_digit * groups * (groups + 1) // 2 + tail * (groups + 1)
        )
        # Reject impossible work before allocating masks or text-sized arrays.
        if mask_work + 2 * len(text) + 10 * work.digits > work.remaining:
            raise _RedactionLimit
        self.text = text
        self.work = work
        self.masks = _key_masks(key, work)
        work.step(10)
        work.charge(2 * len(text))
        self.complete = 1 << len(key)
        self.inner = self.complete - 2
        self.backward = [self.complete] * 7
        self.covered = bytearray(len(text))
        self.joined = bytearray(len(text))
        # Keep leaf prefix bitsets within about four MiB, excluding overhead.
        self.block = max(1, min(1024, (4 * 1024 * 1024 * 8) // len(key)))

    def scan(self, start: int, end: int, pending: list[int]) -> None:
        self.work.step(7)
        if end - start > self.block:
            middle = (start + end) // 2
            _, middle_pending = _forward(self.text, self.masks, start, middle, pending, self.work)
            self.scan(middle, end, middle_pending)
            self.scan(start, middle, pending)
            return
        prefixes, _ = _forward(self.text, self.masks, start, end, pending, self.work, keep=True)
        for pos in range(end - 1, start - 1, -1):
            self.work.step(20)
            active = self.complete
            prefix = prefixes[pos - start]
            for char, width in _json_edges(self.text, pos):
                suffix = (self.backward[(pos + width) % 7] >> 1) & self.masks.get(char, 0)
                active |= suffix
                if prefix & suffix:
                    self.covered[pos : pos + width] = b"1" * width
                    self.joined[pos + 1 : pos + width] = b"1" * (width - 1)
            self.backward[pos % 7] = active
            if prefix & active & self.inner:
                self.joined[pos] = 1

    def replace(self) -> str:
        self.work.charge((3 + len(REDACTION)) * len(self.text))
        pieces: list[str] = []
        pos = 0
        previous = 0
        while pos < len(self.text):
            if not self.covered[pos]:
                pos += 1
                continue
            pieces.extend((self.text[previous:pos], REDACTION))
            pos += 1
            while pos < len(self.text) and self.covered[pos] and self.joined[pos]:
                pos += 1
            previous = pos
        pieces.append(self.text[previous:])
        return "".join(pieces)


def redact(text: str, key: str) -> str:
    """Remove raw and equivalent single-layer JSON spellings of a key.

    Forward and backward integer bitsets identify complete match paths.
    Recursive replay bounds memory without restricting accepted keys.
    With n text characters, k key characters and b leaf positions, time is
    O(n * ceil(k / word_bits) * (1 + log(n / b))); auxiliary space is
    O(n + (b + log(n) + alphabet_size) * ceil(k / word_bits)).
    The leaf prefix payload is limited to about four MiB. Match paths can
    overlap; adjacent independent occurrences retain separate placeholders.
    Every bigint step and allocation is charged before work begins. When
    the fixed allowance is exhausted, omit the entire diagnostic. Never
    return unchecked text or a partially processed diagnostic.
    """
    if not key or len(key) > len(text):
        return text
    work = _RedactionWork(len(key))
    try:
        work.charge(len(text) + len(key))
        if "\\" not in text:
            work.charge(len(text) + len(text) * len(REDACTION) // len(key))
            return text.replace(key, REDACTION)
        matches = _SecretMatches(text, key, work)
        matches.scan(0, len(text), [0] * 7)
        return matches.replace()
    except _RedactionLimit:
        return REDACTION_OMITTED


def _json_edges(text: str, pos: int) -> list[tuple[str, int]]:
    """Read one raw character and, when present, one JSON escape."""
    char = text[pos]
    escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    edges = [(char, 1)]
    if char == "\\" and pos + 1 < len(text):
        following = text[pos + 1]
        if following in escapes:
            edges.append((escapes[following], 2))
        elif following == "u" and pos + 6 <= len(text):
            digits = text[pos + 2 : pos + 6]
            if all(c in "0123456789abcdefABCDEF" for c in digits):
                edges.append((chr(int(digits, 16)), 6))
    return edges


def cut_redacted(text: str, limit: int) -> str:
    """Cut already-redacted text to `limit` characters without splitting the placeholder.

    A placeholder that straddles the limit is kept whole, so a reader can
    still see that a key was removed. Redact before calling this: a cut made
    first can leave part of a key that redaction no longer matches.
    """
    cut = limit
    start = text.find(REDACTION, max(0, cut - len(REDACTION) + 1))
    if 0 <= start < cut:
        cut = start + len(REDACTION)
    return text[:cut]


def error_text(message: str, key: str = "", limit: int = 200, *, terminal: bool = True) -> str:
    """Redact a complete diagnostic, format it safely, then apply a strict cap.

    JSON callers keep controls because JSON encoding makes them safe.
    """
    safe = redact(message, key)
    if terminal:
        safe = printable(safe)
    cut = limit
    start = safe.find(REDACTION, max(0, limit - len(REDACTION) + 1))
    if 0 <= start < limit and start + len(REDACTION) > limit:
        cut = start
    return safe[:cut]


_LINE_BREAKS = frozenset("\n\r\t")


def printable(text: str) -> str:
    """Make text safe to print on one terminal line.

    Text from the gateway can hold a newline that forges a receipt line, or
    an escape sequence that the terminal acts on. Every character that
    `str.isprintable()` rejects is replaced: a newline, carriage return or
    tab becomes a space, and any other becomes "?". Printable characters,
    including non-ASCII letters, pass through unchanged.
    """
    if text.isprintable():
        return text
    return "".join(ch if ch.isprintable() else (" " if ch in _LINE_BREAKS else "?") for ch in text)


_KEPT_IN_BLOCKS = frozenset("\n\t")


def printable_block(text: str) -> str:
    """Make text that spans lines safe to print.

    Like printable, but a newline and a tab are kept, for output that is
    meant to keep its lines, such as `inspect`. A carriage return becomes a
    space, because it can move the cursor back over a line already shown.
    Every other character that `str.isprintable()` rejects becomes "?".
    """
    return "".join(
        ch if ch.isprintable() or ch in _KEPT_IN_BLOCKS else (" " if ch in _LINE_BREAKS else "?")
        for ch in text
    )
