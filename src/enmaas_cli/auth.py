"""Resolve the EnMaaS API key and keep it out of logs."""

from __future__ import annotations

import os
import stat
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


def _from_file(path: Path) -> Key:
    return Key(
        value=_read_key_file(path),
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
        return Key(value=from_env, source=ENV_VAR, warning=None)

    if config_key_file is not None:
        return _from_file(_expand_configured(config_key_file))

    default = home / DEFAULT_RELATIVE_PATH
    if not default.exists():
        raise AuthError(
            f"no EnMaaS key found: pass --key-file, set ${ENV_VAR}, or create {default}"
        )
    return _from_file(default)


def redact(text: str, key: str) -> str:
    """Replace every occurrence of the key with a placeholder."""
    if not key:
        return text
    return text.replace(key, REDACTION)
