"""A configured token budget that cannot be tracked refuses the run before any request.

A missing ledger reads as no spend. When it also cannot be created, every
run would spend, fail to record it, and let the next run start from zero
again. So with a rolling budget, `ask` and `map` first prove the ledger can
be appended, and refuse when it cannot.
"""

from __future__ import annotations

import errno
import http.server
import json
import os
import resource
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

import enmaas_cli
from enmaas_cli import usage
from enmaas_cli.cli import main
from enmaas_cli.usage import UsageEvent, append_event, can_append

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can write a read-only file or directory"
)

MODEL = "rits/zai-org/glm-5-3"
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "the answer"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    on_request: Callable[[], None] | None = None,
) -> list[httpx.Request]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if on_request is not None:
            on_request()
        return httpx.Response(200, json=REPLY)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    return seen


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "xdg" / "enmaas" / "runs"
    state.mkdir(parents=True)
    return state


def _config(tmp_path: Path, *, budget: bool) -> list[str]:
    config = tmp_path / "config.toml"
    config.write_text(
        "[budget]\nwindow_days = 7\ntokens = 1000\n" if budget else "", encoding="utf-8"
    )
    return ["--config", str(config)]


def _ask(tmp_path: Path) -> list[str]:
    return ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"] + [
        "-o",
        str(tmp_path / "answer.md"),
    ]


def _map(tmp_path: Path) -> list[str]:
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n', encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    return ["map", "-m", MODEL, "--dialect", "openai", "--retries", "0"] + [
        "--items",
        str(items),
        "--template",
        str(template),
        "--outdir",
        str(tmp_path / "out"),
    ]


COMMANDS = {"ask": _ask, "map": _map}


def _refusal(ledger: Path) -> str:
    return (
        f"the usage ledger {ledger} cannot be written, so the token budget "
        "cannot be tracked; fix it or remove the [budget] table from the config"
    )


@pytest.fixture
def read_only_dirs() -> Iterator[list[Path]]:
    """Directories a test made read-only. Made writable again, so cleanup can remove them."""
    made: list[Path] = []
    yield made
    for path in made:
        path.chmod(0o700)


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_state_directory_that_cannot_be_written_refuses_a_budgeted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_only_dirs: list[Path],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    state = _state(tmp_path)
    state.chmod(0o500)
    read_only_dirs.append(state)
    argv = [*_config(tmp_path, budget=True), *COMMANDS[command](tmp_path)]
    assert main(argv) == 1
    assert seen == []
    err = capsys.readouterr().err
    assert err.splitlines() == [f"error: {_refusal(state / 'usage.jsonl')}"]
    assert not (state / "usage.jsonl").exists()


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_read_only_ledger_refuses_a_budgeted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    ledger = _state(tmp_path) / "usage.jsonl"
    ledger.write_text("", encoding="utf-8")
    ledger.chmod(0o400)
    argv = [*_config(tmp_path, budget=True), *COMMANDS[command](tmp_path)]
    assert main(argv) == 1
    assert seen == []
    assert capsys.readouterr().err.splitlines() == [f"error: {_refusal(ledger)}"]


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_the_refusal_is_the_json_error_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_only_dirs: list[Path],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    state = _state(tmp_path)
    state.chmod(0o500)
    read_only_dirs.append(state)
    argv = [*_config(tmp_path, budget=True), *COMMANDS[command](tmp_path), "--json"]
    assert main(argv) == 1
    assert seen == []
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": _refusal(state / "usage.jsonl"),
    }


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_without_a_budget_the_run_goes_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    ledger = _state(tmp_path) / "usage.jsonl"
    ledger.write_text("", encoding="utf-8")
    ledger.chmod(0o400)
    argv = [*_config(tmp_path, budget=False), *COMMANDS[command](tmp_path)]
    assert main(argv) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert ledger.read_text(encoding="utf-8") == ""


def test_without_a_budget_map_goes_on_when_the_state_directory_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_only_dirs: list[Path],
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    state = _state(tmp_path)
    state.chmod(0o500)
    read_only_dirs.append(state)
    assert main([*_config(tmp_path, budget=False), *_map(tmp_path)]) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert not (state / "usage.jsonl").exists()


def test_a_budgeted_run_creates_a_missing_ledger_before_it_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "xdg" / "enmaas" / "runs" / "usage.jsonl"
    sizes: list[int] = []
    seen = _gateway(tmp_path, monkeypatch, lambda: sizes.append(ledger.stat().st_size))
    assert main([*_config(tmp_path, budget=True), *_ask(tmp_path)]) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert sizes == [0]
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_with_the_ledger_off_a_budgeted_run_is_refused_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    assert main([*_config(tmp_path, budget=True), *_ask(tmp_path)]) == 3
    assert seen == []
    err = capsys.readouterr().err
    assert "ENMAAS_NO_USAGE_LOG disables the ledger" in err
    assert not (tmp_path / "xdg" / "enmaas" / "runs" / "usage.jsonl").exists()


# The check also proves there is room for a row. A ledger that opens but
# cannot grow, as on a full disk or at a file size limit, would otherwise
# pass, and every budgeted run would spend and record nothing.


class _Gateway(http.server.ThreadingHTTPServer):
    daemon_threads = True
    seen = 0


def _answer(server: _Gateway) -> type[http.server.BaseHTTPRequestHandler]:
    body = json.dumps(REPLY).encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            server.seen += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


@pytest.fixture
def local_gateway() -> Iterator[_Gateway]:
    server = _Gateway(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    server.RequestHandlerClass = _answer(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(30)


def _old_rows(ledger: Path, size: int) -> None:
    """Fill the ledger past `size` bytes with rows from outside the budget window."""
    row = UsageEvent(
        timestamp=datetime.now(UTC) - timedelta(days=60),
        command="ask",
        model=MODEL,
        dialect="openai",
        input_tokens=1,
        cached_tokens=0,
        output_tokens=1,
        reasoning_tokens=None,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
        charge=2,
    )
    while not ledger.exists() or ledger.stat().st_size < size:
        assert append_event(ledger, row)


def _capped_child(
    tmp_path: Path, server: _Gateway, limit: int, argv: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a child process whose files cannot grow past `limit` bytes."""

    def cap() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "ENMAAS_KEY": "sk-oai-x",
        "ENMAAS_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(enmaas_cli.__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", "from enmaas_cli.cli import entry; entry()", *argv],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        preexec_fn=cap,
        check=False,
    )


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_ledger_at_the_file_size_limit_refuses_a_budgeted_run(
    tmp_path: Path, local_gateway: _Gateway, command: str
) -> None:
    ledger = _state(tmp_path) / "usage.jsonl"
    _old_rows(ledger, 8192)
    before = ledger.read_bytes()
    argv = [*_config(tmp_path, budget=True), *COMMANDS[command](tmp_path)]
    done = _capped_child(tmp_path, local_gateway, len(before), argv)
    assert done.returncode == 1, done.stderr
    assert local_gateway.seen == 0
    assert done.stderr.splitlines() == [f"error: {_refusal(ledger)}"]
    assert ledger.read_bytes() == before


def test_a_map_where_no_file_can_grow_sends_nothing_with_a_budget(
    tmp_path: Path, local_gateway: _Gateway
) -> None:
    state = _state(tmp_path)
    argv = [*_config(tmp_path, budget=True), *_map(tmp_path)]
    done = _capped_child(tmp_path, local_gateway, 100, argv)
    assert done.returncode == 1, done.stderr
    assert local_gateway.seen == 0
    assert done.stderr.splitlines() == [f"error: {_refusal(state / 'usage.jsonl')}"]


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_without_a_budget_no_room_probe_is_made(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    probes: list[Path] = []

    def counted(directory: Path) -> bool:
        probes.append(directory)
        return True

    monkeypatch.setattr(usage, "_has_room", counted)
    argv = [*_config(tmp_path, budget=False), *COMMANDS[command](tmp_path)]
    assert main(argv) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert probes == []


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_budgeted_run_leaves_no_probe_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    state = _state(tmp_path)
    probes: list[Path] = []
    real = usage._has_room

    def counted(directory: Path) -> bool:
        probes.append(directory)
        return real(directory)

    monkeypatch.setattr(usage, "_has_room", counted)
    argv = [*_config(tmp_path, budget=True), *COMMANDS[command](tmp_path)]
    assert main(argv) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert probes == [state]
    assert [p.name for p in state.iterdir() if p.name.startswith(".")] == []


def test_the_room_probe_leaves_the_directory_as_it_was(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    assert can_append(ledger)
    assert [p.name for p in tmp_path.iterdir()] == ["usage.jsonl"]
    assert ledger.read_bytes() == b""


def test_a_ledger_too_close_to_the_size_limit_cannot_be_appended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "usage.jsonl"
    ledger.write_bytes(b"x" * 10)
    hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
    monkeypatch.setattr(resource, "getrlimit", lambda which: (10 + usage.LEDGER_HEADROOM - 1, hard))
    assert not can_append(ledger)
    monkeypatch.setattr(resource, "getrlimit", lambda which: (10 + usage.LEDGER_HEADROOM, hard))
    assert can_append(ledger)


def test_a_directory_without_room_for_the_probe_cannot_be_appended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "usage.jsonl"

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    # The probe is the only write the check makes.
    monkeypatch.setattr(os, "write", full)
    assert not can_append(ledger)
    assert [p.name for p in tmp_path.iterdir()] == ["usage.jsonl"]
