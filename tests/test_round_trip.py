"""What enmaas writes, its own readers accept.

Each writer of the index, the ledger and the open-run file is driven at the
largest values a hostile reply or a command-line argument can produce. The
rows it leaves are read back by every reader of that file, and a budgeted
`ask` that follows refuses on the budget, never because a file it wrote
could not be read.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import openruns, runner
from enmaas_cli.budget import charge_from_rows
from enmaas_cli.cli import main
from enmaas_cli.tokens import MAX_TOKEN_COUNT
from enmaas_cli.usage import UsageEvent, append_event, load_events, load_events_strict

MODEL = "rits/zai-org/glm-5-3"
ASK = ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"]
# Every count a reply can report, each at the largest value that is read.
HOSTILE_USAGE = {
    "prompt_tokens": MAX_TOKEN_COUNT,
    "completion_tokens": MAX_TOKEN_COUNT,
    "prompt_tokens_details": {"cached_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": MAX_TOKEN_COUNT},
}


def _recorded_cap() -> int:
    from enmaas_cli.tokens import MAX_RECORDED

    return MAX_RECORDED


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.state = tmp_path / "xdg" / "enmaas" / "runs"
        self.ledger = self.state / "usage.jsonl"
        self.config = tmp_path / "none.toml"

    def budget_config(self, tokens: int) -> Path:
        path = self.tmp / "budget.toml"
        path.write_text(f"[budget]\nwindow_days = 7\ntokens = {tokens}\n", encoding="utf-8")
        return path

    def map_argv(self, name: str, count: int) -> list[str]:
        items = self.tmp / f"{name}-items.jsonl"
        items.write_text(
            "".join(json.dumps({"id": f"i{n}", "x": n}) + "\n" for n in range(count)),
            encoding="utf-8",
        )
        template = self.tmp / "t.txt"
        template.write_text("do {x}", encoding="utf-8")
        argv = ["--config", str(self.config), "map", "-m", MODEL, "--dialect", "openai"]
        argv += ["--items", str(items), "--template", str(template)]
        return [*argv, "--outdir", str(self.tmp / name), "--retries", "0"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": HOSTILE_USAGE,
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    return Env(tmp_path)


def _event(**counts: int) -> UsageEvent:
    from datetime import UTC, datetime

    return UsageEvent(
        timestamp=datetime.now(UTC),
        command="map",
        model=MODEL,
        dialect="openai",
        input_tokens=counts.get("input_tokens"),
        cached_tokens=counts.get("cached_tokens"),
        output_tokens=counts.get("output_tokens"),
        reasoning_tokens=counts.get("reasoning_tokens"),
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
        charge=counts.get("charge"),
    )


def _write_ask(env: Env) -> None:
    assert main(["--config", str(env.config), *ASK]) == 0


def _write_map(env: Env) -> None:
    assert main(env.map_argv("job", 3)) == 0


def _write_record(env: Env) -> None:
    big = str(MAX_TOKEN_COUNT)
    argv = ["--config", str(env.config), "record", "--model", MODEL, "--command", "tool"]
    argv += ["--input", big, "--output", big, "--cached", "0", "--reasoning", big]
    assert main(argv) == 0


def _write_record_with_long_text(env: Env) -> None:
    long = "x" * 5000
    big = str(MAX_TOKEN_COUNT)
    argv = ["--config", str(env.config), "record", "--model", long, "--command", long]
    argv += ["--input", big, "--output", big, "--error-kind", long, "--status", "error"]
    assert main([*argv, "--elapsed", "1e300"]) == 0


def _write_ask_with_a_long_model(env: Env) -> None:
    assert main(["--config", str(env.config), *ASK, "-m", "m" * 5000]) == 0


def _write_event_above_the_cap(env: Env) -> None:
    huge = _recorded_cap() * 10
    names = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "charge")
    assert append_event(env.ledger, _event(**dict.fromkeys(names, huge)))


def _write_killed(env: Env) -> None:
    """A dead run whose index holds rows at the reader's limit, so their sum is above it."""
    outdir = env.tmp / "dead"
    outdir.mkdir()
    run = openruns.begin(env.state, outdir=outdir, model=MODEL, dialect="openai")
    assert run is not None
    rows = "".join(
        runner._charge_row(_recorded_cap(), items=1, run=run.run_id) + "\n" for _ in range(3)
    )
    runner._append_index(outdir / "results.jsonl", rows)
    # This process wrote the record, so settle treats it as dead.
    openruns.settle(env.state, env.ledger)
    (event,) = load_events_strict(env.ledger)[0]
    assert event.error_kind == "Killed"


def _write_charge_rows(env: Env) -> None:
    """Charge rows above the limit, for a run still open in another process."""
    outdir = env.tmp / "live"
    outdir.mkdir()
    run = openruns.begin(env.state, outdir=outdir, model=MODEL, dialect="openai")
    assert run is not None
    rows = "".join(
        runner._charge_row(_recorded_cap() * 10, items=n, run=run.run_id) + "\n" for n in (1, 10)
    )
    runner._append_index(outdir / "results.jsonl", rows)
    # The parent process stands in for the live run that owns the record.
    records = env.state / openruns.OPEN_RUNS_FILE
    data = json.loads(records.read_text(encoding="utf-8"))
    data["pid"] = os.getppid()
    records.write_text(json.dumps(data) + "\n", encoding="utf-8")


WRITERS: dict[str, Callable[[Env], None]] = {
    "ask event": _write_ask,
    "map event and charge rows": _write_map,
    "record": _write_record,
    "record with long text": _write_record_with_long_text,
    "ask with a long model": _write_ask_with_a_long_model,
    "event above the cap": _write_event_above_the_cap,
    "Killed event": _write_killed,
    "charge rows above the cap": _write_charge_rows,
}


def _index_rows(index: Path) -> list[dict[str, Any]]:
    rows = []
    for line in index.read_text(encoding="utf-8").splitlines():
        parsed = json.loads(line)
        assert isinstance(parsed, dict)
        rows.append(parsed)
    return rows


def _read_back(env: Env) -> None:
    """Every reader of every file the writer touched accepts what it found."""
    events, problems = load_events_strict(env.ledger)
    assert problems == []
    assert len(load_events(env.ledger)) == len(events)
    cap = _recorded_cap()
    for event in events:
        for name in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
            value = getattr(event, name)
            assert value is None or 0 <= value <= cap
        assert event.charge is not None and 0 <= event.charge <= cap
    for index in env.tmp.glob("*/results.jsonl"):
        rows = _index_rows(index)
        charges = [row["charge"] for row in rows if row.get("type") == "request"]
        assert charges and all(0 <= charge <= cap for charge in charges)
        assert charge_from_rows(rows) == sum(charges)
        assert runner.spent_tokens(index.parent) == sum(charges)
        runner.resume_skips(index.parent)
    for run in openruns._load(env.state):
        read = openruns.charge_since(run)
        assert read.complete
        assert read.charge > 0


@pytest.mark.parametrize("writer", list(WRITERS))
def test_writers_never_write_what_readers_refuse(
    env: Env, writer: str, capsys: pytest.CaptureFixture[str]
) -> None:
    WRITERS[writer](env)
    capsys.readouterr()
    _read_back(env)
    code = main(["--config", str(env.budget_config(100)), *ASK])
    err = capsys.readouterr().err
    assert code == 3, err
    assert "tokens spent in the last 7 days" in err
    assert "could not" not in err
