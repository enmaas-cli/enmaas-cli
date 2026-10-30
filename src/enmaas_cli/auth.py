"""Resolve the EnMaaS API key and keep it out of logs."""

from __future__ import annotations

import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REDACTION = "***REDACTED***"
ENV_VAR = "ENMAAS_KEY"
DEFAULT_RELATIVE_PATH = Path(".secrets") / "enmaas"


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


def _read_key_file(path: Path) -> str:
    if not path.exists():
        raise AuthError(f"key file does not exist: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise AuthError(f"key file is empty: {path}")
    return value


def _permission_warning(path: Path) -> str | None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{path} is mode {oct(mode & 0o777)[2:]}, readable beyond the owner; "
            f"run: chmod 600 {path}"
        )
    return None


def resolve_key(
    *,
    key_file: Path | None,
    env: Mapping[str, str],
    home: Path,
) -> Key:
    """Resolve a key from --key-file, then $ENMAAS_KEY, then ~/.secrets/enmaas."""
    if key_file is not None:
        return Key(
            value=_read_key_file(key_file),
            source=str(key_file),
            warning=_permission_warning(key_file),
        )

    from_env = env.get(ENV_VAR, "").strip()
    if from_env:
        return Key(value=from_env, source=ENV_VAR, warning=None)

    default = home / DEFAULT_RELATIVE_PATH
    if not default.exists():
        raise AuthError(
            f"no EnMaaS key found: pass --key-file, set ${ENV_VAR}, or create {default}"
        )
    return Key(
        value=_read_key_file(default),
        source=str(default),
        warning=_permission_warning(default),
    )


def redact(text: str, key: str) -> str:
    """Replace every occurrence of the key with a placeholder."""
    if not key:
        return text
    return text.replace(key, REDACTION)
