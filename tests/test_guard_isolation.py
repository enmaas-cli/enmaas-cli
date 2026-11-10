"""Every guard launcher runs Python in isolated mode, so workspace code never loads.

A host runs the guard in the assistant's working directory and passes on its
environment. Without isolated mode, a PYTHONPATH that names the working
directory lets a repository's json.py run before the guard decides anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from enmaas_cli.outputs import protect

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "planted-module-ran.txt"
PLANTED = f'open("{SENTINEL}", "w").write("ran")\nraise SystemExit(0)\n'


def _workspace(tmp_path: Path) -> tuple[Path, str]:
    """A working directory with a planted json.py and one protected answer."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "json.py").write_text(PLANTED, encoding="utf-8")
    answer = work / "answer.md"
    answer.write_text("PRIVATE RESPONSE BODY", encoding="utf-8")
    protect(answer)
    event = {"tool_name": "Read", "cwd": str(work), "tool_input": {"file_path": str(answer)}}
    return work, json.dumps(event)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    return {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "CODEX_HOME": str(home / "codex"),
        "PYTHONPATH": ".",
        **extra,
    }


def _assert_denied_without_planted_code(
    work: Path, result: subprocess.CompletedProcess[str]
) -> None:
    assert not (work / SENTINEL).exists(), "a module from the working directory ran"
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("enmaas containment: full answer")


def test_plugin_launcher_ignores_pythonpath_and_the_working_directory(tmp_path: Path) -> None:
    work, event = _workspace(tmp_path)
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for g in hooks["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert commands
    for command in commands:
        result = subprocess.run(
            ["bash", "-c", command],
            input=event,
            cwd=work,
            env=_env(tmp_path, CLAUDE_PLUGIN_ROOT=str(ROOT)),
            text=True,
            capture_output=True,
            timeout=10,
        )
        _assert_denied_without_planted_code(work, result)


@pytest.mark.skipif(os.name != "posix", reason="install.sh needs a POSIX shell")
def test_installed_launchers_ignore_pythonpath_and_the_working_directory(tmp_path: Path) -> None:
    work, event = _workspace(tmp_path)
    env = _env(tmp_path)
    Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True)
    Path(env["CODEX_HOME"]).mkdir(parents=True)
    # The installer checks its new guard copy from the directory it was run
    # in, so that check meets the planted module too.
    install = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--skill-only"],
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert not (work / SENTINEL).exists(), "the installer's guard check ran workspace code"
    assert install.returncode == 0, install.stdout + install.stderr
    settings = json.loads(Path(env["CLAUDE_CONFIG_DIR"], "settings.json").read_text("utf-8"))
    with Path(env["CODEX_HOME"], "config.toml").open("rb") as handle:
        config = tomllib.load(handle)
    commands = [
        hook["command"]
        for source in (settings, config)
        for group in source["hooks"]["PreToolUse"]
        for hook in group["hooks"]
    ]
    assert len(commands) == 2
    for command in commands:
        result = subprocess.run(
            ["bash", "-c", command],
            input=event,
            cwd=work,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        _assert_denied_without_planted_code(work, result)
