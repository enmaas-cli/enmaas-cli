from pathlib import Path

import pytest

from enmaas_cli.cli import main


def test_missing_key_exits_one_with_actionable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ENMAAS_KEY", raising=False)
    code = main(["ask", "-p", "hi"])
    captured = capsys.readouterr()
    assert code == 1
    assert "no EnMaaS key" in captured.err


def test_no_prompt_source_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    code = main(["ask"])
    captured = capsys.readouterr()
    assert code == 1
    assert "no prompt" in captured.err.lower()


def test_unknown_model_dialect_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    code = main(["ask", "-m", "llama-4", "-p", "hi"])
    captured = capsys.readouterr()
    assert code == 1
    assert "cannot determine dialect" in captured.err
