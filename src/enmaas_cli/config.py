"""Optional TOML configuration. The tool is fully usable without it."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from enmaas_cli.pricing import ModelPrice

BASE_URL = "https://api.enmaas.devshift.net"
DEFAULT_MODEL = "rits/zai-org/glm-5-3"


@dataclass(frozen=True)
class Config:
    base_url: str = BASE_URL
    default_model: str = DEFAULT_MODEL
    concurrency: int = 4
    # Below the measured ~300s gateway ceiling so our deadline fires first.
    timeout: int = 280
    dialect_overrides: dict[str, str] = field(default_factory=dict)
    # USD per million tokens, keyed by model id or by a prefix covering a
    # family. The gateway publishes no rates, so these can only come from
    # whoever knows what their account is charged.
    prices: dict[str, ModelPrice] = field(default_factory=dict)
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
        if key == "prices":
            parsed, price_warnings = _parse_prices(value)
            warnings.extend(price_warnings)
            if parsed:
                updates["prices"] = parsed
        elif key == "dialect_overrides":
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


def _parse_prices(value: object) -> tuple[dict[str, ModelPrice], list[str]]:
    """Read the [prices] table, warning rather than dropping silently.

    A price that vanishes without a word makes a model look free, which is
    the same silent understatement the ledger already had to be fixed for.
    """
    warnings: list[str] = []
    if not isinstance(value, dict):
        return {}, ["prices must be a table, ignoring it"]

    parsed: dict[str, ModelPrice] = {}
    for model, entry in value.items():
        if not isinstance(entry, dict):
            warnings.append(f"price for {model!r} must be a table, ignoring it")
            continue
        rates: dict[str, float] = {}
        bad = False
        for field_name in ("input", "output"):
            raw = entry.get(field_name)
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                warnings.append(
                    f"price for {model!r} needs a numeric {field_name}, "
                    f"ignoring this entry"
                )
                bad = True
                break
            if raw < 0:
                warnings.append(
                    f"price for {model!r} has a negative {field_name}, "
                    f"ignoring this entry"
                )
                bad = True
                break
            rates[field_name] = float(raw)
        if bad:
            continue
        parsed[str(model)] = ModelPrice(
            input_per_mtok=rates["input"],
            output_per_mtok=rates["output"],
            label=f"configured rate for {model}",
        )
    return parsed, warnings
