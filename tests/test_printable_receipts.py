"""Gateway text in a printed receipt cannot forge a line or reach the terminal.

A gateway error body is not trusted. The receipt prints it on one line with
control characters replaced. The index rows and the --json output keep the
message as it was, because JSON encoding already escapes those characters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.cli import main

FORGED = "bad\n  failure   z  forged line\x1b]0;title\x07"
CONTROL = ("\x1b", "\x07", "\n", "\r")


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )


def _map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    _setup(tmp_path, monkeypatch, FORGED)
    items = tmp_path / "items.jsonl"
    items.write_text(json.dumps({"id": "a", "x": "a"}) + "\n", encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    argv = ["--config", str(tmp_path / "none.toml"), "map", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "--items", str(items), "--template", str(template)]
    argv += ["--outdir", str(tmp_path / "out"), "--retries", "0", "--concurrency", "1", *extra]
    return main(argv)


def _ask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, *extra: str) -> int:
    _setup(tmp_path, monkeypatch, body)
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "-p", "hi", "--retries", "0", *extra]
    return main(argv)


def _index_rows(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_map_receipt_prints_a_forged_failure_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _map(tmp_path, monkeypatch) == 2
    out = capsys.readouterr().out
    failures = [line for line in out.split("\n") if line.startswith("  failure")]
    assert len(failures) == 1
    assert not any(ch in failures[0] for ch in CONTROL)
    assert "\x1b" not in out
    assert "\x07" not in out


def test_map_index_and_json_keep_the_raw_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _map(tmp_path, monkeypatch, "--json") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failures"] == [{"id": "a", "error": f"400: {FORGED}"}]
    (row,) = [r for r in _index_rows(tmp_path) if r.get("id") == "a"]
    assert row["error"] == f"400: {FORGED}"


def test_ask_receipt_prints_a_forged_error_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(tmp_path, monkeypatch, FORGED) == 1
    err = capsys.readouterr().err
    lines = err.rstrip("\n").split("\n")
    # The plain failure receipt: header, model line, error line.
    assert len(lines) == 3
    assert lines[2].startswith("  error     400: bad ")
    for line in lines:
        assert not any(ch in line for ch in CONTROL)


def test_ask_json_keeps_the_raw_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(tmp_path, monkeypatch, FORGED, "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == f"400: {FORGED}"
