"""A real enmaas process stopped mid-run: its spend still reaches the ledger.

Each test starts the CLI as a child process against a local gateway on
127.0.0.1. The gateway answers the first few chat requests and holds every
later one until the test releases it, so the test can stop the child at a
known point. Every wait polls a condition with a deadline.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import enmaas_cli

DEADLINE = 10.0
# Metered tokens of one answered request: 5 uncached input plus 2 output.
CHARGE = 7
MODEL = "rits/zai-org/glm-5-3"
REPLY = json.dumps(
    {
        "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
).encode("utf-8")


@dataclass
class Gateway:
    port: int = 0
    # Requests answered at once, counted across every run of the test.
    answer_first: int = 0
    arrived: int = 0
    held: int = 0
    release: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _handler_for(gateway: Gateway) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            with gateway.lock:
                number = gateway.arrived
                gateway.arrived += 1
                hold = number >= gateway.answer_first
                if hold:
                    gateway.held += 1
            if hold:
                gateway.release.wait(DEADLINE * 3)
            with contextlib.suppress(OSError):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(REPLY)))
                self.end_headers()
                self.wfile.write(REPLY)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A held request whose client was killed fails to write its reply.
        pass


@pytest.fixture
def gateway() -> Iterator[Gateway]:
    state = Gateway()
    server = _Server(("127.0.0.1", 0), _handler_for(state))
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(DEADLINE)


@dataclass
class Cli:
    tmp: Path
    gateway: Gateway
    children: list[subprocess.Popen[str]] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return self.tmp / "state" / "enmaas" / "runs"

    @property
    def ledger(self) -> Path:
        return self.state / "usage.jsonl"

    def env(self) -> dict[str, str]:
        source = str(Path(enmaas_cli.__file__).resolve().parents[1])
        return {
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "ENMAAS_BASE_URL": f"http://127.0.0.1:{self.gateway.port}",
            "ENMAAS_KEY": "test-key",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": source,
        }

    def start(self, *args: str, stdout: int = subprocess.PIPE) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys.executable, "-c", "from enmaas_cli.cli import main; raise SystemExit(main())"]
            + list(args),
            env=self.env(),
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.children.append(child)
        return child

    def map_args(self, name: str, count: int, *extra: str) -> list[str]:
        items = self.tmp / f"{name}-items.jsonl"
        items.write_text(
            "".join(json.dumps({"id": f"i{n}", "x": n}) + "\n" for n in range(count)),
            encoding="utf-8",
        )
        template = self.tmp / "template.txt"
        template.write_text("do {x}", encoding="utf-8")
        return [
            "map",
            "-m",
            MODEL,
            "--dialect",
            "openai",
            "--items",
            str(items),
            "--template",
            str(template),
            "--outdir",
            str(self.tmp / name),
            "--concurrency",
            "1",
            "--retries",
            "0",
            *extra,
        ]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        child = self.start(*args)
        out, err = child.communicate(timeout=DEADLINE)
        return subprocess.CompletedProcess(child.args, child.returncode, out, err)

    def events(self) -> list[dict[str, Any]]:
        if not self.ledger.exists():
            return []
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def open_runs(self) -> Path:
        return self.state / "open-runs.jsonl"


@pytest.fixture
def cli(tmp_path: Path, gateway: Gateway) -> Iterator[Cli]:
    runner = Cli(tmp=tmp_path, gateway=gateway)
    try:
        yield runner
    finally:
        gateway.release.set()
        for child in runner.children:
            if child.poll() is None:
                child.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.communicate(timeout=DEADLINE)


def _wait_for(condition: Callable[[], bool], what: str, child: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        if condition():
            return
        if child.poll() is not None:
            _, err = child.communicate()
            raise AssertionError(f"the child ended before {what}: {err}")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _charges(index: Path) -> list[int]:
    if not index.exists():
        return []
    rows = []
    for line in index.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            rows.append(json.loads(line))
    return [row["charge"] for row in rows if isinstance(row, dict) and row.get("type") == "request"]


def _held(gateway: Gateway, count: int) -> Callable[[], bool]:
    def check() -> bool:
        with gateway.lock:
            return gateway.held >= count

    return check


def _kill_after(cli: Cli, args: list[str], index: Path, charged: int) -> None:
    """Start a map, wait until `charged` charge rows exist and a request is held, kill -9."""
    child = cli.start(*args)
    _wait_for(lambda: len(_charges(index)) >= charged, f"{charged} charge rows", child)
    _wait_for(_held(cli.gateway, 1), "a held request", child)
    child.kill()
    child.communicate(timeout=DEADLINE)
    assert child.returncode == -signal.SIGKILL


def test_a_killed_run_is_charged_by_the_next_run(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 2
    index = cli.tmp / "first" / "results.jsonl"
    _kill_after(cli, cli.map_args("first", 5), index, 2)
    assert cli.events() == []
    assert cli.open_runs().exists()
    killed_charge = sum(_charges(index))
    assert killed_charge == 2 * CHARGE

    gateway.release.set()
    done = cli.run(*cli.map_args("second", 1))
    assert done.returncode == 0, done.stderr
    killed = [e for e in cli.events() if e["error_kind"] == "Killed"]
    assert len(killed) == 1
    assert killed[0]["charge"] == killed_charge
    assert killed[0]["command"] == "map"
    assert killed[0]["model"] == MODEL
    assert len(cli.events()) == 2
    assert not cli.open_runs().exists()


def test_a_killed_resumed_run_counts_only_its_own_rows(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 2
    first = cli.run(*cli.map_args("job", 2))
    assert first.returncode == 0, first.stderr
    assert [e["charge"] for e in cli.events()] == [2 * CHARGE]

    gateway.answer_first = 4
    index = cli.tmp / "job" / "results.jsonl"
    _kill_after(cli, cli.map_args("job", 6, "--resume"), index, 4)
    assert sum(_charges(index)) == 4 * CHARGE

    gateway.release.set()
    done = cli.run(*cli.map_args("other", 1))
    assert done.returncode == 0, done.stderr
    killed = [e for e in cli.events() if e["error_kind"] == "Killed"]
    assert [e["charge"] for e in killed] == [2 * CHARGE]


def test_a_run_that_ends_normally_leaves_no_record(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 3
    done = cli.run(*cli.map_args("job", 3))
    assert done.returncode == 0, done.stderr
    assert not cli.open_runs().exists()
    (event,) = cli.events()
    assert event["charge"] == 3 * CHARGE


def _stop_after(
    cli: Cli, args: list[str], index: Path, charged: int, signum: int
) -> subprocess.CompletedProcess[str]:
    """Start a map, wait until `charged` charge rows exist and a request is held, signal it."""
    child = cli.start(*args)
    _wait_for(lambda: len(_charges(index)) >= charged, f"{charged} charge rows", child)
    _wait_for(_held(cli.gateway, 1), "a held request", child)
    child.send_signal(signum)
    out, err = child.communicate(timeout=DEADLINE)
    return subprocess.CompletedProcess(child.args, child.returncode, out, err)


def test_sigterm_records_the_run_and_ends_by_the_signal(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 2
    index = cli.tmp / "job" / "results.jsonl"
    done = _stop_after(cli, cli.map_args("job", 5), index, 2, signal.SIGTERM)
    assert done.returncode == -signal.SIGTERM, done.stderr
    (event,) = cli.events()
    assert event["error_kind"] == "Interrupted"
    assert event["status"] == "error"
    assert event["charge"] == sum(_charges(index)) == 2 * CHARGE
    assert "enmaas map interrupted" in done.stdout
    assert "re-run with --resume" in done.stdout
    assert not cli.open_runs().exists()
    assert "Traceback" not in done.stderr


def test_sigint_with_json_prints_one_object(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 2
    index = cli.tmp / "job" / "results.jsonl"
    done = _stop_after(cli, cli.map_args("job", 5, "--json"), index, 2, signal.SIGINT)
    assert done.returncode == -signal.SIGINT, done.stderr
    obj = json.loads(done.stdout)
    assert obj["status"] == "aborted"
    assert obj["abort_reason"] == "interrupted"
    assert obj["ok"] == 2
    (event,) = cli.events()
    assert event["error_kind"] == "Interrupted"
    assert event["charge"] == 2 * CHARGE
    assert not cli.open_runs().exists()
    assert "Traceback" not in done.stderr


def test_sigterm_on_a_resumed_run_counts_only_its_own_rows(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 2
    first = cli.run(*cli.map_args("job", 2))
    assert first.returncode == 0, first.stderr
    gateway.answer_first = 4
    index = cli.tmp / "job" / "results.jsonl"
    done = _stop_after(cli, cli.map_args("job", 6, "--resume"), index, 4, signal.SIGTERM)
    assert done.returncode == -signal.SIGTERM, done.stderr
    assert [(e["error_kind"], e["charge"]) for e in cli.events()] == [
        (None, 2 * CHARGE),
        ("Interrupted", 2 * CHARGE),
    ]
    assert not cli.open_runs().exists()


@pytest.mark.parametrize("as_json", [False, True], ids=["receipt", "json"])
def test_sigterm_during_ask_ends_cleanly_and_records_nothing(
    cli: Cli, gateway: Gateway, as_json: bool
) -> None:
    gateway.answer_first = 0
    args = ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hello", "--retries", "0"]
    child = cli.start(*args, *(["--json"] if as_json else []))
    _wait_for(_held(gateway, 1), "a held request", child)
    child.send_signal(signal.SIGTERM)
    out, err = child.communicate(timeout=DEADLINE)
    assert child.returncode == -signal.SIGTERM, err
    assert "Traceback" not in err
    assert cli.events() == []
    if as_json:
        obj = json.loads(out)
        assert obj["status"] == "error"
        assert obj["error"] == "interrupted"
    else:
        assert "error: interrupted" in err


def test_a_closed_stdout_does_not_fail_the_run(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 0
    child = cli.start(*cli.map_args("job", 2))
    _wait_for(_held(gateway, 1), "a held request", child)
    assert child.stdout is not None
    child.stdout.close()
    gateway.release.set()
    _, err = child.communicate(timeout=DEADLINE)
    assert child.returncode == 0, err
    (event,) = cli.events()
    assert event["status"] == "ok"
    assert event["charge"] == 2 * CHARGE
    assert "BrokenPipeError" not in err
    assert "Traceback" not in err
