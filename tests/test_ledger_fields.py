"""Ledger rows are checked field by field, so no row can break a reader."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enmaas_cli.cli import main
from enmaas_cli.usage import load_events, load_events_strict


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "command": "ask",
        "model": "m",
        "dialect": "openai",
        "input_tokens": 100,
        "cached_tokens": 0,
        "output_tokens": 10,
        "reasoning_tokens": 0,
        "elapsed": 1.0,
        "status": "ok",
        "schema_used": False,
        "error_kind": None,
        "items_total": None,
        "items_ok": None,
        "items_failed": None,
        "charge": 110,
    }
    row.update(overrides)
    return row


DEEP = '{"model": ' + "[" * 100_000 + "]" * 100_000 + "}"
BAD_ROWS = [
    json.dumps(_row(elapsed="1.0")),
    json.dumps(_row(elapsed=None)),
    # json writes these as the bare words NaN and Infinity, which it reads back.
    json.dumps(_row(elapsed=float("nan"))),
    json.dumps(_row(elapsed=float("inf"))),
    json.dumps(_row(elapsed=-1.0)),
    json.dumps(_row(model=["m"])),
    json.dumps(_row(command=None)),
    json.dumps(_row(model="m" * 1025)),
    json.dumps(_row(error_kind=7)),
    DEEP,
]


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    path = tmp_path / "state" / "enmaas" / "runs" / "usage.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join([json.dumps(_row()), *BAD_ROWS]) + "\n", encoding="utf-8")
    return path


def test_both_readers_skip_every_bad_row(ledger: Path) -> None:
    assert len(load_events(ledger)) == 1
    events, problems = load_events_strict(ledger)
    assert len(events) == 1
    assert [p.split(": ", 1)[1] for p in problems] == [
        *["bad elapsed"] * 5,
        *["bad text field"] * 4,
        "nested too deeply",
    ]


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_usage_reports_the_rows_it_skipped(
    ledger: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    argv = ["--config", str(tmp_path / "none.toml"), "usage", *(["--json"] if as_json else [])]
    assert main(argv) == 0
    captured = capsys.readouterr()
    skipped = len(BAD_ROWS)
    assert f"warning: {skipped} rows of {ledger} could not be read and were skipped" in (
        captured.err
    )
    if as_json:
        obj = json.loads(captured.out)
        assert obj["total_calls"] == 1
        assert obj["skipped_rows"] == skipped
    else:
        assert "Total calls:      1" in captured.out


def test_a_budgeted_ask_refuses_on_a_bad_row(
    ledger: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "budget.toml"
    config.write_text("[budget]\nwindow_days = 7\ntokens = 100000\n", encoding="utf-8")
    argv = ["--config", str(config), "ask", "-m", "m", "--dialect", "openai", "-p", "hi"]
    assert main(argv) == 3
    assert "could not be read: line 2: bad elapsed;" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_record_refuses_an_elapsed_that_is_not_finite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    argv = ["record", "--model", "m", "--command", "c", "--input", "1", "--output", "1"]
    with pytest.raises(SystemExit) as excinfo:
        main([*argv, "--elapsed", value])
    assert excinfo.value.code == 1
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / "enmaas" / "runs" / "usage.jsonl").exists()
