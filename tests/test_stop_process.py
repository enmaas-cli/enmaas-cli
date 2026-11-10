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

    def start(
        self,
        *args: str,
        stdout: int = subprocess.PIPE,
        preexec_fn: Callable[[], Any] | None = None,
        program: str = "from enmaas_cli.cli import main; raise SystemExit(main())",
    ) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys.executable, "-c", program] + list(args),
            env=self.env(),
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=preexec_fn,
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


@pytest.mark.parametrize("concurrency", ["1", "4"])
def test_sigterm_records_the_run_and_ends_by_the_signal(
    cli: Cli, gateway: Gateway, concurrency: str
) -> None:
    gateway.answer_first = 2
    index = cli.tmp / "job" / "results.jsonl"
    args = cli.map_args("job", 5, "--concurrency", concurrency)
    done = _stop_after(cli, args, index, 2, signal.SIGTERM)
    assert done.returncode == -signal.SIGTERM, done.stderr
    (event,) = cli.events()
    assert event["error_kind"] == "Interrupted"
    assert event["status"] == "error"
    assert event["charge"] == sum(_charges(index)) == 2 * CHARGE
    assert "enmaas map interrupted" in done.stdout
    assert "re-run with --resume" in done.stdout
    assert not cli.open_runs().exists()
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize("concurrency", ["1", "4"])
def test_sigint_with_json_prints_one_object(cli: Cli, gateway: Gateway, concurrency: str) -> None:
    gateway.answer_first = 2
    index = cli.tmp / "job" / "results.jsonl"
    args = cli.map_args("job", 5, "--json", "--concurrency", concurrency)
    done = _stop_after(cli, args, index, 2, signal.SIGINT)
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


def _reset(gateway: Gateway, answer_first: int) -> None:
    """Start a fresh round: no request seen, every later one held."""
    gateway.release.set()
    with gateway.lock:
        gateway.release = threading.Event()
        gateway.arrived = 0
        gateway.held = 0
        gateway.answer_first = answer_first


def _signal_after_release(
    cli: Cli, gateway: Gateway, run: int, *, late: bool
) -> tuple[subprocess.Popen[str], int, str, str, list[dict[str, Any]]]:
    """One map of four held requests, signalled once the replies are released."""
    signum = signal.SIGTERM if run % 2 else signal.SIGINT
    _reset(gateway, 0)
    before = len(cli.events())
    args = cli.map_args(f"run{run}", 4, "--json", "--concurrency", "4")
    child = cli.start(*args, program=REPORTING_PROGRAM) if late else cli.start(*args)
    _wait_for(_held(gateway, 4), "four held requests", child)
    gateway.release.set()
    if late:
        index = cli.tmp / f"run{run}" / "results.jsonl"
        _wait_for(lambda: len(_charges(index)) >= 4, "4 charge rows", child)
    child.send_signal(signum)
    out, err = child.communicate(timeout=DEADLINE)
    # After main has returned, a SIGINT meets Python's default handler and the
    # interpreter prints a KeyboardInterrupt traceback. Only that is allowed.
    assert "Traceback" not in err.split("main returned")[0], err
    return child, signum, out, err, cli.events()[before:]


# Writes what main returned before the interpreter exits, so a test can tell
# the CLI's own exit decision from a signal that lands during interpreter exit.
REPORTING_PROGRAM = (
    "import os\n"
    "from enmaas_cli.cli import main\n"
    "code = main()\n"
    "os.write(2, b'main returned %d\\n' % code)\n"
    "raise SystemExit(code)\n"
)


def test_a_signal_after_the_replies_gives_one_output_that_agrees_with_the_ledger(
    cli: Cli, gateway: Gateway
) -> None:
    for run in range(20):
        child, signum, out, err, events = _signal_after_release(cli, gateway, run, late=False)
        obj = json.loads(out)
        assert isinstance(obj, dict)
        assert len(events) == 1, (run, child.returncode, events)
        (event,) = events
        if child.returncode == -signum:
            assert event["error_kind"] == "Interrupted", (run, event)
            assert obj["abort_reason"] == "interrupted"
        else:
            assert child.returncode == 0, (run, child.returncode, err)
            assert event["status"] == "ok" and event["error_kind"] is None, (run, event)
            assert obj["status"] == "ok"


def test_a_signal_after_the_work_is_recorded_changes_nothing(cli: Cli, gateway: Gateway) -> None:
    """The signal lands while the child writes its ledger event, prints, or exits.

    Every run prints one JSON object and writes one ledger event, and the two
    agree. An interrupted run ends by the signal. Once the command has put
    the previous handlers back on its way out, a signal takes its default
    action, so a finished run can still end by it, and a SIGINT after main
    returned prints the interpreter's KeyboardInterrupt traceback; the
    ledger and the output are complete by then. When it exits normally,
    main itself returned 0. The exact orders are pinned without timing in
    the tests below.
    """
    for run in range(8):
        child, signum, out, err, events = _signal_after_release(cli, gateway, run, late=True)
        obj = json.loads(out)
        assert isinstance(obj, dict)
        assert len(events) == 1, (run, child.returncode, events)
        (event,) = events
        if event["error_kind"] == "Interrupted":
            assert obj["abort_reason"] == "interrupted"
            assert child.returncode == -signum, (run, child.returncode, err)
            assert "main returned" not in err
        else:
            assert event["status"] == "ok" and event["error_kind"] is None, (run, event)
            assert obj["status"] == "ok"
            assert child.returncode in (0, -signum), (run, child.returncode, err)
            if child.returncode == 0:
                assert "main returned 0\n" in err, (run, err)


def test_a_child_that_ignores_sigint_finishes_its_map(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 0

    def ignore_sigint() -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    child = cli.start(*cli.map_args("job", 2), preexec_fn=ignore_sigint)
    _wait_for(_held(gateway, 1), "a held request", child)
    child.send_signal(signal.SIGINT)
    gateway.release.set()
    out, err = child.communicate(timeout=DEADLINE)
    assert child.returncode == 0, err
    assert out.startswith("enmaas map ok")
    (event,) = cli.events()
    assert event["status"] == "ok"
    assert event["charge"] == 2 * CHARGE


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_a_second_signal_ends_a_process_blocked_on_the_ledger(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 1
    cli.state.mkdir(parents=True)
    os.mkfifo(cli.ledger)
    answer = cli.tmp / "answer.md"
    args = ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hello", "--retries", "0"]
    child = cli.start(*args, "-o", str(answer))
    # The answer is written before the ledger, and nothing awaits in between,
    # so once it exists the child is on its way to the blocking ledger open.
    _wait_for(answer.exists, "the answer file", child)
    child.send_signal(signal.SIGTERM)
    with pytest.raises(subprocess.TimeoutExpired):
        child.wait(timeout=1)
    child.send_signal(signal.SIGTERM)
    _, err = child.communicate(timeout=DEADLINE)
    assert child.returncode == -signal.SIGTERM
    assert "Traceback" not in err


def test_two_quick_sigints_still_count_the_run(cli: Cli, gateway: Gateway) -> None:
    gateway.answer_first = 2
    index = cli.tmp / "job" / "results.jsonl"
    child = cli.start(*cli.map_args("job", 5))
    _wait_for(lambda: len(_charges(index)) >= 2, "2 charge rows", child)
    _wait_for(_held(gateway, 1), "a held request", child)
    child.send_signal(signal.SIGINT)
    time.sleep(0.05)
    child.send_signal(signal.SIGINT)
    _, err = child.communicate(timeout=DEADLINE)
    assert child.returncode == -signal.SIGINT, err
    assert "Traceback" not in err

    gateway.release.set()
    done = cli.run("ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0")
    assert done.returncode == 0, done.stderr
    counted = [e["charge"] for e in cli.events() if e["error_kind"] in ("Interrupted", "Killed")]
    # Once, or twice in the documented window between the ledger write and
    # the record removal. Never zero.
    assert sum(counted) in (2 * CHARGE, 4 * CHARGE), counted
    assert not cli.open_runs().exists()


def _record_handlers() -> dict[int, Any]:
    return {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}


def test_main_in_process_leaves_the_signal_handlers_as_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from enmaas_cli import cli as cli_module

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def custom(signum: int, frame: object) -> None:
        raise AssertionError("not expected to run")

    seen: list[dict[int, Any]] = []
    real_dispatch = cli_module._dispatch

    async def dispatch(*args: Any, **kwargs: Any) -> int:
        seen.append(_record_handlers())
        return await real_dispatch(*args, **kwargs)

    monkeypatch.setattr(cli_module, "_dispatch", dispatch)
    previous = signal.signal(signal.SIGTERM, custom)
    try:
        before = _record_handlers()
        assert cli_module.main(["--config", str(tmp_path / "none.toml"), "usage"]) == 0
        assert _record_handlers() == before
        # While the command ran, both signals had the command's handler.
        (during,) = seen
        assert during[signal.SIGTERM] is not custom
        assert during[signal.SIGINT] is not before[signal.SIGINT]

        seen.clear()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        before = _record_handlers()
        assert cli_module.main(["--config", str(tmp_path / "none.toml"), "usage"]) == 0
        assert _record_handlers() == before
        # An ignored signal is left ignored while the command runs.
        assert seen[0][signal.SIGTERM] is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, previous)
    capsys.readouterr()


# A child that runs main with _dispatch replaced, so a test can place a
# signal exactly before or after the result is reported. SCENARIO picks it.
SCENARIO_PROGRAM = """
import asyncio, os, signal, sys
from enmaas_cli import cli

SCENARIO = sys.argv[1]


async def dispatch(args, config, base_url, key, final):
    if SCENARIO == "raised-after-result":
        # The signal arrives before the result, and the cancel it caused is
        # raised only after the result, as a library that defers a cancel
        # can do.
        os.kill(os.getpid(), signal.SIGTERM)
        final.code = 0
        print("result", flush=True)
        asyncio.current_task().cancel()
        await asyncio.sleep(5)
        raise AssertionError("not cancelled")
    if SCENARIO in ("absorbed", "absorbed-interrupted"):
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            # Absorbed, as a shielded close in a client library can do.
            pass
        final.code = 3 if SCENARIO == "absorbed-interrupted" else 0
        final.interrupted = SCENARIO == "absorbed-interrupted"
        print("result", flush=True)
        return final.code
    if SCENARIO == "after-result":
        final.code = 0
        print("result", flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.01)
        return 0
    raise AssertionError(SCENARIO)


cli._dispatch = dispatch
code = cli.main(["--config", "none.toml", "usage"])
os.write(2, b"main returned %d\\n" % code)
raise SystemExit(code)
"""


@pytest.mark.parametrize(
    ("scenario", "returncode"),
    [
        ("after-result", 0),
        ("raised-after-result", 0),
        ("absorbed", 0),
        ("absorbed-interrupted", -signal.SIGTERM),
    ],
)
def test_the_printed_result_decides_how_the_process_ends(
    cli: Cli, scenario: str, returncode: int
) -> None:
    child = cli.start(scenario, program=SCENARIO_PROGRAM)
    out, err = child.communicate(timeout=DEADLINE)
    assert child.returncode == returncode, err
    assert out == "result\n"
    assert "Traceback" not in err
    if returncode == 0:
        assert err.endswith("main returned 0\n"), err
