from pathlib import Path

from enmaas_cli.config import DEFAULTS, load_config


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


def test_malformed_toml_warns_and_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("this is not = = toml\n", encoding="utf-8")
    cfg = load_config(path)
    assert any("could not be parsed" in w for w in cfg.warnings)
    assert cfg.concurrency == 4


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
