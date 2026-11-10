"""The token totals of a map run count every usage block the gateway returned.

A reply is counted whether or not its answer was usable: an answer the
schema refuses and a reply that cannot be read both reported usage, and
both were billed. The receipt, the --json object and the ledger event all
show the same totals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.cli import main

KEY = "sk-token-totals-test-key"
MODEL = "rits/zai-org/glm-5-3"
SCHEMA = {"type": "object", "required": ["answer"]}
USAGE = {
    "ok": {"prompt_tokens": 40, "completion_tokens": 6},
    "schema": {"prompt_tokens": 50, "completion_tokens": 7},
    "unreadable": {"prompt_tokens": 60, "completion_tokens": 8},
}
CONTENT: dict[str, Any] = {
    "ok": '{"answer": "yes"}',
    "schema": "plain text, not JSON",
    # A list with no text part is a reply enmaas cannot read.
    "unreadable": [{"type": "image"}],
}
_ITEM = re.compile(r"=== ITEM (\S+) ===\ntask (\w+)")
_TASK = re.compile(r"task (\w+)")


def _handler(request: httpx.Request) -> httpx.Response:
    prompt = json.loads(request.content)["messages"][-1]["content"]
    batched = _ITEM.findall(prompt)
    if not batched:
        (task,) = _TASK.findall(prompt)
        content = CONTENT[task]
        usage = USAGE[task]
    else:
        tasks = [task for _, task in batched]
        content = (
            CONTENT["unreadable"]
            if "unreadable" in tasks
            else "".join(f"=== ANSWER {i} ===\n{CONTENT[t]}\n" for i, t in batched)
        )
        usage = {
            name: sum(USAGE[t][name] for t in tasks)
            for name in ("prompt_tokens", "completion_tokens")
        }
    body = {"choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": usage}
    return httpx.Response(200, json=body)


def _map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tasks: list[str], *extra: str) -> int:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", KEY)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport))
    items = tmp_path / "items.jsonl"
    items.write_text(
        "".join(json.dumps({"id": f"i{n}", "task": task}) + "\n" for n, task in enumerate(tasks)),
        encoding="utf-8",
    )
    template = tmp_path / "t.txt"
    template.write_text("Answer.\n===PER ITEM===\ntask {task}", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    argv = ["--config", str(tmp_path / "none.toml"), "map", "-m", MODEL, "--dialect", "openai"]
    argv += ["--items", str(items), "--template", str(template), "--outdir", str(tmp_path / "out")]
    argv += ["--retries", "0", "--concurrency", "1", "--schema", str(schema), *extra]
    return main(argv)


def _ledger_tokens(tmp_path: Path) -> tuple[int, int]:
    ledger = tmp_path / "state" / "enmaas" / "runs" / "usage.jsonl"
    (line,) = ledger.read_text(encoding="utf-8").splitlines()
    event = json.loads(line)
    return event["input_tokens"], event["output_tokens"]


@pytest.mark.parametrize(
    ("tasks", "batch"),
    [
        (["ok", "schema"], "1"),
        (["ok", "unreadable"], "1"),
        (["ok", "unreadable"], "2"),
    ],
    ids=["single-schema", "single-unreadable", "batched-unreadable"],
)
def test_the_json_object_and_the_ledger_count_every_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tasks: list[str],
    batch: str,
) -> None:
    assert _map(tmp_path, monkeypatch, tasks, "--batch", batch, "--json") == 2
    expected = (
        sum(USAGE[t]["prompt_tokens"] for t in tasks),
        sum(USAGE[t]["completion_tokens"] for t in tasks),
    )
    tokens = json.loads(capsys.readouterr().out)["tokens"]
    assert (tokens["input"], tokens["output"]) == expected
    assert _ledger_tokens(tmp_path) == expected


def test_the_receipt_counts_an_answer_the_schema_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _map(tmp_path, monkeypatch, ["ok", "schema"], "--batch", "1") == 2
    assert "  tokens    in 90   out 13" in capsys.readouterr().out.splitlines()
    assert _ledger_tokens(tmp_path) == (90, 13)
