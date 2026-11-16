"""Real command refusal and recovery with a counting loopback gateway."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from enmaas_cli import openruns
from enmaas_cli.cli import main
from enmaas_cli.runner import _charge_row


@pytest.fixture
def local_gateway() -> Iterator[tuple[str, list[str]]]:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(self.path)
            body = json.dumps(
                {
                    "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith(("ENMAAS", "CLAUDE", "CODEX", "XDG")):
            monkeypatch.delenv(name)
    for name in (
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        path = tmp_path / name.lower()
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("ENMAAS_KEY", "fake-openruns-test")
    state = tmp_path / "xdg_state_home" / "enmaas" / "runs"
    state.mkdir(parents=True)
    return state


def _record(outdir: Path, run_id: str, charges: list[int]) -> dict[str, Any]:
    outdir.mkdir()
    (outdir / "results.jsonl").write_text(
        "\n".join(_charge_row(n, items=1, run=run_id) for n in charges), encoding="utf-8"
    )
    record = asdict(
        openruns.OpenRun(
            run_id=run_id,
            outdir=str(outdir),
            offset=0,
            started=datetime.now(UTC).isoformat(),
            pid=os.getpid(),
            model="gpt-test",
            dialect="openai",
        )
    )
    record["future"] = {"private-content": "retained"}
    return record


def _argv(tmp_path: Path, command: str, gateway: str) -> list[str]:
    config = tmp_path / "config.toml"
    config.write_text(
        f'base_url = "{gateway}"\n[budget]\nwindow_days = 7\ntokens = 1000\n',
        encoding="utf-8",
    )
    args = [
        "--config",
        str(config),
        command,
        "-m",
        "gpt-test",
        "--dialect",
        "openai",
        "--retries",
        "0",
    ]
    if command == "ask":
        return [*args, "-p", "hi"]
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "text": "hi"}\n', encoding="utf-8")
    template = tmp_path / "template.txt"
    template.write_text("{text}", encoding="utf-8")
    return [
        *args,
        "--items",
        str(items),
        "--template",
        str(template),
        "--outdir",
        str(tmp_path / "new-out"),
        "--concurrency",
        "1",
    ]


@pytest.mark.parametrize("command", ["ask", "map"])
@pytest.mark.parametrize("problem", ["capped", "malformed mixed"])
def test_budgeted_commands_preserve_incomplete_state_and_recover_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    local_gateway: tuple[str, list[str]],
    command: str,
    problem: str,
) -> None:
    state = _isolated_state(tmp_path, monkeypatch)
    gateway, seen = local_gateway
    args = _argv(tmp_path, command, gateway)
    record_file = state / openruns.OPEN_RUNS_FILE
    ledger = state / "usage.jsonl"
    outdir = tmp_path / "old-out"
    if problem == "capped":
        record = _record(outdir, "old", [5, 7])
        raw = (json.dumps(record) + "\n").encode()
        monkeypatch.setattr(openruns, "MAX_READ_BYTES", len(_charge_row(5, items=1, run="old")) + 1)
        expected = [12]
    else:
        record = _record(outdir, "old", [5])
        unfinished = _record(tmp_path / "second-out", "second", [11])
        raw = (json.dumps(record) + "\n" + json.dumps(unfinished)[:-1]).encode()
        expected = [5, 11]
    record_file.write_bytes(raw)
    for _ in range(2):
        assert main(args) == 3
        output = capsys.readouterr()
        expected_message = (
            f"open-run records in {record_file} could not all be read, so the token budget "
            "cannot be checked; records were retained; restore accounting before budgeted "
            "work can resume"
        )
        assert output.err == f"enmaas {command} refused\n  budget    {expected_message[:200]}\n"
        assert "private-content" not in output.err
        assert seen == []
        assert record_file.read_bytes() == raw
        assert not ledger.exists()

    if problem == "capped":
        monkeypatch.setattr(openruns, "MAX_READ_BYTES", (outdir / "results.jsonl").stat().st_size)
    else:
        # Complete the original record. Every original charge row is kept.
        record_file.write_bytes(raw + b"}\n")
    assert main(args) == 0
    capsys.readouterr()
    assert len(seen) == 1
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert sorted(row["charge"] for row in rows if row["error_kind"] == "Killed") == expected
    assert sum(row["charge"] for row in rows) == sum(expected) + 7
    assert not record_file.exists()
    before = ledger.read_bytes()
    openruns.settle(state, ledger)
    assert ledger.read_bytes() == before
