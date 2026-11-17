from __future__ import annotations

import contextlib
import http.server
import json
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.cli import main
from enmaas_cli.client import call, is_retryable, make_client
from enmaas_cli.dialect import Request
from enmaas_cli.errors import GatewayError

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


async def test_bad_reply_content_type_is_redacted_before_it_is_cut() -> None:
    from enmaas_cli.auth import REDACTION
    from enmaas_cli.errors import BadReplyError

    secret = "sk-oai-ABCDEFGHIJKLMNOP"
    prefix = "text/plain; " + "x" * 58
    content_type = prefix + secret + "; charset=utf-8"
    # The key starts at character 70 and ends past character 80.
    assert len(prefix) < 80 < len(prefix) + len(secret)
    transport, calls = _counting(
        httpx.Response(200, content=b"not json", headers={"content-type": content_type})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BadReplyError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep, secret=secret)
    assert len(calls) == 1
    message = str(exc.value)
    for start in range(len(secret) - 3):
        assert secret[start : start + 4] not in message
    assert f"content type {prefix}{REDACTION}, " in message


async def test_bad_reply_content_type_is_printable() -> None:
    from enmaas_cli.errors import BadReplyError

    transport, _ = _counting(
        httpx.Response(
            200,
            content=b"not json",
            headers=[(b"content-type", b"text/plain\xc2\x9b31m\x1b]0;t\x07")],
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BadReplyError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    message = str(exc.value)
    assert all(ch.isprintable() for ch in message)
    assert "text/plain?31m" in message


async def test_reply_nested_too_deeply_is_a_bad_reply_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enmaas_cli.errors import BadReplyError

    # The depth at which the decoder gives up depends on the Python version
    # and the stack size, so the decoder is made to give up here.
    def deep(response: httpx.Response) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(httpx.Response, "json", deep)
    transport, calls = _counting(
        httpx.Response(200, content=b"[[1]]", headers={"content-type": "application/json"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BadReplyError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    assert len(calls) == 1
    assert "JSON nested too deeply" in str(exc.value)


async def test_a_reply_with_200_000_levels_is_a_bad_reply_and_never_retried() -> None:
    from enmaas_cli.errors import BadReplyError

    body = b"[" * 200_000 + b"]" * 200_000
    transport, calls = _counting(
        httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BadReplyError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    assert len(calls) == 1
    # The decoder gives up on this body, or reads it when the stack has room.
    # A body it can read is a list, which is a bad reply as well.
    assert str(exc.value) in {
        f"gateway reply is {kind} (content type application/json, {len(body)} bytes)"
        for kind in ("JSON nested too deeply", "JSON that is not an object")
    }


async def test_invalid_url_from_the_transport_is_terminal_gateway_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.InvalidURL("bad url")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=3, sleep=_no_sleep)
    assert calls == 1
    assert exc.value.status is None
    assert exc.value.attempts == 1
    assert "InvalidURL" in str(exc.value)


async def test_a_key_echoed_json_escaped_in_an_error_body_is_redacted() -> None:
    import json

    key = 'sk-a"b\\c-123'
    echoed = json.dumps({"error": {"message": f"invalid key {key}"}})
    assert key not in echoed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=echoed.encode(), headers={"content-type": "json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as exc:
            await call(client, BASE, REQ, timeout=5, retries=0, sleep=_no_sleep, secret=key)
    message = str(exc.value)
    assert json.dumps(key)[1:-1] not in message
    assert key not in message
    assert "REDACTED" in message


# A loopback gateway is reached directly, never through a proxy from the environment.
#
# Each test runs `main` against a gateway on a loopback address with every
# proxy variable pointing at a listener that stands in for a proxy. The
# listener counts the connections it accepts. A request that went through it
# would carry the key, so the count must stay at zero.


KEY = "sk-proxy-test-key-0123"
MODEL = "rits/proxy-test"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
OPENAI_REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}
ANTHROPIC_REPLY = {
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 2},
}


@dataclass
class Seen:
    """What the fake gateway received: one (method, path, headers) per request."""

    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _handler_for(seen: Seen) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(self, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _note(self) -> None:
            with seen.lock:
                seen.requests.append((self.command, self.path, dict(self.headers.items())))

        def do_GET(self) -> None:
            self._note()
            self._reply({"data": [{"id": MODEL}]})

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self._note()
            self._reply(ANTHROPIC_REPLY if self.path == "/v1/messages" else OPENAI_REPLY)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


class _Server6(_Server):
    address_family = socket.AF_INET6


def _ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _serving(server: http.server.ThreadingHTTPServer) -> Iterator[int]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


class ProxyListener:
    """A listening socket that accepts and closes every connection, and counts them."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.05)
        self.port = int(self.sock.getsockname()[1])
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _accept_one(self) -> bool:
        try:
            conn, _ = self.sock.accept()
        except (TimeoutError, BlockingIOError):
            return False
        self.accepted += 1
        conn.close()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self._accept_one()

    def connections(self) -> int:
        """Stop the loop, take any connection still queued, and return the count."""
        self._stop.set()
        self._thread.join(10)
        self.sock.setblocking(False)
        while self._accept_one():
            pass
        return self.accepted

    def close(self) -> None:
        self._stop.set()
        self._thread.join(10)
        self.sock.close()


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ProxyListener]:
    listener = ProxyListener()
    for name in PROXY_VARS:
        monkeypatch.setenv(name, f"http://127.0.0.1:{listener.port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", KEY)
    try:
        yield listener
    finally:
        listener.close()


HOSTS = [
    pytest.param("localhost", "127.0.0.1", id="localhost"),
    pytest.param("127.0.0.1", "127.0.0.1", id="ipv4"),
    pytest.param(
        "[::1]",
        "::1",
        id="ipv6",
        marks=pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback"),
    ),
]


def _command(name: str, dialect: str, tmp_path: Path) -> list[str]:
    config = ["--config", str(tmp_path / "none.toml")]
    if name == "models":
        return [*config, "models", "--dialect", dialect, "--timeout", "3"]
    common = ["-m", MODEL, "--dialect", dialect, "--retries", "0", "--timeout", "3"]
    if name == "ask":
        return [*config, "ask", *common, "-p", "hello", "-o", str(tmp_path / "answer.md")]
    items = tmp_path / "items.jsonl"
    items.write_text(json.dumps({"id": "a", "x": 1}) + "\n", encoding="utf-8")
    template = tmp_path / "template.txt"
    template.write_text("do {x}", encoding="utf-8")
    return [
        *config,
        "map",
        *common,
        "--items",
        str(items),
        "--template",
        str(template),
        "--outdir",
        str(tmp_path / "out"),
    ]


def _key_header(dialect: str, headers: dict[str, str]) -> str | None:
    lowered = {name.lower(): value for name, value in headers.items()}
    if dialect == "anthropic":
        return lowered.get("x-api-key")
    value = lowered.get("authorization")
    return value.removeprefix("Bearer ") if value is not None else None


@pytest.mark.parametrize(("host", "bind"), HOSTS)
@pytest.mark.parametrize("command", ["models", "ask", "map"])
@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
def test_a_loopback_gateway_is_never_reached_through_a_proxy(
    proxy: ProxyListener,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    host: str,
    bind: str,
    command: str,
    dialect: str,
) -> None:
    seen = Seen()
    server_class = _Server6 if ":" in bind else _Server
    with _serving(server_class((bind, 0), _handler_for(seen))) as port:
        monkeypatch.setenv("ENMAAS_BASE_URL", f"http://{host}:{port}")
        code = main(_command(command, dialect, tmp_path))
    out, err = capsys.readouterr()
    assert proxy.connections() == 0
    assert code == 0, err
    assert seen.requests, err
    for _, _, headers in seen.requests:
        assert _key_header(dialect, headers) == KEY
    if command == "models":
        assert MODEL in out


async def test_ssl_cert_file_is_still_read_for_a_loopback_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    not_a_certificate = tmp_path / "ca.pem"
    not_a_certificate.write_text("not a certificate\n", encoding="utf-8")
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(not_a_certificate))
    with pytest.raises(ssl.SSLError):
        make_client("https://localhost:1")
    monkeypatch.delenv("SSL_CERT_FILE")
    client = make_client("https://localhost:1")
    await client.aclose()


async def test_a_remote_gateway_still_uses_the_proxy_variables(proxy: ProxyListener) -> None:
    remote = make_client("https://gateway.example")
    local = make_client("https://localhost:8443")
    try:
        assert any(transport is not None for transport in remote._mounts.values())
        assert local._mounts == {}
    finally:
        await remote.aclose()
        await local.aclose()
    assert proxy.connections() == 0


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


def _make_certificates(directory: Path) -> tuple[Path, Path, Path]:
    """A certificate authority and a localhost certificate it signed.

    Returns the authority's certificate, the server certificate and the
    server key. The extensions are the ones a strict verifier requires.
    """
    curve = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    _openssl(
        "req",
        "-x509",
        *curve,
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-days",
        "2",
        "-subj",
        "/CN=enmaas test authority",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        cwd=directory,
    )
    _openssl(
        "req",
        *curve,
        "-keyout",
        "server.key",
        "-out",
        "server.csr",
        "-subj",
        "/CN=localhost",
        cwd=directory,
    )
    (directory / "server.ext").write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n",
        encoding="utf-8",
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        "server.csr",
        "-CA",
        "ca.pem",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-out",
        "server.pem",
        "-days",
        "2",
        "-extfile",
        "server.ext",
        cwd=directory,
    )
    return directory / "ca.pem", directory / "server.pem", directory / "server.key"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command")
def test_a_private_authority_in_ssl_cert_file_verifies_a_loopback_gateway(
    proxy: ProxyListener,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    certs = tmp_path / "certs"
    certs.mkdir()
    authority, certificate, private_key = _make_certificates(certs)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    seen = Seen()
    server = _Server(("127.0.0.1", 0), _handler_for(seen))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(authority))
    with _serving(server) as port:
        monkeypatch.setenv("ENMAAS_BASE_URL", f"https://localhost:{port}")
        code = main(["--config", str(tmp_path / "none.toml"), "models", "--timeout", "3"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert MODEL in out
    assert len(seen.requests) == 2
    assert proxy.connections() == 0
