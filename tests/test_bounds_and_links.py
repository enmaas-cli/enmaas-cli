"""Gateway and file inputs that try to grow messages or redirect writes."""

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import cli
from enmaas_cli.auth import AuthError, resolve_key
from enmaas_cli.client import MAX_RETRY_AFTER_SECONDS, GatewayError, call
from enmaas_cli.dialect import EmptyResponseError, Request, parse_response
from enmaas_cli.outputs import is_protected, marker_path, protect, write_output
from enmaas_cli.receipts import prune_runs
from enmaas_cli.runner import run_map
from enmaas_cli.schema import ValidationError, load_schema

LINK_REFUSAL = "refusing to write through a symbolic link"
REQ = Request(path="/v1/chat/completions", headers={}, body={"model": "m"})


async def _no_sleep(seconds: float) -> None:
    return None


# Schema load diagnostics


def test_schema_error_with_a_huge_type_name_is_bounded(tmp_path: Path) -> None:
    long_name = "T" * 6000
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": long_name}), encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        load_schema(schema)
    message = str(caught.value)
    assert len(message) < 400
    assert long_name not in message
    assert "T" * 300 not in message
    assert message.startswith(f"schema file is not a valid JSON Schema: {schema} (")


def test_schema_decode_error_is_bounded(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text('{"type": "object", ' + '"' + "q" * 6000, encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        load_schema(schema)
    message = str(caught.value)
    assert len(message) < 400
    assert "q" * 100 not in message
    assert message.startswith(f"schema file contains invalid JSON: {schema} (")


# Usage counts from the gateway


def _empty_openai(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": usage,
    }


def test_a_huge_string_count_does_not_reach_the_empty_response_message() -> None:
    data = _empty_openai({"prompt_tokens": 3, "completion_tokens": "Z" * 9000})
    with pytest.raises(EmptyResponseError) as caught:
        parse_response(data, "openai")
    assert len(str(caught.value)) < 300
    assert "ZZZ" not in str(caught.value)
    assert caught.value.usage.output_tokens is None
    assert caught.value.usage.input_tokens == 3


def test_a_string_prompt_count_is_dropped() -> None:
    data = {
        "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
        "usage": {"prompt_tokens": "abc", "completion_tokens": 2},
    }
    usage = parse_response(data, "openai").usage
    assert usage.input_tokens is None
    assert usage.output_tokens == 2


@pytest.mark.parametrize("bad", ["7", -1, True, 1.5, None, [1], {"n": 1}])
def test_every_openai_usage_field_rejects_non_counts(bad: object) -> None:
    data = {
        "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
        "usage": {
            "prompt_tokens": bad,
            "completion_tokens": bad,
            "prompt_tokens_details": {"cached_tokens": bad},
            "completion_tokens_details": {"reasoning_tokens": bad},
        },
    }
    usage = parse_response(data, "openai").usage
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cached_tokens is None
    assert usage.reasoning_tokens is None


@pytest.mark.parametrize("bad", ["7", -1, True, 1.5, None])
def test_every_anthropic_usage_field_rejects_non_counts(bad: object) -> None:
    data = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {
            "input_tokens": bad,
            "output_tokens": bad,
            "cache_read_input_tokens": bad,
            "cache_creation_input_tokens": bad,
        },
    }
    usage = parse_response(data, "anthropic").usage
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cached_tokens is None


def test_anthropic_sums_only_valid_counts() -> None:
    data = {
        "stop_reason": "max_tokens",
        "content": [],
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": "Z" * 9000,
            "cache_creation_input_tokens": 5,
            "output_tokens": "Z" * 9000,
        },
    }
    with pytest.raises(EmptyResponseError) as caught:
        parse_response(data, "anthropic")
    assert len(str(caught.value)) < 300
    assert caught.value.usage.input_tokens == 15
    assert caught.value.usage.cached_tokens is None
    assert caught.value.usage.output_tokens is None


def test_non_object_usage_is_treated_as_missing() -> None:
    data = {
        "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
        "usage": "lots",
    }
    usage = parse_response(data, "openai").usage
    assert usage.input_tokens is None


def _ask_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENMAAS_KEY", "local-fixture-key")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://g.test")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _ledger(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "state" / "enmaas" / "runs" / "usage.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_bad_usage_counts_reach_the_ledger_and_json_as_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ask_env(tmp_path, monkeypatch)

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _empty_openai({"prompt_tokens": "abc", "completion_tokens": "Z" * 9000})

    monkeypatch.setattr(cli, "call", fake_call)
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-p", "x", "-m", "gpt-4o", "--json"]
    code = cli.main(argv)
    captured = capsys.readouterr()
    assert code == 1
    assert "ZZZ" not in captured.out + captured.err
    payload = json.loads(captured.out)
    assert payload["tokens"]["input"] is None
    assert payload["tokens"]["output"] is None
    assert len(payload["error"]) < 300
    (row,) = _ledger(tmp_path)
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert "ZZZ" not in json.dumps(row)


# Output writes and symbolic links


def test_write_output_creates_a_new_file_with_mode_600(tmp_path: Path) -> None:
    target = tmp_path / "answer.md"
    write_output(target, "hello é")
    assert target.read_text(encoding="utf-8") == "hello é"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_output_keeps_the_mode_of_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "answer.md"
    target.write_text("old contents that are longer")
    target.chmod(0o644)
    write_output(target, "new")
    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_write_output_refuses_a_symbolic_link(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    link = tmp_path / "answer.md"
    link.symlink_to(victim)
    with pytest.raises(OSError) as caught:
        write_output(link, "overwrite")
    assert str(caught.value) == f"{LINK_REFUSAL}: {link}"
    assert victim.read_text() == "keep me"


def test_write_output_refuses_a_link_that_appears_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    link = tmp_path / "answer.md"
    link.symlink_to(victim)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    with pytest.raises(OSError) as caught:
        write_output(link, "overwrite")
    assert str(caught.value) == f"{LINK_REFUSAL}: {link}"
    assert victim.read_text() == "keep me"


def test_protect_refuses_a_symbolic_link(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    link = tmp_path / "answer.md"
    link.symlink_to(victim)
    with pytest.raises(OSError) as caught:
        protect(link)
    assert str(caught.value) == f"{LINK_REFUSAL}: {link}"
    assert not marker_path(victim).exists()
    assert not is_protected(victim)


OK = {
    "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


@pytest.mark.parametrize("batch", [1, 2])
async def test_map_fails_an_item_whose_output_is_a_symbolic_link(
    tmp_path: Path, batch: int
) -> None:
    items = tmp_path / "items.jsonl"
    items.write_text('{"id":"a"}\n{"id":"b"}\n')
    template = tmp_path / "template.txt"
    template.write_text("shared\n===PER ITEM===\n{id}")
    outdir = tmp_path / "out"
    outdir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (outdir / "a.md").symlink_to(victim)

    def handler(request: httpx.Request) -> httpx.Response:
        if batch > 1:
            text = "\n".join(f"=== ANSWER {name} ===\nanswer" for name in ("a", "b"))
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": text}}]},
            )
        return httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await run_map(
            client=client,
            base_url="https://mock.test",
            key="local-fixture-key",
            model="gpt-4o",
            items_path=items,
            template_path=template,
            outdir=outdir,
            concurrency=1,
            retries=0,
            timeout=2,
            thinking=False,
            max_tokens=50,
            dialect="openai",
            resume=False,
            batch=batch,
        )
    assert victim.read_text() == "keep me"
    assert not marker_path(victim).exists()
    assert outcome.ok == 1
    assert outcome.failed == 1
    assert outcome.failures == [("a", f"{LINK_REFUSAL}: {outdir / 'a.md'}")]
    assert (outdir / "b.md").read_text() == "answer"
    assert stat.S_IMODE((outdir / "b.md").stat().st_mode) == 0o600
    rows = [json.loads(line) for line in (outdir / "results.jsonl").read_text().splitlines()]
    failed = [row for row in rows if row.get("id") == "a"]
    assert failed[0]["status"] == "error"
    assert failed[0]["error"] == f"{LINK_REFUSAL}: {outdir / 'a.md'}"


def test_ask_refuses_an_output_that_is_a_symbolic_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ask_env(tmp_path, monkeypatch)
    calls = 0

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return OK

    monkeypatch.setattr(cli, "call", fake_call)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    link = tmp_path / "answer.md"
    link.symlink_to(victim)
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-p", "x", "-m", "gpt-4o"]
    code = cli.main([*argv, "-o", str(link)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == f"error: {LINK_REFUSAL}: {link}\n"
    assert victim.read_text() == "keep me"
    assert not marker_path(victim).exists()
    assert calls == 0


def test_ask_writes_a_new_output_with_mode_600(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ask_env(tmp_path, monkeypatch)

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return OK

    monkeypatch.setattr(cli, "call", fake_call)
    output = tmp_path / "answer.md"
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-p", "x", "-m", "gpt-4o"]
    code = cli.main([*argv, "-o", str(output)])
    assert code == 0, capsys.readouterr().err
    assert output.read_text() == "answer"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert is_protected(output)


def test_ask_passes_the_key_to_call_for_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ask_env(tmp_path, monkeypatch)
    seen: list[str] = []

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs.get("secret", ""))
        return OK

    monkeypatch.setattr(cli, "call", fake_call)
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-p", "x", "-m", "gpt-4o"]
    assert cli.main(argv) == 0, capsys.readouterr().err
    assert seen == ["local-fixture-key"]


@pytest.mark.parametrize("batch", [1, 2])
async def test_map_passes_the_key_to_call_for_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: int
) -> None:
    seen: list[str] = []

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs.get("secret", ""))
        return OK

    monkeypatch.setattr("enmaas_cli.runner.call", fake_call)
    items = tmp_path / "items.jsonl"
    items.write_text('{"id":"a"}\n')
    template = tmp_path / "template.txt"
    template.write_text("shared\n===PER ITEM===\n{id}")
    async with httpx.AsyncClient() as client:
        await run_map(
            client=client,
            base_url="https://mock.test",
            key="the-key",
            model="gpt-4o",
            items_path=items,
            template_path=template,
            outdir=tmp_path / "out",
            concurrency=1,
            retries=0,
            timeout=2,
            thinking=False,
            max_tokens=50,
            dialect="openai",
            resume=False,
            batch=batch,
        )
    assert seen == ["the-key"]


# Pruning old runs

RUN_NAME = "2020-01-01T00-00-00-aaaa"


def _age(path: Path, days: int = 40) -> None:
    then = time.time() - days * 86400
    os.utime(path, (then, then), follow_symlinks=False)


def test_prune_leaves_a_symbolic_link_root_alone(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / RUN_NAME).mkdir(parents=True)
    _age(real / RUN_NAME)
    link = tmp_path / "runs"
    link.symlink_to(real)
    prune_runs(link)
    assert (real / RUN_NAME).exists()


def test_prune_leaves_a_root_under_a_symbolic_link_parent_alone(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "runs" / RUN_NAME).mkdir(parents=True)
    _age(real / "runs" / RUN_NAME)
    link = tmp_path / "state"
    link.symlink_to(real)
    prune_runs(link / "runs")
    assert (real / "runs" / RUN_NAME).exists()


def test_prune_keeps_old_directories_that_are_not_runs(tmp_path: Path) -> None:
    important = tmp_path / "important"
    important.mkdir()
    (important / "data.txt").write_text("keep")
    _age(important)
    old_run = tmp_path / RUN_NAME
    old_run.mkdir()
    _age(old_run)
    prune_runs(tmp_path)
    assert (important / "data.txt").read_text() == "keep"
    assert not old_run.exists()


def test_prune_does_not_follow_a_run_named_symbolic_link(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "data.txt").write_text("keep")
    _age(elsewhere)
    root = tmp_path / "runs"
    root.mkdir()
    link = root / RUN_NAME
    link.symlink_to(elsewhere)
    _age(link)
    prune_runs(root)
    assert (elsewhere / "data.txt").read_text() == "keep"


# Gateway scheme


def test_cleartext_gateway_off_localhost_is_rejected() -> None:
    url = "http://gateway.example.test"
    with pytest.raises(ValueError) as caught:
        cli.resolve_base_url(None, {"ENMAAS_BASE_URL": url}, Path("/nonexistent"))
    assert str(caught.value) == (
        f"gateway URL is not valid (http is only allowed for localhost, use https): {url}"
    )


@pytest.mark.parametrize(
    "url",
    ["http://10.0.0.1", "http://localhost.example.test", "http://[::2]", "http://128.0.0.1"],
)
def test_cleartext_gateway_on_other_hosts_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="http is only allowed for localhost"):
        cli.resolve_base_url(None, {"ENMAAS_BASE_URL": url}, Path("/nonexistent"))


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://127.3.4.5:9000",
        "http://[::1]:8080",
        "http://LOCALHOST",
    ],
)
def test_cleartext_gateway_on_loopback_is_accepted(url: str) -> None:
    assert cli.resolve_base_url(None, {"ENMAAS_BASE_URL": url}, Path("/nonexistent"))


# Gateway error bodies and retry waits

KEY = "sk-oai-" + "k" * 40


async def test_a_key_straddling_the_truncation_point_is_redacted() -> None:
    body = "x" * 495 + KEY + "..."

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as caught:
            await call(
                client, "https://g.test", REQ, timeout=5, retries=0, sleep=_no_sleep, secret=KEY
            )
    message = str(caught.value)
    assert "***REDACTED***" in message
    assert "sk-oai" not in message
    assert "kkkk" not in message


async def test_a_terminal_error_body_is_redacted_before_truncation() -> None:
    body = "x" * 495 + KEY

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as caught:
            await call(
                client, "https://g.test", REQ, timeout=5, retries=0, sleep=_no_sleep, secret=KEY
            )
    assert "sk-oai" not in str(caught.value)


async def test_retry_after_is_capped() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "100000"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    transport = httpx.MockTransport(lambda request: next(responses))
    async with httpx.AsyncClient(transport=transport) as client:
        await call(client, "https://g.test", REQ, timeout=5, retries=3, sleep=record)
    assert MAX_RETRY_AFTER_SECONDS == 60.0
    assert slept == [60.0]


# Key files


def test_a_fifo_key_file_fails_fast(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("platform has no named pipes")
    fifo = tmp_path / "key"
    os.mkfifo(fifo)
    with pytest.raises(AuthError) as caught:
        resolve_key(key_file=fifo, env={}, home=tmp_path)
    assert str(caught.value) == f"key file is not a regular file: {fifo}"


def test_a_large_key_file_is_rejected(tmp_path: Path) -> None:
    big = tmp_path / "key"
    big.write_text("k" * 8192)
    big.chmod(0o600)
    with pytest.raises(AuthError) as caught:
        resolve_key(key_file=big, env={}, home=tmp_path)
    assert str(caught.value) == f"key file is too large to be a key: {big}"


def test_a_key_file_of_exactly_the_limit_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text("k" * 4096)
    path.chmod(0o600)
    assert resolve_key(key_file=path, env={}, home=tmp_path).value == "k" * 4096


def test_a_directory_key_file_keeps_its_message(tmp_path: Path) -> None:
    folder = tmp_path / "keydir"
    folder.mkdir()
    with pytest.raises(AuthError) as caught:
        resolve_key(key_file=folder, env={}, home=tmp_path)
    assert str(caught.value) == f"key file is a directory: {folder}"
