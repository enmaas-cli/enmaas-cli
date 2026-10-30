"""Tests for models command error handling."""

from collections.abc import Callable

import httpx

from enmaas_cli.cli import run_models


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_models_401_raises_http_error() -> None:
    """Verify that a 401 response raises HTTPError (to be caught by _dispatch)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    async with _client(handler) as client:
        try:
            await run_models(
                client=client,
                base_url="https://g.test",
                key="sk-test",
                dialect="both",
                timeout=5.0,
            )
            raise AssertionError("Expected HTTPError to be raised")
        except httpx.HTTPError:
            pass  # Expected
