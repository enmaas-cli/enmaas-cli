"""Command regressions for synthetic secrets in schema diagnostics."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.auth import REDACTION
from enmaas_cli.cli import main


@pytest.mark.parametrize("command", ["ask", "single", "batch"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("key", ["tiny-secret", 'slash/quote"back\\key', "q" * 145 + "/end"])
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_schema_command_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    as_json: bool,
    key: str,
    required: bool,
    nested: bool,
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
    payload: dict[str, Any] = {} if required else {key: "bad"}
    if nested:
        for index in range(3):
            name = str(index) + "field" * 6
            schema = {"type": "object", "properties": {name: schema}}
            payload = {name: payload}
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    answer = json.dumps(payload)
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
    if not (nested and required):
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
            assert len(message) <= 200
            if not (nested and required):
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


def test_ask_receipt_cuts_a_long_schema_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from enmaas_cli.schema import ValidationError, validate_against_schema

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
    monkeypatch.setenv("ENMAAS_KEY", "schema-length-synthetic-key")
    monkeypatch.setenv("ENMAAS_BASE_URL", "http://127.0.0.1:1")
    # A long path and four long missing names give a diagnostic over 200.
    schema: dict[str, Any] = {"type": "object", "required": ["n" * 40 + str(i) for i in range(4)]}
    payload: dict[str, Any] = {}
    for index in range(4):
        name = "p" * 40 + str(index)
        schema = {"type": "object", "properties": {name: schema}}
        payload = {name: payload}
    answer = json.dumps(payload)
    with pytest.raises(ValidationError) as caught:
        validate_against_schema(answer, schema)
    full = str(caught.value)
    assert len(full) > 200
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": answer}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: real_client(transport=httpx.MockTransport(handler))
    )
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-m", "gpt-6-sol"]
    argv += ["--dialect", "openai", "--retries", "0", "--schema", str(schema_path)]
    argv += ["-p", "hi", "--output", str(tmp_path / "out" / "answer.md")]
    assert main(argv) == 1
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).splitlines()
    (line,) = [line for line in lines if line.startswith("  error     ")]
    message = line.removeprefix("  error     ")
    assert len(message) == 200
    assert message == full[:200]


@pytest.mark.parametrize(
    ("command", "failure"),
    [
        (command, failure)
        for command in ("ask", "single", "batch")
        for failure in ("transport", "http", "output", "refusal", "index")
        if (command, failure) != ("ask", "index")
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_actual_command_errors_are_strictly_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    failure: str,
    as_json: bool,
) -> None:
    from enmaas_cli import cli
    from enmaas_cli.auth import redact
    from enmaas_cli.budget import RollingCheck

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
    key = 'bounds/key"\\synthetic'
    monkeypatch.setenv("ENMAAS_KEY", key)
    monkeypatch.setenv("ENMAAS_BASE_URL", "http://127.0.0.1:1")
    encoded = "".join(f"\\u{ord(char):04X}" for char in key)
    original = "x" * 170 + encoded + "z" * 2000
    expected_full = "x" * 170 + REDACTION + "z" * 2000
    assert redact(original, key) == expected_full
    calls = []
    index_diagnostics = []

    def fail_write(*args: object) -> None:
        raise OSError(original)

    if failure == "output":
        monkeypatch.setattr(
            "enmaas_cli.ask.write_output" if command == "ask" else "enmaas_cli.runner.write_output",
            fail_write,
        )
    if failure == "index":
        from enmaas_cli.runner import MapOutcome, run_job

        real_run_map = run_job

        async def capture_map(*args: Any, **kwargs: Any) -> MapOutcome:
            outcome = await real_run_map(*args, **kwargs)
            index_diagnostics.append(outcome.index_error)
            return outcome

        monkeypatch.setattr(cli, "run_job", capture_map)
        monkeypatch.setattr("enmaas_cli.runner._append_index", fail_write)
    if failure == "refusal":
        monkeypatch.setattr(cli, "rolling_check", lambda *a: RollingCheck(10, 5, original))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if failure == "transport":
            raise httpx.ConnectError(original)
        if failure == "http":
            return httpx.Response(400, text=original)
        prompt = json.loads(request.content)["messages"][-1]["content"]
        ids = re.findall(r"=== ITEM (\S+) ===", prompt)
        content = "\n".join(f"=== ANSWER {item_id} ===\ndone" for item_id in ids) if ids else "done"
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
    argv = [
        "--config",
        str(tmp_path / "none.toml"),
        "ask" if command == "ask" else "map",
        "-m",
        "gpt-6-sol",
        "--dialect",
        "openai",
        "--retries",
        "0",
    ]
    out = tmp_path / "out"
    if as_json:
        argv += ["--json"]
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
            "--concurrency",
            "1",
        ]
    expected_code = (
        3 if failure == "refusal" else 1 if command == "ask" or failure == "index" else 2
    )
    assert main(argv) == expected_code
    captured = capsys.readouterr()
    if failure == "index":
        assert index_diagnostics == [expected_full]
    assert bool(calls) is (failure != "refusal")
    visible = captured.out + captured.err
    assert key not in visible
    assert encoded not in visible
    if as_json:
        result = json.loads(captured.out)
        messages = (
            [result["error"]]
            if command == "ask" or failure in ("refusal", "index")
            else [entry["error"] for entry in result["failures"]]
        )
        for message in messages:
            assert len(message) <= 200
            assert "\\u" not in message
        if failure in ("output", "index"):
            assert result["tokens"]["input"] > 0
    else:
        messages = [
            line.split("  ", 2)[-1].lstrip()
            for line in visible.splitlines()
            if line.startswith(("  error ", "  failure ", "  budget "))
        ]
        assert messages
        for message in messages:
            # A map failure line also carries its item id.
            if message.startswith(("a  ", "b  ")):
                message = message[3:]
            assert len(message) <= 200
    if command != "ask" and failure not in ("refusal", "index"):
        rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
        errors = [row["error"] for row in rows if row.get("status") == "error"]
        assert errors
        if failure == "transport":
            assert errors[0] == "transport error: ConnectError: " + expected_full
        elif failure == "output":
            assert errors[0] == expected_full
        for message in errors:
            assert len(message) > 200
            assert REDACTION in message
            assert encoded not in json.dumps(message)


@pytest.mark.parametrize("command", ["ask", "map"])
@pytest.mark.parametrize("as_json", [False, True])
def test_ledger_write_refusal_redacts_before_strict_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    as_json: bool,
) -> None:
    from enmaas_cli import cli
    from enmaas_cli.budget import RollingCheck

    key = "long-synthetic-key-" + "s" * 145
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
    state_home = tmp_path / key
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("ENMAAS_KEY", key)
    monkeypatch.setenv("ENMAAS_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(cli, "rolling_check", lambda *a: RollingCheck(0, 5, None))
    monkeypatch.setattr(cli, "can_append", lambda *a: False)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("ledger refusal must precede client creation")

    monkeypatch.setattr(cli, "make_client", forbidden)
    argv = [
        "--config",
        str(tmp_path / "none.toml"),
        command,
        "-m",
        "gpt-6-sol",
        "--dialect",
        "openai",
    ]
    if as_json:
        argv += ["--json"]
    if command == "ask":
        argv += ["-p", "hi"]
    else:
        argv += [
            "--items",
            str(tmp_path / "unused.jsonl"),
            "--template",
            str(tmp_path / "unused.txt"),
            "--outdir",
            str(tmp_path / "out"),
        ]
    assert main(argv) == 1
    captured = capsys.readouterr()
    message = (
        json.loads(captured.out)["error"]
        if as_json
        else captured.err.removeprefix("error: ").rstrip("\n")
    )
    expected_full = (
        f"the usage ledger {tmp_path}/{REDACTION}/enmaas/runs/usage.jsonl cannot be "
        "written, so the token budget cannot be tracked; fix it or remove the [budget] "
        "table from the config"
    )
    assert message == expected_full[:200]
    assert len(message) <= 200
    assert key[:32] not in message
    assert key not in captured.out + captured.err
    assert not (tmp_path / "out").exists()
