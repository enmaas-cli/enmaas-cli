from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import ExitCode, run_ask, run_models


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ask_writes_body_to_file_and_returns_receipt(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "ANSWER"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    async with _client(handler) as client:
        result = await run_ask(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model="rits/zai-org/glm-5-3",
            prompt="hi",
            system=None,
            dialect=None,
            thinking=False,
            max_tokens=None,
            timeout=5,
            retries=0,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
        )
    assert result.path.read_text(encoding="utf-8") == "ANSWER"
    assert "ANSWER" in result.receipt
    assert result.exit_code == 0


async def test_ask_surfaces_empty_response_as_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8192},
            },
        )

    async with _client(handler) as client:
        result = await run_ask(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model="rits/zai-org/glm-5-3",
            prompt="hi",
            system=None,
            dialect=None,
            thinking=True,
            max_tokens=None,
            timeout=5,
            retries=0,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
        )
    assert result.exit_code == 1
    assert "--thinking off" in result.receipt


async def test_ask_gateway_failure_marks_a_defaulted_model(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _client(handler) as client:
        result = await run_ask(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model="rits/zai-org/glm-5-3",
            prompt="hi",
            system=None,
            dialect=None,
            thinking=True,
            max_tokens=None,
            timeout=5,
            retries=0,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
            model_defaulted=True,
        )
    assert result.exit_code == 1
    assert "  model     rits/zai-org/glm-5-3  (default)" in result.receipt


async def test_ask_schema_rejection_marks_a_defaulted_model(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "not json"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    schema = tmp_path / "schema.json"
    schema.write_text('{"type": "object"}', encoding="utf-8")
    async with _client(handler) as client:
        result = await run_ask(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model="rits/zai-org/glm-5-3",
            prompt="hi",
            system=None,
            dialect=None,
            thinking=True,
            max_tokens=None,
            timeout=5,
            retries=0,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
            schema_file=schema,
            model_defaulted=True,
        )
    assert result.exit_code == 1
    assert "  model     rits/zai-org/glm-5-3  (default)" in result.receipt


@pytest.mark.parametrize("defaulted", [True, False])
async def test_ask_success_receipt_marks_only_a_defaulted_model(
    tmp_path: Path, defaulted: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "ANSWER"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    async with _client(handler) as client:
        result = await run_ask(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model="rits/zai-org/glm-5-3",
            prompt="hi",
            system=None,
            dialect=None,
            thinking=False,
            max_tokens=None,
            timeout=5,
            retries=0,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
            model_defaulted=defaulted,
        )
    assert result.exit_code == 0
    if defaulted:
        assert "  model     rits/zai-org/glm-5-3  (default)" in result.receipt
    else:
        assert "(default)" not in result.receipt


async def test_ask_redacts_the_key_from_errors(tmp_path: Path) -> None:
    key = "sk-oai-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key {key}")

    async with _client(handler) as client:
        result = await run_ask(
            client=client,
            base_url="https://g.test",
            key=key,
            model="rits/zai-org/glm-5-3",
            prompt="hi",
            system=None,
            dialect=None,
            thinking=False,
            max_tokens=None,
            timeout=5,
            retries=0,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
        )
    assert key not in result.receipt
    assert "***REDACTED***" in result.receipt
    assert result.exit_code == 1


async def test_models_merges_both_dialects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "Authorization" in request.headers:
            return httpx.Response(
                200, json={"data": [{"id": "gpt-6-sol"}, {"id": "rits/zai-org/glm-5-3"}]}
            )
        return httpx.Response(
            200, json={"data": [{"id": "claude-opus-5-5"}, {"id": "rits/zai-org/glm-5-3"}]}
        )

    async with _client(handler) as client:
        rows = await run_models(
            client=client,
            base_url="https://g.test",
            key="k",
            dialect="both",
            timeout=5,
        )
    by_id = {r["id"]: r for r in rows}
    assert by_id["rits/zai-org/glm-5-3"]["dialects"] == ["anthropic", "openai"]
    assert by_id["gpt-6-sol"]["dialects"] == ["openai"]
    assert by_id["claude-opus-5-5"]["dialects"] == ["anthropic"]


def test_an_over_budget_ask_exits_three_without_calling_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusal happens before the HTTP client is opened, so it costs nothing."""
    import json
    from datetime import UTC, datetime

    from enmaas_cli.cli import main

    state = tmp_path / "state"
    # state_root() is XDG_STATE_HOME/enmaas/runs, and the ledger sits
    # inside it, so the fixture must match that exact path.
    (state / "enmaas" / "runs").mkdir(parents=True)
    (state / "enmaas" / "runs" / "usage.jsonl").write_text(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "command": "ask",
                "model": "m",
                "dialect": "openai",
                "input_tokens": 60_000,
                "cached_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "elapsed": 1.0,
                "status": "ok",
                "schema_used": False,
                "error_kind": None,
                "items_total": None,
                "items_ok": None,
                "items_failed": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("ENMAAS_KEY", "k")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    config = tmp_path / "config.toml"
    config.write_text("[budget]\nwindow_days = 30\ntokens = 50_000\n", encoding="utf-8")

    def explode(*a: object, **k: object) -> None:
        raise AssertionError("a refused command must not open a client")

    monkeypatch.setattr(httpx, "AsyncClient", explode)

    code = main(["--config", str(config), "ask", "-p", "hi"])

    assert code == int(ExitCode.ABORTED)
    assert "60,000 of 50,000" in capsys.readouterr().err


def test_an_over_budget_json_run_still_emits_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 3 alone cannot distinguish a refusal from a breaker abort.

    A --json caller that got an empty stdout on refusal would have to
    guess why the run stopped.
    """
    import json as json_mod
    from datetime import UTC, datetime

    from enmaas_cli.cli import main

    state = tmp_path / "state"
    (state / "enmaas" / "runs").mkdir(parents=True)
    (state / "enmaas" / "runs" / "usage.jsonl").write_text(
        json_mod.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "command": "ask",
                "model": "m",
                "dialect": "openai",
                "input_tokens": 60_000,
                "cached_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "elapsed": 1.0,
                "status": "ok",
                "schema_used": False,
                "error_kind": None,
                "items_total": None,
                "items_ok": None,
                "items_failed": None,
                "charge": 60_000,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("ENMAAS_KEY", "k")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    config = tmp_path / "config.toml"
    config.write_text("[budget]\nwindow_days = 30\ntokens = 50_000\n", encoding="utf-8")

    code = main(["--config", str(config), "ask", "-p", "hi", "--json"])

    assert code == int(ExitCode.ABORTED)
    payload = json_mod.loads(capsys.readouterr().out)
    assert payload["abort_reason"] == "budget"
    assert payload["budget"]["tripped"] == "rolling"
    assert payload["budget"]["rolling"]["spent"] == 60_000
