"""An installer dry run prints the files it would change and what it would do to them.

It never prints what a host settings file already holds. Those files can hold
tokens and provider headers, and a dry run is often pasted into a log.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_PLANTED = "PLANTED_CLAUDE_VALUE"
CODEX_PLANTED = "PLANTED_CODEX_VALUE"
ENMAAS_PLANTED = "PLANTED_ENMAAS_VALUE"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="install.sh needs a POSIX shell")


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / "data"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _install(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "install.sh"), *args],
        cwd=home,
        env=_env(home),
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=60,
    )


def _seed(home: Path, settings_text: str, config_text: str) -> list[Path]:
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    settings = home / ".claude" / "settings.json"
    config = home / ".codex" / "config.toml"
    settings.write_text(settings_text, encoding="utf-8")
    config.write_text(config_text, encoding="utf-8")
    return [settings, config]


def _planted_settings() -> str:
    return json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": CLAUDE_PLANTED}, "model": "opus"})


def _planted_config() -> str:
    return f'[model_providers.fixture.http_headers]\nX-Token = "{CODEX_PLANTED}"\n'


def _no_planted_value(result: subprocess.CompletedProcess[str]) -> None:
    for planted in (CLAUDE_PLANTED, CODEX_PLANTED, ENMAAS_PLANTED):
        assert planted not in result.stdout
        assert planted not in result.stderr


def test_install_dry_run_prints_no_host_settings(tmp_path: Path) -> None:
    files = _seed(tmp_path, _planted_settings(), _planted_config())
    before = [path.read_bytes() for path in files]
    result = _install(tmp_path, "--skill-only", "--dry-run")
    assert result.returncode == 0, result.stderr
    _no_planted_value(result)
    assert before == [path.read_bytes() for path in files]
    for path in files:
        assert f"{path}: would add the enmaas guard registration" in result.stdout


def test_update_and_uninstall_dry_runs_print_no_host_settings(tmp_path: Path) -> None:
    files = _seed(tmp_path, _planted_settings(), _planted_config())
    assert _install(tmp_path, "--skill-only").returncode == 0
    # A registration from an earlier version is replaced on the next install.
    for path in files:
        text = path.read_text(encoding="utf-8").replace("python3 -I -S ", "python3 -S ")
        path.write_text(text, encoding="utf-8")
    before = [path.read_bytes() for path in files]
    update = _install(tmp_path, "--skill-only", "--dry-run")
    assert update.returncode == 0, update.stderr
    _no_planted_value(update)
    for path in files:
        assert f"{path}: would replace the enmaas guard registration" in update.stdout
    remove = _install(tmp_path, "--skill-only", "--dry-run", "--uninstall")
    assert remove.returncode == 0, remove.stderr
    _no_planted_value(remove)
    for path in files:
        assert f"{path}: would remove the enmaas guard registration" in remove.stdout
    assert before == [path.read_bytes() for path in files]


def test_settings_dry_run_names_the_setting_and_not_the_file(tmp_path: Path) -> None:
    _seed(tmp_path, "{}", "")
    config = tmp_path / ".config" / "enmaas" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f"# {ENMAAS_PLANTED}\n[budget]\ntokens = 1\n", encoding="utf-8")
    before = config.read_bytes()
    result = _install(
        tmp_path, "--skill-only", "--no-hook", "--dry-run", "--gateway", "https://gw.example.test"
    )
    assert result.returncode == 0, result.stderr
    _no_planted_value(result)
    assert f"{config}: would set base_url" in result.stdout
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "settings_text,config_text",
    [
        ('{"env": {"TOKEN": "' + CLAUDE_PLANTED + '"', ""),
        ('{"' + CLAUDE_PLANTED + '": 1, "' + CLAUDE_PLANTED + '": 2}', ""),
        ("{}", f'X-Token = "{CODEX_PLANTED}"\n{CODEX_PLANTED} = '),
        ("{}", f"{CODEX_PLANTED} = 1\n{CODEX_PLANTED} = 2\n"),
    ],
    ids=["json-unclosed", "json-duplicate-key", "toml-unfinished", "toml-duplicate-key"],
)
def test_a_file_that_does_not_parse_is_named_without_its_content(
    tmp_path: Path, settings_text: str, config_text: str
) -> None:
    files = _seed(tmp_path, settings_text, config_text)
    before = [path.read_bytes() for path in files]
    result = _install(tmp_path, "--skill-only", "--dry-run")
    assert result.returncode == 1
    _no_planted_value(result)
    assert "is not valid" in result.stderr
    # Uninstall reads config.toml only when it names the guard, so only a
    # broken settings.json stops it. Neither prints the file.
    _no_planted_value(_install(tmp_path, "--skill-only", "--dry-run", "--uninstall"))
    assert before == [path.read_bytes() for path in files]
