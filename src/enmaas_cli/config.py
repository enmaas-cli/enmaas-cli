"""TOML configuration.

Every key is optional except the gateway URL, and that one can come from
$ENMAAS_BASE_URL instead. Commands that never call the gateway need no
configuration at all.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from enmaas_cli.pricing import ModelPrice

DEFAULT_MODEL = "rits/zai-org/glm-5-3"


class ConfigError(Exception):
    """A configured control that cannot be honoured as written.

    Raised only for the [budget] table. Every other key degrades to its
    default with a warning, because the others are preferences. A budget
    is a control: silently ignoring a malformed one would permit the
    unlimited spend the user was trying to prevent.
    """


@dataclass(frozen=True)
class Config:
    # No default: the gateway address belongs to whoever runs the gateway,
    # so it comes from this file or from $ENMAAS_BASE_URL.
    base_url: str | None = None
    # A key file named here is read after $ENMAAS_KEY and before
    # ~/.secrets/enmaas.
    key_file: str | None = None
    default_model: str = DEFAULT_MODEL
    concurrency: int = 4
    # Below the measured ~300s gateway ceiling so our deadline fires first.
    timeout: int = 280
    dialect_overrides: dict[str, str] = field(default_factory=dict)
    # USD per million tokens, keyed by model id or by a prefix covering a
    # family. The gateway publishes no rates, so these can only come from
    # whoever knows what their account is charged.
    prices: dict[str, ModelPrice] = field(default_factory=dict)
    # A standing spend policy. Both keys or neither: half a budget is not
    # a budget, and guessing the missing half would enforce something
    # nobody asked for.
    budget_window_days: int | None = None
    budget_tokens: int | None = None
    warnings: list[str] = field(default_factory=list)


DEFAULTS = Config()

_SCALARS = {
    "base_url": str,
    "key_file": str,
    "default_model": str,
    "concurrency": int,
    "timeout": int,
}


def default_config_path() -> Path:
    return Path.home() / ".config" / "enmaas" / "config.toml"


def load_config(path: Path) -> Config:
    """Load config, degrading to defaults with a warning rather than failing.

    One exception: a file that exists but cannot be parsed at all is
    fatal. Degrading to defaults there would silently drop a configured
    [budget] along with everything else, so a stray typo anywhere in the
    file would run uncapped. We cannot read the table to know whether one
    was set, and an enforcement control we cannot read must not be
    assumed absent.
    """
    if not path.exists():
        return Config()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"{path} could not be parsed: {exc}") from exc

    config = Config()
    warnings: list[str] = []
    updates: dict[str, object] = {}

    for key, value in raw.items():
        if key == "prices":
            parsed, price_warnings = _parse_prices(value)
            warnings.extend(price_warnings)
            if parsed:
                updates["prices"] = parsed
        elif key == "budget":
            updates.update(_parse_budget(value))
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
                    f"price for {model!r} needs a numeric {field_name}, ignoring this entry"
                )
                bad = True
                break
            if raw < 0:
                warnings.append(
                    f"price for {model!r} has a negative {field_name}, ignoring this entry"
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


def _parse_budget(value: object) -> dict[str, int]:
    """Read the [budget] table, raising rather than warning.

    The opposite failure posture to _parse_prices above, deliberately. A
    price that cannot be read costs you a number in a report. A budget
    that cannot be read costs you the ceiling itself, and a warning
    nobody reads is indistinguishable from no budget at all.
    """
    if not isinstance(value, dict):
        raise ConfigError("[budget] must be a table")

    unknown = sorted(set(value) - {"window_days", "tokens"})
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [budget]: {', '.join(unknown)}. "
            f"A typo here would leave the ceiling unset."
        )

    parsed: dict[str, int] = {}
    for name in ("window_days", "tokens"):
        raw = value.get(name)
        if raw is None:
            raise ConfigError(f"[budget] needs both window_days and tokens; {name} is missing")
        # bool subclasses int, so `tokens = true` would otherwise pass.
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ConfigError(f"[budget] {name} must be an integer, got {raw!r}")
        if raw <= 0:
            raise ConfigError(f"[budget] {name} must be positive, got {raw!r}")
        parsed[name] = raw

    return {
        "budget_window_days": parsed["window_days"],
        "budget_tokens": parsed["tokens"],
    }
