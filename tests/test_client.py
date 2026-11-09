import httpx
import pytest

from enmaas_cli.client import GatewayError, call, is_retryable
from enmaas_cli.dialect import Request

REQ = Request(path="/v1/messages", headers={"x-api-key": "k"}, body={"model": "m"})
BASE = "https://gateway.test"


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_service_faults_are_retryable(status: int) -> None:
    assert is_retryable(status) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_request_faults_are_not_retryable(status: int) -> None:
    assert is_retryable(status) is False


def test_504_near_the_gateway_ceiling_is_not_retried() -> None:
    """Retrying a 300s timeout three times spends 20 minutes to fail."""
    assert is_retryable(504, elapsed=299.0) is False
    assert is_retryable(504, elapsed=2.0) is True


def _transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    calls = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(calls)

    return httpx.MockTransport(handler)


async def test_success_returns_json() -> None:
    transport = _transport([httpx.Response(200, json={"ok": True})])
    async with httpx.AsyncClient(transport=transport) as client:
        data = await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    assert data == {"ok": True}


async def test_retries_then_succeeds() -> None:
    transport = _transport(
        [
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    async with httpx.AsyncClient(transport=transport) as client:
        data = await call(client, BASE, REQ, timeout=5, retries=3, sleep=record)
    assert data == {"ok": True}
    assert len(slept) == 1


async def test_terminal_status_does_not_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="unauthorized")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    assert calls == 1
    assert exc.value.status == 401


async def test_retries_exhausted_raises_with_attempt_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=2, sleep=_no_sleep)
    assert exc.value.attempts == 3
    assert "503" in str(exc.value)


async def test_retry_after_header_is_honoured() -> None:
    transport = _transport(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    async with httpx.AsyncClient(transport=transport) as client:
        await call(client, BASE, REQ, timeout=5, retries=3, sleep=record)
    assert slept == [7.0]


async def test_connection_error_is_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data = await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    assert data == {"ok": True}


async def test_backoff_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)
    transport = _transport(
        [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    async with httpx.AsyncClient(transport=transport) as client:
        await call(client, BASE, REQ, timeout=5, retries=3, sleep=record)
    assert slept == [1.0, 2.0]


async def test_slow_504_fails_immediately_with_actionable_message() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(504, text="Gateway Time-out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as exc:
            await call(
                client, BASE, REQ, timeout=280, retries=3, sleep=_no_sleep, elapsed_fn=lambda: 299.0
            )
    assert calls == 1
    assert "--thinking off" in str(exc.value)


async def test_slow_timeout_fails_immediately_with_actionable_message() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("request timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as exc:
            await call(
                client, BASE, REQ, timeout=280, retries=3, sleep=_no_sleep, elapsed_fn=lambda: 281.0
            )
    assert calls == 1
    assert "--thinking off" in str(exc.value)


async def test_fast_timeout_is_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("quick timeout")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data = await call(
            client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep, elapsed_fn=lambda: 2.0
        )
    assert data == {"ok": True}
    assert attempts == 2


SECRET_BODY_MARK = "MODEL-OUTPUT-SECRET"


def _counting(response: httpx.Response) -> tuple[httpx.MockTransport, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return response

    return httpx.MockTransport(handler), calls


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            content=f"<html><body>{SECRET_BODY_MARK}</body></html>".encode(),
            headers={"content-type": "text/html"},
        ),
        httpx.Response(200, json=[SECRET_BODY_MARK, 1]),
        httpx.Response(200, json=SECRET_BODY_MARK),
        httpx.Response(
            200,
            content=b'{"text": "' + SECRET_BODY_MARK.encode() + b'\xff\xfe"}',
            headers={"content-type": "application/json"},
        ),
    ],
    ids=["html", "json-list", "json-string", "invalid-utf8"],
)
async def test_unusable_200_body_is_a_bad_reply_and_never_retried(
    response: httpx.Response,
) -> None:
    from enmaas_cli.errors import BadReplyError

    transport, calls = _counting(response)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BadReplyError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    assert len(calls) == 1
    message = str(exc.value)
    assert SECRET_BODY_MARK not in message
    assert "html" not in message.replace("text/html", "")
    assert "bytes" in message


async def test_bad_reply_message_names_content_type_cut_to_80_chars() -> None:
    from enmaas_cli.errors import BadReplyError

    long_type = "text/plain; " + "x" * 200
    body = b"not json at all"
    transport, _ = _counting(httpx.Response(200, content=body, headers={"content-type": long_type}))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BadReplyError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    message = str(exc.value)
    assert long_type[:80] in message
    assert long_type[:81] not in message
    assert f"{len(body)} bytes" in message
    assert "not json" not in message


async def test_decoding_error_is_terminal_gateway_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.DecodingError("bad gzip stream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    assert calls == 1
    assert exc.value.status is None
    assert exc.value.attempts == 1
    assert "DecodingError" in str(exc.value)


async def test_http_error_message_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("failed near sk-live-key-123")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as exc:
            await call(
                client,
                BASE,
                REQ,
                timeout=5,
                retries=3,
                sleep=_no_sleep,
                secret="sk-live-key-123",
            )
    assert "sk-live-key-123" not in str(exc.value)
