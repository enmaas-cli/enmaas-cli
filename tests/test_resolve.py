"""Model and dialect resolution shared by ask and map."""

from pathlib import Path

import pytest

from enmaas_cli.cli import _resolve_dialect_override, _resolve_model, main
from enmaas_cli.config import Config, load_config


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


def test_invalid_override_is_ignored_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[dialect_overrides]\nm = "cobol"\n', encoding="utf-8")
    cfg = load_config(path)
    assert _resolve_dialect_override("m", None, cfg) is None
    assert cfg.warnings == ["invalid dialect override 'cobol' for m, ignoring"]


def test_invalid_override_is_reported_when_the_config_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The warning comes before the gateway URL is resolved, so it is printed
    # even though the command stops there and never resolves a dialect.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ENMAAS_BASE_URL", raising=False)
    path = tmp_path / "config.toml"
    path.write_text('[dialect_overrides]\nm = "cobol"\n', encoding="utf-8")
    assert main(["--config", str(path), "ask", "-m", "m", "--dialect", "openai", "-p", "hi"]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "warning: invalid dialect override 'cobol' for m, ignoring"
