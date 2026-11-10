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
