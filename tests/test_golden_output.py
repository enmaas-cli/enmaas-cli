"""Exact output of `map`, `ask` and `usage`, pinned byte for byte.

Each case runs the command in process against a fixed fake gateway and
compares one transcript with a file in `tests/data/golden/`. A transcript
holds the exit code, stdout, stderr, the index, the answer files with their
modes, the protection records and the usage ledger. Key order is part of
the bytes.

Values that change between runs are replaced by fixed markers before the
comparison, and nothing else is:

- `<TMP>`: the temporary directory of the test.
- `<RUN>`: the run id in a charge row.
- `<RUNDIR>`: the name of the directory `ask` creates for its answer.
- `<TIME>`: a ledger timestamp.
- `<ELAPSED>`: an elapsed time, in JSON and in a receipt.

The `usage` cases read a fixed ledger, so their output is compared with no
marker at all. The umask is fixed, because it sets the mode of a directory
the run creates.

Every relative path is relative to the temporary directory, which is the
working directory of each case. A message that is cut to a fixed length
then cuts at the same place on every machine.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.cli import main

GOLDEN = Path(__file__).parent / "data" / "golden"
KEY = "sk-golden-test-key-0000"
MODEL = "rits/zai-org/glm-5-3"
SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}
# One template per send mode. The batched one carries the marker, so a
# run without --batch sends groups of ten.
SINGLE_TEMPLATE = "task {task}"
BATCHED_TEMPLATE = "Answer each item.\n===PER ITEM===\ntask {task}\n"

# What the fake gateway answers for each task word: the answer text and
# the usage it reports. "fail" gets a 500 with no usage. A batched request
# reports the sum of its items' usage, and fails as a whole when one of
# its items is "fail". A batched reply leaves out every "missing" item.
ANSWERS: dict[str, tuple[str, dict[str, Any]]] = {
    "ok": (
        '{"answer": "yes"}',
        {
            "prompt_tokens": 40,
            "completion_tokens": 6,
            "prompt_tokens_details": {"cached_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    ),
    "repair": ('{"answer": "x\ud800y"}', {"prompt_tokens": 30, "completion_tokens": 5}),
    "schema": ("plain text, not JSON", {"prompt_tokens": 50, "completion_tokens": 7}),
    "missing": ("", {"prompt_tokens": 20, "completion_tokens": 0}),
    # Answers like "ok". The gateway swaps the index for a link first.
    "swap": ('{"answer": "yes"}', {"prompt_tokens": 40, "completion_tokens": 6}),
}
_TASK = re.compile(r"task (\w+)")
_ITEM = re.compile(r"=== ITEM (\S+) ===\ntask (\w+)")


def _reply(text: str, usage: dict[str, Any]) -> httpx.Response:
    body = {
        "choices": [{"finish_reason": "stop", "message": {"content": text}}],
        "usage": usage,
    }
    # Encoded here, with ASCII escapes, so a lone surrogate reaches the
    # client the way a real gateway sends it.
    return httpx.Response(
        200, content=json.dumps(body).encode("ascii"), headers={"content-type": "application/json"}
    )


def _swap_index(root: Path) -> None:
    """Put a link where the index was, so the next index write is refused."""
    index = root / "out" / "results.jsonl"
    saved = root / "out" / "saved.jsonl"
    index.rename(saved)
    index.symlink_to(saved.name)


def _handler(root: Path) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        batched = _ITEM.findall(prompt)
        tasks = [task for _, task in batched] if batched else _TASK.findall(prompt)
        if "fail" in tasks:
            return httpx.Response(500, text="the gateway is down")
        if "swap" in tasks:
            _swap_index(root)
        totals: dict[str, int] = {}
        for task in tasks:
            for name, value in ANSWERS[task][1].items():
                if isinstance(value, dict):
                    for inner, count in value.items():
                        totals[f"{name}.{inner}"] = totals.get(f"{name}.{inner}", 0) + count
                else:
                    totals[name] = totals.get(name, 0) + value
        usage: dict[str, Any] = {}
        for name, count in totals.items():
            outer, _, inner = name.partition(".")
            if inner:
                usage.setdefault(outer, {})[inner] = count
            else:
                usage[outer] = count
        if not batched:
            return _reply(ANSWERS[tasks[0]][0], usage)
        text = "".join(
            f"=== ANSWER {item_id} ===\n{ANSWERS[task][0]}\n"
            for item_id, task in batched
            if task != "missing"
        )
        return _reply(text, usage)

    return handle


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", KEY)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    monkeypatch.delenv("ENMAAS_DEBUG", raising=False)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(_handler(tmp_path))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport))
    Path("schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    Path("single.txt").write_text(SINGLE_TEMPLATE, encoding="utf-8")
    Path("batched.txt").write_text(BATCHED_TEMPLATE, encoding="utf-8")
    previous = os.umask(0o022)
    try:
        yield tmp_path
    finally:
        os.umask(previous)


def _items(*tasks: str) -> str:
    """Write one item per task, with ids a, b, c and so on."""
    Path("items.jsonl").write_text(
        "".join(
            json.dumps({"id": chr(ord("a") + n), "task": task}) + "\n"
            for n, task in enumerate(tasks)
        ),
        encoding="utf-8",
    )
    return "items.jsonl"


def _map(template: str, *extra: str) -> list[str]:
    return [
        "--config",
        "none.toml",
        "map",
        "-m",
        MODEL,
        "--dialect",
        "openai",
        "--items",
        "items.jsonl",
        "--template",
        template,
        "--outdir",
        "out",
        "--retries",
        "0",
        "--concurrency",
        "1",
        *extra,
    ]


def _ask(prompt: str, *extra: str) -> list[str]:
    return [
        "--config",
        "none.toml",
        "ask",
        "-m",
        MODEL,
        "--dialect",
        "openai",
        "-p",
        prompt,
        "--retries",
        "0",
        *extra,
    ]


def _ledger(root: Path) -> Path:
    return root / "state" / "enmaas" / "runs" / "usage.jsonl"


def _ledger_row(**fields: Any) -> str:
    row: dict[str, Any] = {
        "timestamp": "2026-11-01T09:00:00+00:00",
        "command": "ask",
        "model": MODEL,
        "dialect": "openai",
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "elapsed": 1.5,
        "status": "ok",
        "schema_used": False,
        "error_kind": None,
        "items_total": None,
        "items_ok": None,
        "items_failed": None,
        "charge": 0,
    }
    row.update(fields)
    return json.dumps(row) + "\n"


def _spent_ledger(root: Path) -> None:
    """A budget config and a ledger whose window is already over that budget."""
    Path("budget.toml").write_text("[budget]\nwindow_days = 7\ntokens = 100\n", encoding="utf-8")
    ledger = _ledger(root)
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        _ledger_row(
            timestamp=datetime.now(UTC).isoformat(),
            input_tokens=400,
            output_tokens=100,
            charge=500,
        ),
        encoding="utf-8",
    )


def _mask(text: str, root: Path) -> str:
    text = text.replace(str(root), "<TMP>")
    text = re.sub(r'"run": "[0-9a-f]+"', '"run": "<RUN>"', text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f]{4}", "<RUNDIR>", text)
    text = re.sub(r'"timestamp": "[^"]*"', '"timestamp": "<TIME>"', text)
    text = re.sub(r'"elapsed": [0-9.e+-]+', '"elapsed": <ELAPSED>', text)
    return re.sub(r"elapsed   \d+\.\ds", "elapsed   <ELAPSED>", text)


def _files(directory: Path) -> list[str]:
    """Each entry under the directory: its kind and mode, and a file's text."""
    lines: list[str] = []
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory).as_posix()
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            lines.append(f"{name}  link to {os.readlink(path)}")
        elif stat.S_ISDIR(info.st_mode):
            lines.append(f"{name}/  {stat.S_IMODE(info.st_mode):o}")
        else:
            lines.append(f"{name}  {stat.S_IMODE(info.st_mode):o}")
            lines.append(path.read_text(encoding="utf-8"))
    return lines


def _transcript(
    code: int, capsys: pytest.CaptureFixture[str], root: Path, *, mask: bool = True
) -> str:
    captured = capsys.readouterr()
    parts = [f"=== exit {code}", "=== stdout", captured.out, "=== stderr", captured.err]
    if (root / "out").exists():
        parts += ["=== out", *_files(root / "out")]
    ledger = _ledger(root)
    if ledger.exists():
        parts += ["=== usage.jsonl", ledger.read_text(encoding="utf-8")]
    text = "\n".join(parts)
    return _mask(text, root) if mask else text


def _check(name: str, transcript: str) -> None:
    expected = (GOLDEN / f"{name}.txt").read_text(encoding="utf-8")
    assert transcript == expected


FORMS = ["text", "json"]


def _form(form: str) -> list[str]:
    return ["--json"] if form == "json" else []


# Index rows in both send modes, with and without the run field. The run
# field is written when the run has an open-run record, which a disabled
# ledger turns off.


@pytest.mark.parametrize("run_field", ["run", "norun"])
def test_index_rows_single(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    run_field: str,
) -> None:
    if run_field == "norun":
        monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    _items("ok", "fail", "repair", "schema")
    code = main(_map("single.txt", "--schema", "schema.json"))
    capsys.readouterr()
    lines = [f"=== exit {code}", "=== out", *_files(root / "out")]
    _check(f"index_single_{run_field}", _mask("\n".join(lines), root))


@pytest.mark.parametrize("run_field", ["run", "norun"])
def test_index_rows_batched(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    run_field: str,
) -> None:
    if run_field == "norun":
        monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    # Groups of two: ok and repair, schema and missing, fail and ok, then a
    # last group of one that still uses the batched mode.
    _items("ok", "repair", "schema", "missing", "fail", "ok", "ok")
    code = main(_map("batched.txt", "--batch", "2", "--schema", "schema.json"))
    capsys.readouterr()
    lines = [f"=== exit {code}", "=== out", *_files(root / "out")]
    _check(f"index_batched_{run_field}", _mask("\n".join(lines), root))


# `map`: the --json object and the text receipt, with the index and the
# ledger event each run leaves.


@pytest.mark.parametrize("form", FORMS)
def test_map_success_single(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    _items("ok", "ok")
    code = main(_map("single.txt", *_form(form)))
    _check(f"map_success_single_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_success_batched(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    _items("ok", "ok", "ok")
    code = main(_map("batched.txt", *_form(form)))
    _check(f"map_success_batched_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_partial_single(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    _items("ok", "fail", "repair")
    code = main(_map("single.txt", *_form(form)))
    _check(f"map_partial_single_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_partial_batched(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    _items("ok", "repair", "schema", "missing", "fail", "ok", "ok")
    code = main(_map("batched.txt", "--batch", "2", "--schema", "schema.json", *_form(form)))
    _check(f"map_partial_batched_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_schema_failure_single(
    root: Path, capsys: pytest.CaptureFixture[str], form: str
) -> None:
    # One item per request, and one answer the schema refuses.
    _items("ok", "schema")
    code = main(_map("single.txt", "--schema", "schema.json", *_form(form)))
    _check(f"map_schema_failure_single_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_budget_stop(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    # Each request charges 36, so the second one reaches the ceiling.
    _items("ok", "ok", "ok", "ok")
    code = main(_map("single.txt", "--token-budget", "50", *_form(form)))
    _check(f"map_budget_stop_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_budget_stop_batched(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    _items("ok", "ok", "ok", "ok", "ok")
    code = main(_map("batched.txt", "--batch", "2", "--token-budget", "80", *_form(form)))
    _check(f"map_budget_stop_batched_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_rolling_refusal(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    _spent_ledger(root)
    _items("ok")
    argv = _map("single.txt", *_form(form))
    argv[1] = "budget.toml"
    code = main(argv)
    _check(f"map_rolling_refusal_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_index_error(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    # The second request finds a link in place of the index, so its rows
    # cannot be written and the run stops.
    _items("ok", "swap", "ok")
    code = main(_map("single.txt", *_form(form)))
    _check(f"map_index_error_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_map_missing_items(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    argv = _map("single.txt", *_form(form))
    argv[argv.index("items.jsonl")] = "no-such-items.jsonl"
    code = main(argv)
    _check(f"map_missing_items_{form}", _transcript(code, capsys, root))


# `ask`: the --json object and the text receipt.


@pytest.mark.parametrize("form", FORMS)
def test_ask_success(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    code = main(_ask("task ok", *_form(form)))
    _check(f"ask_success_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_ask_repaired(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    code = main(_ask("task repair", *_form(form)))
    _check(f"ask_repaired_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_ask_schema_failure(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    # The gateway replied, so the answer is saved and its usage is recorded.
    code = main(_ask("task schema", "--schema", "schema.json", *_form(form)))
    _check(f"ask_schema_failure_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_ask_gateway_error(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    code = main(_ask("task fail", *_form(form)))
    _check(f"ask_gateway_error_{form}", _transcript(code, capsys, root))


@pytest.mark.parametrize("form", FORMS)
def test_ask_rolling_refusal(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    _spent_ledger(root)
    argv = _ask("task ok", *_form(form))
    argv[1] = "budget.toml"
    code = main(argv)
    _check(f"ask_rolling_refusal_{form}", _transcript(code, capsys, root))


# `usage`: a small fixed ledger.


@pytest.mark.parametrize("form", FORMS)
def test_usage(root: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    ledger = _ledger(root)
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        _ledger_row(input_tokens=1200, cached_tokens=200, output_tokens=300, charge=1300)
        + _ledger_row(
            timestamp="2026-11-02T10:30:00+00:00",
            command="map",
            input_tokens=5000,
            cached_tokens=1000,
            output_tokens=900,
            reasoning_tokens=120,
            elapsed=42.25,
            status="error",
            error_kind="PartialFailure",
            items_total=10,
            items_ok=9,
            items_failed=1,
            charge=4900,
        )
        + _ledger_row(
            timestamp="2026-11-03T12:00:00+00:00",
            command="record",
            model="other/model-a",
            dialect="anthropic",
            input_tokens=10,
            output_tokens=20,
            elapsed=0.5,
            charge=30,
        ),
        encoding="utf-8",
    )
    code = main(["--config", "none.toml", "usage", *_form(form)])
    _check(f"usage_{form}", _transcript(code, capsys, root, mask=False))
