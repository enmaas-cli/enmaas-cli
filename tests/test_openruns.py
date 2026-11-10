"""Open-run records: written at the start of a map run, settled after a kill."""

from __future__ import annotations

import fcntl
import functools
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from enmaas_cli import budget, openruns
from enmaas_cli.cli import rolling_check
from enmaas_cli.config import Config
from enmaas_cli.openruns import OpenRun


@pytest.fixture(autouse=True)
def _usage_log_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)


@functools.cache
def _dead_pid() -> int:
    """A process id that existed and has been reaped, so the probe finds nothing."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _index(outdir: Path, rows: list[dict[str, Any]]) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    index = outdir / "results.jsonl"
    with index.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return index


def _charge(n: int, items: int = 1) -> dict[str, Any]:
    return {"type": "request", "charge": n, "items": items}


def _item(item_id: str) -> dict[str, Any]:
    return {"id": item_id, "status": "ok", "path": "x", "tokens": None, "error": None}


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": "0123456789abcdef",
        "outdir": "/nonexistent/enmaas-test",
        "offset": 0,
        "started": datetime.now(UTC).isoformat(),
        "pid": _dead_pid(),
        "model": "rits/zai-org/glm-5-3",
        "dialect": "openai",
    }
    record.update(overrides)
    return record


def _write_records(state: Path, records: list[dict[str, Any]], extra: str = "") -> Path:
    state.mkdir(parents=True, exist_ok=True)
    path = state / openruns.OPEN_RUNS_FILE
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + extra, encoding="utf-8")
    return path


def _ledger_rows(ledger: Path) -> list[dict[str, Any]]:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]


def _run_ids(state: Path) -> list[str]:
    path = state / openruns.OPEN_RUNS_FILE
    if not path.exists():
        return []
    return [json.loads(line)["run_id"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_begin_then_end_leaves_no_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    run = openruns.begin(state, outdir=tmp_path / "out", model="m", dialect="openai")
    assert run is not None
    assert (state / openruns.OPEN_RUNS_FILE).exists()
    openruns.end(state, run)
    assert not (state / openruns.OPEN_RUNS_FILE).exists()


def test_begin_records_the_run(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(5), _item("a")])
    run = openruns.begin(state, outdir=outdir, model="m", dialect="anthropic")
    assert run is not None
    assert run.outdir == os.path.abspath(outdir)
    assert run.offset == (outdir / "results.jsonl").stat().st_size
    assert run.pid == os.getpid()
    assert len(run.run_id) == 16
    assert datetime.fromisoformat(run.started).utcoffset() == timedelta(0)
    assert _run_ids(state) == [run.run_id]


def test_begin_offset_is_zero_without_an_index(tmp_path: Path) -> None:
    run = openruns.begin(tmp_path / "state", outdir=tmp_path / "out", model="m", dialect="openai")
    assert run is not None
    assert run.offset == 0


def test_files_are_mode_0600(tmp_path: Path) -> None:
    state = tmp_path / "state"
    old = os.umask(0)
    try:
        run = openruns.begin(state, outdir=tmp_path / "out", model="m", dialect="openai")
    finally:
        os.umask(old)
    assert run is not None
    for name in (openruns.OPEN_RUNS_FILE, openruns.LOCK_FILE):
        assert stat.S_IMODE((state / name).stat().st_mode) == 0o600, name


def test_every_change_holds_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    events: list[str] = []
    real_flock = fcntl.flock
    real_replace = os.replace
    real_unlink = os.unlink

    def flock(fd: int, operation: int) -> None:
        assert os.readlink(f"/proc/self/fd/{fd}") == str(state / openruns.LOCK_FILE)
        events.append(f"flock {operation}")
        real_flock(fd, operation)

    def replace(src: Any, dst: Any) -> None:
        events.append("replace")
        real_replace(src, dst)

    def unlink(path: Any) -> None:
        events.append("unlink")
        real_unlink(path)

    if not Path("/proc/self/fd").exists():
        pytest.skip("needs /proc to name a descriptor")
    dead = _record()
    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)

    run = openruns.begin(state, outdir=tmp_path / "out", model="m", dialect="openai")
    openruns.end(state, run)
    _write_records(state, [dead])
    openruns.settle(state, state / "usage.jsonl")
    lock = f"flock {fcntl.LOCK_EX}"
    assert events == [lock, "replace", lock, "unlink", lock, "unlink"]


def test_settle_writes_one_killed_event_and_removes_the_record(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(7), _item("a"), _charge(11, items=2), _item("b"), _item("c")])
    _write_records(state, [_record(outdir=str(outdir), model="rits/x", dialect="anthropic")])
    ledger = state / "usage.jsonl"
    openruns.settle(state, ledger)
    (event,) = _ledger_rows(ledger)
    assert event["command"] == "map"
    assert event["model"] == "rits/x"
    assert event["dialect"] == "anthropic"
    assert event["status"] == "error"
    assert event["error_kind"] == "Killed"
    assert event["charge"] == 18
    for name in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
        assert event[name] is None
    for name in ("items_total", "items_ok", "items_failed"):
        assert event[name] is None
    assert event["elapsed"] == 0.0
    assert event["schema_used"] is False
    assert datetime.fromisoformat(event["timestamp"]).utcoffset() is not None
    assert not (state / openruns.OPEN_RUNS_FILE).exists()


def test_rows_before_the_offset_are_not_counted(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    index = _index(outdir, [_charge(100), _item("a")])
    offset = index.stat().st_size
    _index(outdir, [_charge(3), _item("b")])
    _write_records(state, [_record(outdir=str(outdir), offset=offset)])
    openruns.settle(state, state / "usage.jsonl")
    (event,) = _ledger_rows(state / "usage.jsonl")
    assert event["charge"] == 3


def test_an_index_shorter_than_the_offset_counts_nothing(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    _index(outdir, [_charge(9)])
    run = OpenRun(**_record(outdir=str(outdir), offset=10_000))
    assert openruns.charge_since(run) == 0


def test_a_zero_charge_dead_record_is_removed_without_an_event(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(0), _item("a")])
    _write_records(state, [_record(outdir=str(outdir))])
    openruns.settle(state, state / "usage.jsonl")
    assert _ledger_rows(state / "usage.jsonl") == []
    assert not (state / openruns.OPEN_RUNS_FILE).exists()


def test_a_live_record_is_kept_and_counted(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(4), _item("a"), _charge(6), _item("b")])
    _write_records(state, [_record(outdir=str(outdir), pid=os.getppid())])
    openruns.settle(state, state / "usage.jsonl")
    assert _ledger_rows(state / "usage.jsonl") == []
    assert _run_ids(state) == ["0123456789abcdef"]
    assert openruns.live_charge(state) == 10


def test_a_live_child_process_keeps_its_record(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(4)])
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=-1)
    try:
        _write_records(state, [_record(outdir=str(outdir), pid=child.pid)])
        openruns.settle(state, state / "usage.jsonl")
        assert _run_ids(state) == ["0123456789abcdef"]
        assert openruns.live_charge(state) == 4
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)


def test_a_record_from_this_process_is_dead(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(4)])
    _write_records(state, [_record(outdir=str(outdir), pid=os.getpid())])
    assert openruns.live_charge(state) == 0
    openruns.settle(state, state / "usage.jsonl")
    (event,) = _ledger_rows(state / "usage.jsonl")
    assert event["charge"] == 4


def test_a_stale_record_with_a_live_pid_is_settled(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(8)])
    started = (datetime.now(UTC) - timedelta(hours=49)).isoformat()
    _write_records(state, [_record(outdir=str(outdir), pid=os.getppid(), started=started)])
    assert openruns.live_charge(state) == 0
    openruns.settle(state, state / "usage.jsonl")
    (event,) = _ledger_rows(state / "usage.jsonl")
    assert event["charge"] == 8
    assert not (state / openruns.OPEN_RUNS_FILE).exists()


def test_permission_error_from_the_probe_means_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def kill(pid: int, sig: int) -> None:
        raise PermissionError

    run = OpenRun(**_record(pid=12345))
    monkeypatch.setattr(os, "kill", kill)
    assert not openruns._is_dead(run, datetime.now(UTC))


def _real_index_elsewhere(tmp_path: Path) -> Path:
    return _index(tmp_path / "real", [_charge(50), _item("a")])


def test_an_index_that_is_a_link_gives_zero(tmp_path: Path) -> None:
    real = _real_index_elsewhere(tmp_path)
    outdir = tmp_path / "forged"
    outdir.mkdir()
    (outdir / "results.jsonl").symlink_to(real)
    assert openruns.charge_since(OpenRun(**_record(outdir=str(outdir)))) == 0


def test_an_index_that_is_a_directory_gives_zero(tmp_path: Path) -> None:
    outdir = tmp_path / "forged"
    (outdir / "results.jsonl").mkdir(parents=True)
    assert openruns.charge_since(OpenRun(**_record(outdir=str(outdir)))) == 0


def test_an_index_that_is_a_fifo_gives_zero_without_blocking(tmp_path: Path) -> None:
    outdir = tmp_path / "forged"
    outdir.mkdir()
    os.mkfifo(outdir / "results.jsonl")
    assert openruns.charge_since(OpenRun(**_record(outdir=str(outdir)))) == 0


def test_a_missing_index_gives_zero(tmp_path: Path) -> None:
    assert openruns.charge_since(OpenRun(**_record(outdir=str(tmp_path / "none")))) == 0


def test_negative_and_malformed_charge_rows_never_lower_the_sum(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    _index(
        outdir,
        [
            _charge(5),
            {"type": "request", "charge": -1000},
            {"type": "request", "charge": True},
            {"type": "request", "charge": 2.5},
            {"type": "request", "charge": "9"},
        ],
    )
    index = outdir / "results.jsonl"
    with index.open("a", encoding="utf-8") as handle:
        handle.write("not json\n" + "[" * 100_000 + "\n")
    with index.open("ab") as raw:
        raw.write(b"\xff\xfe\n")
    _index(outdir, [_charge(1)])
    assert openruns.charge_since(OpenRun(**_record(outdir=str(outdir)))) == 6


def test_the_index_tail_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outdir = tmp_path / "out"
    index = _index(outdir, [_charge(5)])
    first = index.stat().st_size
    _index(outdir, [_charge(7)])
    monkeypatch.setattr(openruns, "MAX_INDEX_TAIL_BYTES", first)
    assert openruns.charge_since(OpenRun(**_record(outdir=str(outdir)))) == 5


def test_garbage_lines_are_skipped(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(3)])
    good = _record(outdir=str(outdir), run_id="good")
    bad = [
        _record(offset=True),
        _record(offset=-1),
        _record(offset=1.0),
        _record(offset="0"),
        _record(pid=0),
        _record(pid=-5),
        _record(pid=True),
        _record(pid=1 << 40),
        _record(outdir="relative/out"),
        _record(outdir="/" + "a" * 5000),
        _record(outdir="/tmp/a\x00b"),
        _record(outdir=17),
        _record(started="2026-11-09T10:00:00"),
        _record(started="yesterday"),
        _record(model=None),
        _record(dialect="other"),
        _record(run_id=""),
    ]
    missing = _record()
    del missing["pid"]
    extra = "not json\n[1, 2]\nnull\n" + "[" * 100_000 + "\n"
    path = _write_records(state, [*bad, missing, good], extra=extra)
    with path.open("ab") as handle:
        handle.write(b"\xff\xfe{\n")
    assert [run.run_id for run in openruns._load(state)] == ["good"]
    openruns.settle(state, state / "usage.jsonl")
    (event,) = _ledger_rows(state / "usage.jsonl")
    assert event["charge"] == 3


def test_the_record_file_read_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    path = _write_records(state, [_record(run_id="first")])
    size = path.stat().st_size
    _write_records(state, [_record(run_id="first"), _record(run_id="second")])
    monkeypatch.setattr(openruns, "MAX_FILE_BYTES", size)
    assert [run.run_id for run in openruns._load(state)] == ["first"]


def test_a_record_file_that_is_a_link_is_not_followed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "target.jsonl"
    target.write_text(json.dumps(_record(pid=os.getppid())) + "\n", encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    (state / openruns.OPEN_RUNS_FILE).symlink_to(target)
    assert openruns.live_charge(state) == 0
    openruns.settle(state, state / "usage.jsonl")
    assert openruns.begin(state, outdir=tmp_path / "out", model="m", dialect="openai") is None
    assert target.read_text(encoding="utf-8") == before
    assert (state / openruns.OPEN_RUNS_FILE).is_symlink()


def test_a_record_file_that_is_a_fifo_does_not_block(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    os.mkfifo(state / openruns.OPEN_RUNS_FILE)
    assert openruns.live_charge(state) == 0
    openruns.settle(state, state / "usage.jsonl")


def test_a_ledger_that_cannot_be_written_keeps_the_record(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(5)])
    _write_records(state, [_record(outdir=str(outdir))])
    ledger = state / "usage.jsonl"
    ledger.mkdir()
    openruns.settle(state, ledger)
    assert _run_ids(state) == ["0123456789abcdef"]


def test_settle_keeps_live_records_and_removes_dead_ones(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(5)])
    live = _record(outdir=str(outdir), pid=os.getppid(), run_id="live")
    dead = _record(outdir=str(outdir), run_id="dead")
    _write_records(state, [live, dead])
    openruns.settle(state, state / "usage.jsonl")
    assert _run_ids(state) == ["live"]
    assert len(_ledger_rows(state / "usage.jsonl")) == 1
    assert stat.S_IMODE((state / openruns.OPEN_RUNS_FILE).stat().st_mode) == 0o600


def test_no_usage_log_turns_it_all_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(5)])
    path = _write_records(
        state,
        [_record(outdir=str(outdir), run_id="dead"), _record(outdir=str(outdir), pid=os.getppid())],
    )
    before = path.read_text(encoding="utf-8")
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    assert openruns.begin(state, outdir=outdir, model="m", dialect="openai") is None
    openruns.settle(state, state / "usage.jsonl")
    assert openruns.live_charge(state) == 0
    openruns.end(state, OpenRun(**_record(run_id="dead")))
    assert path.read_text(encoding="utf-8") == before
    assert not (state / "usage.jsonl").exists()
    assert not (state / openruns.LOCK_FILE).exists()


def test_begin_warns_and_returns_none_when_it_cannot_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    state.write_text("a file, not a directory", encoding="utf-8")
    assert openruns.begin(state, outdir=tmp_path / "out", model="m", dialect="openai") is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "warning: the open-run record could not be written; "
        "if this run is killed its spend is not counted\n"
    )


def test_end_suppresses_os_errors(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.write_text("a file, not a directory", encoding="utf-8")
    openruns.end(state, OpenRun(**_record()))


def test_end_leaves_other_records(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = openruns.begin(state, outdir=tmp_path / "a", model="m", dialect="openai")
    second = openruns.begin(state, outdir=tmp_path / "b", model="m", dialect="openai")
    assert first is not None and second is not None
    openruns.end(state, first)
    assert _run_ids(state) == [second.run_id]
    openruns.end(state, None)
    assert _run_ids(state) == [second.run_id]


def test_nothing_read_is_ever_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    secret = "SECRET-ROW-CONTENT-sk-oai-123"
    _index(outdir, [_charge(5), {"id": "a", "status": "ok", "error": secret}])
    with (outdir / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(secret + "\n")
    _write_records(
        state,
        [_record(outdir=str(outdir)), _record(outdir=str(outdir), pid=os.getppid())],
        extra=secret + "\n",
    )
    assert openruns.live_charge(state) == 5
    openruns.settle(state, state / "usage.jsonl")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert secret not in (state / "usage.jsonl").read_text(encoding="utf-8")
    assert secret not in (state / openruns.OPEN_RUNS_FILE).read_text(encoding="utf-8")


def test_rolling_check_counts_a_live_record(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(60)])
    _write_records(state, [_record(outdir=str(outdir), pid=os.getppid())])
    config = Config(budget_tokens=100, budget_window_days=7)
    check = rolling_check(config, state / "usage.jsonl")
    assert check.spent == 60
    assert check.message is None
    _index(outdir, [_charge(40)])
    check = rolling_check(config, state / "usage.jsonl")
    assert check.spent == 100
    assert check.message is not None


HUGE = int("9" * 4300)
SKIPPED_WARNING = (
    "warning: an open-run record could not be read and was dropped; "
    "its spend is not counted (RuntimeError)\n"
)


def test_a_charge_above_the_cap_is_skipped_when_settling(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(HUGE), _charge(HUGE), _charge(21)])
    _write_records(state, [_record(outdir=str(outdir))])
    openruns.settle(state, state / "usage.jsonl")
    (event,) = _ledger_rows(state / "usage.jsonl")
    assert event["error_kind"] == "Killed"
    assert event["charge"] == 21
    assert not (state / openruns.OPEN_RUNS_FILE).exists()


def test_a_charge_above_the_cap_is_skipped_in_live_charge(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outdir = tmp_path / "out"
    _index(outdir, [_charge(HUGE), _charge(HUGE), _charge(21)])
    _write_records(state, [_record(outdir=str(outdir), pid=os.getppid())])
    assert openruns.live_charge(state) == 21


def _main_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point main at a temporary state directory and a mock gateway. Returns the state dir."""
    import httpx

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    return tmp_path / "xdg" / "enmaas" / "runs"


def _budget_config(tmp_path: Path, tokens: int = 100) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(f"[budget]\nwindow_days = 7\ntokens = {tokens}\n", encoding="utf-8")
    return config


ASK = ["ask", "-m", "rits/zai-org/glm-5-3", "--dialect", "openai", "-p", "hi", "--retries", "0"]


def test_ask_under_a_rolling_budget_survives_huge_charge_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from enmaas_cli.cli import main

    state = _main_env(tmp_path, monkeypatch)
    live_out = tmp_path / "live"
    dead_out = tmp_path / "dead"
    _index(live_out, [_charge(HUGE), _charge(HUGE), _charge(21)])
    _index(dead_out, [_charge(HUGE), _charge(HUGE), _charge(21)])
    _write_records(
        state,
        [
            _record(outdir=str(live_out), pid=os.getppid(), run_id="live"),
            _record(outdir=str(dead_out), run_id="dead"),
        ],
    )
    assert main(["--config", str(_budget_config(tmp_path)), *ASK]) == 0
    capsys.readouterr()
    charges = [row["charge"] for row in _ledger_rows(state / "usage.jsonl")]
    assert charges == [21, 7]


def test_a_record_whose_handling_raises_is_dropped_and_the_rest_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    _index(tmp_path / "a", [_charge(3)])
    _index(tmp_path / "b", [_charge(5)])
    _write_records(
        state,
        [
            _record(outdir=str(tmp_path / "a"), run_id="bad"),
            _record(outdir=str(tmp_path / "b"), run_id="good"),
        ],
    )
    real = budget.charge_from_rows
    calls = 0

    def charge_from_rows(rows: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret text from a row")
        return real(rows)

    monkeypatch.setattr(openruns, "charge_from_rows", charge_from_rows)
    openruns.settle(state, state / "usage.jsonl")
    assert [row["charge"] for row in _ledger_rows(state / "usage.jsonl")] == [5]
    assert not (state / openruns.OPEN_RUNS_FILE).exists()
    captured = capsys.readouterr()
    assert captured.err == SKIPPED_WARNING
    assert "secret" not in captured.err


def test_a_live_record_whose_handling_raises_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    _index(tmp_path / "a", [_charge(3)])
    _index(tmp_path / "b", [_charge(5)])
    _write_records(
        state,
        [
            _record(outdir=str(tmp_path / "a"), run_id="bad", pid=os.getppid()),
            _record(outdir=str(tmp_path / "b"), run_id="good", pid=os.getppid()),
        ],
    )
    real = budget.charge_from_rows
    calls = 0

    def charge_from_rows(rows: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret text from a row")
        return real(rows)

    monkeypatch.setattr(openruns, "charge_from_rows", charge_from_rows)
    assert openruns.live_charge(state) == 5
    captured = capsys.readouterr()
    assert captured.err == SKIPPED_WARNING
    assert _run_ids(state) == ["bad", "good"]
