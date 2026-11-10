"""An ask that fails after the reply arrived still records the charge.

The gateway billed the request once the reply came back. A failure in the
schema check or the file write after that must not lose the ledger event.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.ask import run_ask
from enmaas_cli.cli import main

REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": '{"a": 1}'}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=REPLY)


def _ask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(_handler)),
    )
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "-p", "hi", "--retries", "0", *extra]
    return main(argv)


def _ledger_rows(tmp_path: Path) -> list[dict[str, Any]]:
    ledger = tmp_path / "state" / "enmaas" / "runs" / "usage.jsonl"
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]


def _schema(tmp_path: Path) -> Path:
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    return path


def test_a_write_that_fails_keeps_the_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def write(path: Path, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("enmaas_cli.ask.write_output", write)
    assert _ask(tmp_path, monkeypatch) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    assert "  error     disk full" in captured.err
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error_kind"] == "OSError"
    assert rows[0]["charge"] == 7


def test_a_write_that_fails_after_a_schema_failure_keeps_the_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def write(path: Path, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("enmaas_cli.ask.write_output", write)
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "array"}), encoding="utf-8")
    assert _ask(tmp_path, monkeypatch, "--schema", str(schema)) == 1
    capsys.readouterr()
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["error_kind"] == "OSError"
    assert rows[0]["charge"] == 7


@pytest.mark.parametrize("as_json", [False, True])
def test_an_unexpected_error_keeps_the_charge_and_never_its_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    def validate(text: str, schema: dict[str, Any]) -> None:
        raise RuntimeError("secret text")

    monkeypatch.setattr("enmaas_cli.ask.validate_against_schema", validate)
    extra = ["--schema", str(_schema(tmp_path))] + (["--json"] if as_json else [])
    assert _ask(tmp_path, monkeypatch, *extra) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "internal error (RuntimeError at" in output
    assert "secret text" not in output
    assert "Traceback" not in output
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["error_kind"] == "RuntimeError"
    assert rows[0]["charge"] == 7


async def test_a_cancel_after_the_reply_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write(path: Path, text: str) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("enmaas_cli.ask.write_output", write)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await run_ask(
                client=client,
                base_url="https://g.test",
                key="sk-oai-x",
                model="rits/zai-org/glm-5-3",
                prompt="hi",
                system=None,
                dialect="openai",
                thinking=False,
                max_tokens=None,
                timeout=5,
                retries=0,
                output=None,
                run_root=tmp_path,
                save_thinking=False,
            )
