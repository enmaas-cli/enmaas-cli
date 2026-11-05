from pathlib import Path

import pytest

from enmaas_cli.config import DEFAULTS, ConfigError, load_config


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.base_url == DEFAULTS.base_url
    assert cfg.default_model == "rits/zai-org/glm-5-3"
    assert cfg.concurrency == 4
    assert cfg.timeout == 280
    assert cfg.warnings == []


def test_values_override_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('base_url = "https://other.test"\nconcurrency = 8\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.base_url == "https://other.test"
    assert cfg.concurrency == 8
    assert cfg.timeout == 280


def test_unknown_keys_warn_but_do_not_fail(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("future_option = true\n", encoding="utf-8")
    cfg = load_config(path)
    assert any("future_option" in w for w in cfg.warnings)
    assert cfg.concurrency == 4


def test_unparseable_toml_is_fatal(tmp_path: Path) -> None:
    """Deliberate change: this used to warn and fall back to defaults.

    Falling back dropped any configured [budget] along with everything
    else, so a stray typo anywhere in the file ran uncapped. The table
    cannot be read to find out whether one was set, and an enforcement
    control that cannot be read must not be assumed absent.
    """
    path = tmp_path / "config.toml"
    path.write_text("this is not = = toml\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not be parsed"):
        load_config(path)


def test_a_syntax_error_does_not_silently_disable_a_budget(tmp_path: Path) -> None:
    """The case that motivated making this fatal."""
    path = tmp_path / "config.toml"
    path.write_text("[budget]\nwindow_days = 30\ntokens =\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_dialect_overrides_are_read(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[dialect_overrides]\n"rits/zai-org/glm-5-3" = "anthropic"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.dialect_overrides["rits/zai-org/glm-5-3"] == "anthropic"


def test_boolean_is_rejected_for_int_fields(tmp_path: Path) -> None:
    """bool subclasses int, so a bare isinstance check would accept `true` as 1."""
    path = tmp_path / "config.toml"
    path.write_text("concurrency = true\ntimeout = false\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.concurrency == 4
    assert cfg.timeout == 280
    assert any("concurrency must be int" in w for w in cfg.warnings)
    assert any("timeout must be int" in w for w in cfg.warnings)


def test_zero_concurrency_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("concurrency = 0\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.concurrency == 4
    assert any("concurrency" in w and "must be positive" in w for w in cfg.warnings)


def test_negative_concurrency_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("concurrency = -1\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.concurrency == 4
    assert any("concurrency" in w and "must be positive" in w for w in cfg.warnings)


def test_zero_timeout_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("timeout = 0\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.timeout == 280
    assert any("timeout" in w and "must be positive" in w for w in cfg.warnings)


def test_budget_table_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[budget]\nwindow_days = 30\ntokens = 50_000_000\n", encoding="utf-8")
    config = load_config(path)
    assert config.budget_window_days == 30
    assert config.budget_tokens == 50_000_000
    assert config.warnings == []


def test_budget_absent_means_no_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('base_url = "https://example.invalid"\n', encoding="utf-8")
    assert load_config(path).budget_tokens is None


def test_a_half_written_budget_table_is_fatal(tmp_path: Path) -> None:
    """Every other config key degrades to a default. This one must not.

    A half-written budget is a request for enforcement that cannot be
    honoured, and warning while permitting unlimited work is the same
    false assurance as a flag that silently does nothing.
    """
    path = tmp_path / "config.toml"
    path.write_text("[budget]\ntokens = 1000\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="window_days"):
        load_config(path)


def test_a_nonpositive_budget_value_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[budget]\nwindow_days = 0\ntokens = 1000\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="positive"):
        load_config(path)


def test_a_non_integer_budget_value_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[budget]\nwindow_days = 30\ntokens = "lots"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="integer"):
        load_config(path)


def test_an_unknown_budget_key_is_fatal(tmp_path: Path) -> None:
    """A typo in a control must not silently leave it half-configured."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[budget]\nwindow_days = 30\ntokens = 1000\nwindow_dayz = 7\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="window_dayz"):
        load_config(path)
