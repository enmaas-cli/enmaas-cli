from collections.abc import Callable
from pathlib import Path

import httpx

from enmaas_cli.cli import run_ask, run_models


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
