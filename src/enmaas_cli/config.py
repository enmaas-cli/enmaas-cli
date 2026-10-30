"""Optional TOML configuration. The tool is fully usable without it."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

BASE_URL = "https://GATEWAY-HOST"
DEFAULT_MODEL = "rits/zai-org/glm-5-3"


@dataclass(frozen=True)
class Config:
    base_url: str = BASE_URL
    default_model: str = DEFAULT_MODEL
    concurrency: int = 4
    # Below the measured ~300s gateway ceiling so our deadline fires first.
    timeout: int = 280
    dialect_overrides: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


DEFAULTS = Config()

_SCALARS = {"base_url": str, "default_model": str, "concurrency": int, "timeout": int}


def default_config_path() -> Path:
    return Path.home() / ".config" / "enmaas" / "config.toml"


def load_config(path: Path) -> Config:
    """Load config, degrading to defaults with a warning rather than failing."""
    if not path.exists():
        return Config()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return Config(warnings=[f"{path} could not be parsed, using defaults: {exc}"])

    config = Config()
    warnings: list[str] = []
    updates: dict[str, object] = {}

    for key, value in raw.items():
        if key == "dialect_overrides":
            if isinstance(value, dict):
                updates["dialect_overrides"] = {str(k): str(v) for k, v in value.items()}
            else:
                warnings.append("dialect_overrides must be a table, ignoring it")
        elif key in _SCALARS:
            expected = _SCALARS[key]
            # bool subclasses int, so `concurrency = true` would otherwise pass
            # the isinstance check and silently become 1.
            wrong_type = not isinstance(value, expected) or (
                expected is int and isinstance(value, bool)
            )
            if wrong_type:
                warnings.append(f"{key} must be {expected.__name__}, ignoring {value!r}")
            elif key in {"concurrency", "timeout"} and value <= 0:
                warnings.append(f"{key} must be positive, ignoring {value!r}")
            else:
                updates[key] = value
        else:
            warnings.append(f"unknown config key {key!r} in {path}, ignoring it")

    return replace(config, **updates, warnings=warnings)  # type: ignore[arg-type]
