"""Model and dialect resolution shared by ask and map."""

import pytest

from enmaas_cli.cli import _resolve_dialect_override, _resolve_model
from enmaas_cli.config import Config


def test_explicit_model_wins() -> None:
    model, defaulted = _resolve_model("gpt-6-sol", Config())
    assert model == "gpt-6-sol"
    assert defaulted is False


def test_missing_model_uses_config_default() -> None:
    # Not the built-in default: a resolver that ignored config and
    # hardcoded GLM would pass otherwise.
    cfg = Config(default_model="claude-haiku-5-5")
    model, defaulted = _resolve_model(None, cfg)
    assert model == "claude-haiku-5-5"
    assert defaulted is True


def test_dialect_flag_wins_over_override() -> None:
    cfg = Config(dialect_overrides={"m": "anthropic"})
    assert _resolve_dialect_override("m", "openai", cfg) == "openai"


def test_dialect_override_applies_when_flag_absent() -> None:
    cfg = Config(dialect_overrides={"m": "anthropic"})
    assert _resolve_dialect_override("m", None, cfg) == "anthropic"


def test_invalid_override_is_ignored_with_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Config(dialect_overrides={"m": "cobol"})
    assert _resolve_dialect_override("m", None, cfg) is None
    assert "invalid dialect override 'cobol'" in capsys.readouterr().err
