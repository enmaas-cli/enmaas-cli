"""Record files are private, and are read and written only as regular files.

The index, the usage ledger and the open-run file name the user's work and
what it cost. They are created readable by the owner only, in a state
directory only the owner can enter. A link or a FIFO planted at the ledger
path would redirect a write or block a command, so every open is
nonblocking, refuses a link, and checks the file type on the descriptor.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

import enmaas_cli
from enmaas_cli import openruns
from enmaas_cli.cli import main
from enmaas_cli.runner import _append_index
from enmaas_cli.usage import (
    UsageEvent,
    append_event,
    load_events_counted,
    load_events_strict,
)

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")

MODEL = "rits/zai-org/glm-5-3"
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "the answer"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}
UNTRACKED = (
    "warning: a token budget is configured but this run's usage was "
    "not recorded, so it will not count against the ceiling"
)
# Long enough that a blocked open is a hang, not a slow machine.
DEADLINE = 10.0


def _event() -> UsageEvent:
    return UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="m",
        dialect="openai",
        input_tokens=5,
        cached_tokens=0,
        output_tokens=2,
        reasoning_tokens=None,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
        charge=7,
    )


@contextlib.contextmanager
def _umask(mask: int) -> Iterator[None]:
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@contextlib.contextmanager
def _fifo_reader(path: Path) -> Iterator[int]:
    """A FIFO at `path` with a reader, so a blocking write to it would succeed."""
    os.mkfifo(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        yield fd
    finally:
        os.close(fd)


def _drain(fd: int) -> bytes:
    try:
        return os.read(fd, 1 << 16)
    except BlockingIOError:
        return b""


def _within_deadline(work: Callable[[], Any], unblock: Callable[[], None]) -> Any:
    """Run `work` in a thread. Fail when it is still running at the deadline.

    `unblock` releases a blocked open so the thread can end before the test fails.
    """
    result: list[Any] = []
    thread = threading.Thread(target=lambda: result.append(work()), daemon=True)
    thread.start()
    thread.join(DEADLINE)
    if thread.is_alive():
        unblock()
        thread.join(DEADLINE)
        raise AssertionError("the call blocked on a FIFO")
    return result[0]


def _gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on_request: Callable[[], None] | None = None
) -> tuple[Path, list[httpx.Request]]:
    """Point main at a mock gateway. Returns the state directory and the requests sent."""
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
    return tmp_path / "xdg" / "enmaas" / "runs", seen


def _budget(tmp_path: Path) -> list[str]:
    config = tmp_path / "config.toml"
    config.write_text("[budget]\nwindow_days = 7\ntokens = 1000\n", encoding="utf-8")
    return ["--config", str(config)]


ASK = ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"]


# File modes.


def test_a_new_ledger_is_mode_600_under_umask_022(tmp_path: Path) -> None:
    ledger = tmp_path / "state" / "usage.jsonl"
    with _umask(0o022):
        assert append_event(ledger, _event())
    assert _mode(ledger) == 0o600


def test_an_existing_ledger_keeps_its_mode(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("", encoding="utf-8")
    ledger.chmod(0o640)
    assert append_event(ledger, _event())
    assert _mode(ledger) == 0o640


def test_a_new_index_is_mode_600_under_umask_022(tmp_path: Path) -> None:
    index = tmp_path / "results.jsonl"
    with _umask(0o022):
        _append_index(index, "row\n")
    assert _mode(index) == 0o600


def test_an_existing_index_keeps_its_mode(tmp_path: Path) -> None:
    index = tmp_path / "results.jsonl"
    index.write_text("", encoding="utf-8")
    index.chmod(0o644)
    _append_index(index, "row\n")
    assert _mode(index) == 0o644


def test_the_open_run_file_is_mode_600_under_umask_022(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with _umask(0o022):
        run = openruns.begin(state, outdir=tmp_path / "out", model="m", dialect="openai")
    assert run is not None
    assert _mode(state / openruns.OPEN_RUNS_FILE) == 0o600


def test_the_ledger_append_creates_a_private_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "xdg" / "enmaas" / "runs"
    with _umask(0o022):
        assert append_event(state / "usage.jsonl", _event())
    assert _mode(state) == 0o700


def test_record_creates_a_private_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    argv = ["--config", str(tmp_path / "none.toml"), "record", "--model", "m", "--input", "5"]
    argv += ["--command", "other-tool", "--output", "2"]
    with _umask(0o022):
        assert main(argv) == 0
    state = tmp_path / "xdg" / "enmaas" / "runs"
    assert _mode(state) == 0o700
    assert _mode(state / "usage.jsonl") == 0o600


def test_ask_with_the_ledger_off_creates_a_private_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, _ = _gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    with _umask(0o022):
        assert main(["--config", str(tmp_path / "none.toml"), *ASK]) == 0
    capsys.readouterr()
    assert _mode(state) == 0o700


def test_a_map_run_writes_a_private_index_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, _ = _gateway(tmp_path, monkeypatch)
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n', encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    outdir = tmp_path / "out"
    argv = ["--config", str(tmp_path / "none.toml"), "map", "-m", MODEL, "--dialect", "openai"]
    argv += ["--items", str(items), "--template", str(template), "--outdir", str(outdir)]
    with _umask(0o022):
        assert main([*argv, "--retries", "0"]) == 0
    capsys.readouterr()
    assert _mode(state) == 0o700
    assert _mode(state / "usage.jsonl") == 0o600
    assert _mode(outdir / "results.jsonl") == 0o600


# The ledger writer.


def test_a_link_at_the_ledger_path_is_not_written_through(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep me\n", encoding="utf-8")
    ledger = tmp_path / "usage.jsonl"
    ledger.symlink_to(target)
    assert append_event(ledger, _event()) is False
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_a_fifo_with_no_reader_at_the_ledger_path_is_refused_without_blocking(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "usage.jsonl"
    os.mkfifo(ledger)

    def unblock() -> None:
        fd = os.open(ledger, os.O_RDONLY | os.O_NONBLOCK)
        threading.Timer(DEADLINE, os.close, (fd,)).start()

    assert _within_deadline(lambda: append_event(ledger, _event()), unblock) is False


def test_a_fifo_with_a_reader_at_the_ledger_path_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    with _fifo_reader(ledger) as reader:
        assert append_event(ledger, _event()) is False
        assert _drain(reader) == b""


def test_a_fifo_at_the_ledger_after_the_check_does_not_block_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A FIFO that appears while the request is in flight: the answer is kept and the user told."""
    readers: list[int] = []

    def plant() -> None:
        ledger = tmp_path / "xdg" / "enmaas" / "runs" / "usage.jsonl"
        with contextlib.suppress(FileNotFoundError):
            ledger.unlink()
        os.mkfifo(ledger)
        # A reader, so a blocking open would succeed and write into it.
        readers.append(os.open(ledger, os.O_RDONLY | os.O_NONBLOCK))

    _, seen = _gateway(tmp_path, monkeypatch, on_request=plant)
    output = tmp_path / "answer.md"
    try:
        assert main([*_budget(tmp_path), *ASK, "-o", str(output)]) == 0
        assert _drain(readers[0]) == b""
    finally:
        for fd in readers:
            os.close(fd)
    assert len(seen) == 1
    assert output.read_text(encoding="utf-8") == "the answer"
    assert UNTRACKED in capsys.readouterr().err.splitlines()


# The ledger readers.


def _blocked_reader(ledger: Path) -> Callable[[], None]:
    """Open and close a writer, so a reader blocked on the FIFO sees end of file."""

    def unblock() -> None:
        os.close(os.open(ledger, os.O_WRONLY))

    return unblock


def test_the_strict_reader_names_a_fifo_without_blocking(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    os.mkfifo(ledger)
    events, problems = _within_deadline(lambda: load_events_strict(ledger), _blocked_reader(ledger))
    assert events == []
    assert problems == [f"{ledger} is not a regular file"]


def test_the_lenient_reader_returns_no_rows_for_a_fifo_without_blocking(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    os.mkfifo(ledger)
    read = _within_deadline(lambda: load_events_counted(ledger), _blocked_reader(ledger))
    assert read.events == []
    assert read.skipped == 0
    assert read.not_regular is True


def test_the_strict_reader_refuses_a_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(json.dumps({"x": 1}) + "\n", encoding="utf-8")
    ledger = tmp_path / "usage.jsonl"
    ledger.symlink_to(target)
    assert load_events_strict(ledger) == ([], [f"{ledger} is not a regular file"])
    assert load_events_counted(ledger).not_regular is True


def test_the_readers_still_read_a_regular_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    assert append_event(ledger, _event())
    events, problems = load_events_strict(ledger)
    assert problems == []
    assert [event.charge for event in events] == [7]
    read = load_events_counted(ledger)
    assert (len(read.events), read.skipped, read.not_regular) == (1, 0, False)


def test_a_missing_ledger_is_still_empty_for_both_readers(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    assert load_events_strict(ledger) == ([], [])
    assert tuple(load_events_counted(ledger)) == ([], 0, False)


def _cli(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run the console script in a child process, killed at the deadline.

    A child, because a command blocked on a FIFO cannot be stopped from a
    thread in this process.
    """
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "ENMAAS_KEY": "sk-oai-x",
        # No request may be sent, so the gateway is never reached.
        "ENMAAS_BASE_URL": "https://gw.example.test",
        "PATH": "/usr/bin:/bin",
        # The tree under test, not whatever copy of the package the
        # interpreter would find on its own.
        "PYTHONPATH": str(Path(enmaas_cli.__file__).resolve().parents[1]),
    }
    return subprocess.run(
        [sys.executable, "-c", "from enmaas_cli.cli import entry; entry()", *argv],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _fifo_ledger(tmp_path: Path) -> Path:
    state = tmp_path / "xdg" / "enmaas" / "runs"
    state.mkdir(parents=True)
    ledger = state / "usage.jsonl"
    os.mkfifo(ledger)
    return ledger


def test_a_budgeted_ask_refuses_a_fifo_ledger_and_names_it(tmp_path: Path) -> None:
    ledger = _fifo_ledger(tmp_path)
    done = _cli(tmp_path, *_budget(tmp_path), *ASK, "-o", str(tmp_path / "a.md"))
    assert done.returncode == 3
    assert f"{ledger} is not a regular file" in done.stderr
    assert not (tmp_path / "a.md").exists()


def test_usage_warns_about_a_fifo_ledger_and_exits_0(tmp_path: Path) -> None:
    ledger = _fifo_ledger(tmp_path)
    done = _cli(tmp_path, "--config", str(tmp_path / "none.toml"), "usage")
    assert done.returncode == 0
    assert done.stderr.splitlines() == [
        f"warning: {ledger} is not a regular file, so no usage was read from it"
    ]
    assert "No calls recorded" in done.stdout


def test_an_unbudgeted_ask_with_a_fifo_ledger_writes_its_answer_and_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, seen = _gateway(tmp_path, monkeypatch)
    state.mkdir(parents=True)
    ledger = state / "usage.jsonl"
    output = tmp_path / "answer.md"
    with _fifo_reader(ledger) as reader:
        code = main(["--config", str(tmp_path / "none.toml"), *ASK, "-o", str(output)])
        assert _drain(reader) == b""
    capsys.readouterr()
    assert code == 0
    assert len(seen) == 1
    assert output.read_text(encoding="utf-8") == "the answer"
