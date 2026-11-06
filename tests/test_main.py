from pathlib import Path

import pytest

from enmaas_cli.cli import build_parser, main


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


def test_map_model_is_optional() -> None:
    args = build_parser().parse_args(
        ["map", "--items", "i.jsonl", "--template", "t.txt", "--outdir", "out"]
    )
    assert args.model is None


def test_batch_default_is_unset_until_the_template_is_read() -> None:
    args = build_parser().parse_args(
        ["map", "--items", "i.jsonl", "--template", "t.txt", "--outdir", "out"]
    )
    assert args.batch is None
