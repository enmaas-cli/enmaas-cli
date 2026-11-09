"""The ledger records the class of the failure, never a guess from its text."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.cli import main


def _ledger_rows(tmp_path: Path) -> list[dict[str, Any]]:
    ledger = tmp_path / "state" / "enmaas" / "runs" / "usage.jsonl"
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]


def _ask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *extra: str,
) -> int:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "-p", "hi", "--retries", "0", *extra]
    return main(argv)


def test_gateway_400_mentioning_validation_is_recorded_as_gateway_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="request validation failed: bad field")

    assert _ask(tmp_path, monkeypatch, handler) == 1
    capsys.readouterr()
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["error_kind"] == "GatewayError"


def test_bad_reply_is_recorded_as_bad_reply_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>gateway page</html>", headers={"content-type": "text/html"}
        )

    assert _ask(tmp_path, monkeypatch, handler) == 1
    capsys.readouterr()
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["error_kind"] == "BadReplyError"


def test_schema_failure_is_recorded_as_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "[1, 2]"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    assert _ask(tmp_path, monkeypatch, handler, "--schema", str(schema)) == 1
    capsys.readouterr()
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["error_kind"] == "ValidationError"
    assert rows[0]["charge"] == 7


def test_success_records_no_error_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    assert _ask(tmp_path, monkeypatch, handler) == 0
    capsys.readouterr()
    assert _ledger_rows(tmp_path)[0]["error_kind"] is None
