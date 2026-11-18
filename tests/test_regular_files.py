"""Answers and index rows are written only to regular files.

A FIFO or a device at an output path would let another process read an
answer, or hold the write open, before the spend is recorded. Every open
here is nonblocking, so a planted FIFO can never stall the run.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.cli import main
from enmaas_cli.outputs import write_output
from enmaas_cli.runner import run_map

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")

MODEL = "rits/zai-org/glm-5-3"
USAGE = {"prompt_tokens": 5, "completion_tokens": 2}
CHARGE = 7
SECRET = "REPLY-CONTENT-7f3a"


def _reply(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": USAGE,
        },
    )


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


def _gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point main at a mock gateway and a temporary state directory. Returns the ledger."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(lambda r: _reply(SECRET))),
    )
    return tmp_path / "xdg" / "enmaas" / "runs" / "usage.jsonl"


def _ledger_rows(ledger: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]


def test_write_output_refuses_a_fifo_with_no_reader_without_blocking(tmp_path: Path) -> None:
    target = tmp_path / "answer.md"
    os.mkfifo(target)
    raised: list[BaseException] = []

    def write() -> None:
        try:
            write_output(target, "answer")
        except BaseException as exc:
            raised.append(exc)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    thread.join(5)
    if thread.is_alive():
        # Unblock the open so the thread can end, then fail.
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
        thread.join(5)
        os.close(fd)
        raise AssertionError("write_output blocked on a FIFO")
    assert len(raised) == 1 and isinstance(raised[0], OSError)


def test_write_output_refuses_a_fifo_with_a_reader(tmp_path: Path) -> None:
    target = tmp_path / "answer.md"
    with _fifo_reader(target) as reader:
        with pytest.raises(OSError, match="not a regular file"):
            write_output(target, "answer")
        assert _drain(reader) == b""


def test_an_ask_whose_output_is_a_fifo_fails_and_keeps_its_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _gateway(monkeypatch, tmp_path)
    output = tmp_path / "answer.md"
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-m", MODEL, "--dialect", "openai"]
    argv += ["-p", "hi", "--retries", "0", "-o", str(output)]
    with _fifo_reader(output) as reader:
        assert main(argv) == 1
        assert _drain(reader) == b""
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    (event,) = _ledger_rows(ledger)
    assert event["status"] == "error"
    assert event["charge"] == CHARGE


async def test_a_map_item_whose_output_is_a_fifo_fails_alone(tmp_path: Path) -> None:
    items = tmp_path / "items.jsonl"
    items.write_text("".join(json.dumps({"id": i, "x": i}) + "\n" for i in "abc"))
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    outdir = tmp_path / "out"
    outdir.mkdir()
    transport = httpx.MockTransport(lambda request: _reply("answer"))
    with _fifo_reader(outdir / "b.md") as reader:
        async with httpx.AsyncClient(transport=transport) as client:
            outcome = await run_map(
                client=client,
                base_url="https://g.test",
                key="sk-oai-x",
                model=MODEL,
                items_path=items,
                template_path=template,
                outdir=outdir,
                concurrency=1,
                retries=0,
                timeout=5,
                thinking=False,
                max_tokens=None,
                dialect="openai",
                resume=False,
            )
        assert _drain(reader) == b""
    assert (outcome.ok, outcome.failed) == (2, 1)
    assert [item_id for item_id, _ in outcome.failures] == ["b"]
    rows = [json.loads(line) for line in (outdir / "results.jsonl").read_text().splitlines()]
    assert [r["charge"] for r in rows if r.get("type") == "request"] == [CHARGE] * 3
    assert (outdir / "a.md").read_text() == (outdir / "c.md").read_text() == "answer"


ITEMS = ["a", "b", "c", "d", "e"]


def _map_files(tmp_path: Path, count: int = 3) -> tuple[Path, Path]:
    items = tmp_path / "items.jsonl"
    items.write_text("".join(json.dumps({"id": i, "x": i}) + "\n" for i in ITEMS[:count]))
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    return items, template


async def _map(tmp_path: Path, transport: httpx.AsyncBaseTransport, *, concurrency: int = 1) -> Any:
    items, template = _map_files(tmp_path)
    async with httpx.AsyncClient(transport=transport) as client:
        return await run_map(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model=MODEL,
            items_path=items,
            template_path=template,
            outdir=tmp_path / "out",
            concurrency=concurrency,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect="openai",
            resume=False,
        )


def _answering(seen: list[int]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return _reply("answer")

    return httpx.MockTransport(handle)


async def test_an_index_that_is_a_hard_link_owned_by_the_user_is_written(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    outdir.mkdir()
    backup = tmp_path / "backup.jsonl"
    backup.write_text("", encoding="utf-8")
    os.link(backup, outdir / "results.jsonl")
    outcome = await _map(tmp_path, _answering([]))
    assert outcome.abort_reason is None
    assert outcome.ok == 3
    assert len(backup.read_text(encoding="utf-8").splitlines()) == 6


async def test_an_index_owned_by_another_user_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat

    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "results.jsonl").write_text("", encoding="utf-8")
    real_fstat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        fields = list(real_fstat(fd))
        fields[stat.ST_UID] = os.getuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", fstat)
    seen: list[int] = []
    with pytest.raises(OSError, match="owned by another user"):
        await _map(tmp_path, _answering(seen))
    assert seen == []


def test_a_fifo_swapped_in_for_the_index_ends_the_run_without_blocking(tmp_path: Path) -> None:
    import asyncio

    index = tmp_path / "out" / "results.jsonl"

    def handle(request: httpx.Request) -> httpx.Response:
        if not index.exists():
            os.mkfifo(index)
        return _reply("answer")

    outcomes: list[Any] = []
    thread = threading.Thread(
        target=lambda: outcomes.append(asyncio.run(_map(tmp_path, httpx.MockTransport(handle)))),
        daemon=True,
    )
    thread.start()
    thread.join(10)
    if thread.is_alive():
        fd = os.open(index, os.O_RDONLY | os.O_NONBLOCK)
        thread.join(10)
        os.close(fd)
        raise AssertionError("the index write blocked on a FIFO")
    (outcome,) = outcomes
    assert outcome.abort_reason == "index"
    assert outcome.index_error is not None
    assert outcome.attempted == 1
    assert outcome.charged == CHARGE
    assert outcome.exit_code == 1


def _failing_third_write(monkeypatch: pytest.MonkeyPatch, index: Path) -> None:
    from enmaas_cli import runner

    real_append = runner._append_index
    writes = 0

    def append(path: Path, text: str) -> None:
        nonlocal writes
        if path == index:
            writes += 1
            if writes == 3:
                raise OSError(f"{path} is read-only")
        real_append(path, text)

    monkeypatch.setattr(runner, "_append_index", append)


@pytest.mark.parametrize("as_json", [True, False], ids=["json", "receipt"])
def test_a_failed_index_write_ends_the_map_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    from enmaas_cli import openruns

    ledger = _gateway(monkeypatch, tmp_path)
    index = tmp_path / "out" / "results.jsonl"
    _failing_third_write(monkeypatch, index)
    items = tmp_path / "items.jsonl"
    items.write_text("".join(json.dumps({"id": i, "x": i}) + "\n" for i in ITEMS))
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    argv = ["--config", str(tmp_path / "none.toml"), "map", "-m", MODEL, "--dialect", "openai"]
    argv += ["--items", str(items), "--template", str(template)]
    argv += ["--outdir", str(tmp_path / "out"), "--retries", "0", "--concurrency", "1"]
    assert main([*argv, *(["--json"] if as_json else [])]) == 1
    captured = capsys.readouterr()
    (event,) = _ledger_rows(ledger)
    assert event["error_kind"] == "IndexWriteError"
    assert event["status"] == "error"
    assert event["charge"] == 3 * CHARGE
    assert not (ledger.parent / openruns.OPEN_RUNS_FILE).exists()
    assert "Traceback" not in captured.err
    if as_json:
        obj = json.loads(captured.out)
        assert obj["status"] == "aborted"
        assert obj["abort_reason"] == "index"
        assert obj["error"] == f"{index} is read-only"
    else:
        lines = captured.out.splitlines()
        assert lines[0] == "enmaas map stopped: the index could not be written"
        assert lines[1] == f"  error     {index} is read-only"


async def test_a_signal_after_a_failed_index_write_keeps_every_billed_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from enmaas_cli import runner

    index = tmp_path / "out" / "results.jsonl"
    real_append = runner._append_index
    failed = asyncio.Event()
    writes = 0

    def append(path: Path, text: str) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            failed.set()
            raise OSError("index is read-only")
        real_append(path, text)

    monkeypatch.setattr(runner, "_append_index", append)
    requests = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            # Held until the run is cancelled. It must be the first request:
            # the other two finish without a wait, and a request that starts
            # after the failed write is never sent.
            await asyncio.Event().wait()
        return _reply("answer")

    task = asyncio.ensure_future(_map(tmp_path, httpx.MockTransport(handle), concurrency=3))
    await asyncio.wait_for(failed.wait(), 10)
    # The held request keeps the run going, so the cancel reaches a running
    # task on every Python version. A finished task would ignore it.
    assert requests == 3
    assert not task.done()
    task.cancel()
    outcome = await asyncio.wait_for(task, 10)
    assert outcome.abort_reason == "interrupted"
    # Both replies were billed, the one whose rows could not be written too.
    assert outcome.charged == 2 * CHARGE
    assert [json.loads(line)["charge"] for line in index.read_text().splitlines()[:1]] == [CHARGE]
