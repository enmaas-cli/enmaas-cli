import importlib.metadata
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


def test_version_prints_package_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"enmaas {importlib.metadata.version('enmaas-cli')}\n"


def test_version_falls_back_when_package_metadata_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    # cli.package_version looks up importlib.metadata.version at call time.
    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out == "enmaas 0+unknown\n"
