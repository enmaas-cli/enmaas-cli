"""Command regressions for synthetic secrets in schema diagnostics."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest

from enmaas_cli.auth import REDACTION
from enmaas_cli.cli import main


@pytest.mark.parametrize("command", ["ask", "single", "batch"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("key", ["tiny-secret", 'slash/quote"back\\key', "q" * 145 + "/end"])
@pytest.mark.parametrize("required", [False, True])
def test_schema_command_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    as_json: bool,
    key: str,
    required: bool,
) -> None:
    for name in os.environ:
        if name.startswith("ENMAAS_"):
            monkeypatch.delenv(name, raising=False)
    for name in (
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("ENMAAS_KEY", key)
    monkeypatch.setenv("ENMAAS_BASE_URL", "http://127.0.0.1:1")
    schema = {"type": "object", "properties": {key: {"type": "integer"}}}
    if required:
        schema["required"] = [key]
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    answer = json.dumps({} if required else {key: "bad"})
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        prompt = json.loads(request.content)["messages"][-1]["content"]
        ids = re.findall(r"=== ITEM (\S+) ===", prompt)
        content = (
            "\n".join(f"=== ANSWER {item_id} ===\n{answer}" for item_id in ids) if ids else answer
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: real_client(transport=httpx.MockTransport(handler))
    )
    argv = ["--config", str(tmp_path / "none.toml")]
    argv += [
        "ask" if command == "ask" else "map",
        "-m",
        "gpt-6-sol",
        "--dialect",
        "openai",
        "--retries",
        "0",
        "--schema",
        str(schema_path),
    ]
    if as_json:
        argv += ["--json"]
    out = tmp_path / "out"
    if command == "ask":
        argv += ["-p", "hi", "--output", str(out / "answer.md")]
    else:
        items = tmp_path / "items.jsonl"
        items.write_text('{"id":"a","x":1}\n{"id":"b","x":2}\n', encoding="utf-8")
        template = tmp_path / "template.txt"
        template.write_text("shared\n===PER ITEM===\ndo {x}", encoding="utf-8")
        argv += [
            "--items",
            str(items),
            "--template",
            str(template),
            "--outdir",
            str(out),
            "--batch",
            "2" if command == "batch" else "1",
        ]
    assert main(argv) == (1 if command == "ask" else 2)
    captured = capsys.readouterr()
    assert calls
    visible = captured.out + captured.err
    assert REDACTION in visible
    assert key[:32] not in visible
    if as_json:
        payload = json.loads(captured.out)
        assert payload["tokens"]["input"] == (10 if command == "single" else 5)
        assert payload["tokens"]["output"] == (4 if command == "single" else 2)
        errors = (
            [payload["error"]] if command == "ask" else [x["error"] for x in payload["failures"]]
        )
        for message in errors:
            assert REDACTION in message
            assert key[:32] not in message
    if command != "ask":
        rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
        errors = [row["error"] for row in rows if row.get("status") == "error"]
        assert len(errors) == 2
        for message in errors:
            assert REDACTION in message
            assert key[:32] not in message
            assert key not in json.dumps(message)
