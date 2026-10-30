"""Tests for warning output."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import run_ask


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ask_surfaces_reasoning_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ask with a model that has no verified knob and explicit --thinking off writes a warning."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "response"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    async with _client(handler) as client:
        await run_ask(
            client=client,
            base_url="https://g.test",
            key="sk-test",
            model="gpt-6-sol",
            prompt="hi",
            system=None,
            dialect="openai",
            thinking=False,
            max_tokens=None,
            timeout=5.0,
            retries=0,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
        )

    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert "gpt-6-sol" in captured.err
