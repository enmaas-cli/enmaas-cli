"""A cancel never separates an answer file from its index rows.

Nothing awaits between a reply arriving and its rows being written, so a
cancel that lands mid-run leaves every answer on disk with an ok row and a
charge row before it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.receipts import map_receipt
from enmaas_cli.runner import MapOutcome, run_map

USAGE = {"prompt_tokens": 5, "completion_tokens": 2}


def _reply(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": USAGE,
        },
    )


def _ids_in(request: httpx.Request) -> list[str]:
    prompt = request.content.decode("utf-8")
    return re.findall(r"=== ITEM (\w+) ===", prompt) or re.findall(r"do (\w+)", prompt)[:1]


def _answer(ids: list[str], batched: bool) -> str:
    if not batched:
        return f"done {ids[0]}"
    return "\n".join(f"=== ANSWER {i} ===\ndone {i}" for i in ids)


IDS = [f"i{n}" for n in range(8)]


async def _cancelled_run(tmp_path: Path, batched: bool) -> MapOutcome | None:
    """A run cancelled from inside its second request. None if the cancel escaped."""
    items = tmp_path / "items.jsonl"
    items.write_text("".join(json.dumps({"id": i, "x": i}) + "\n" for i in IDS), encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("shared\n===PER ITEM===\ndo {x}" if batched else "do {x}", encoding="utf-8")
    seen = 0
    holder: dict[str, asyncio.Task[MapOutcome]] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        request_ids = _ids_in(request)
        if seen == 2:
            holder["task"].cancel()
            # The cancel is raised here, inside the second request.
            await asyncio.sleep(0)
        return _reply(_answer(request_ids, batched))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        holder["task"] = asyncio.create_task(
            run_map(
                client=client,
                base_url="https://g.test",
                key="sk-oai-x",
                model="rits/zai-org/glm-5-3",
                items_path=items,
                template_path=template,
                outdir=tmp_path / "out",
                concurrency=2,
                retries=0,
                timeout=5,
                thinking=False,
                max_tokens=None,
                dialect="openai",
                resume=False,
                batch=2 if batched else 1,
            )
        )
        with contextlib.suppress(asyncio.CancelledError):
            return await holder["task"]
    return None


@pytest.mark.parametrize("batched", [False, True], ids=["single", "batched"])
async def test_cancel_mid_run_keeps_answer_files_and_rows_together(
    tmp_path: Path, batched: bool
) -> None:
    await _cancelled_run(tmp_path, batched)
    outdir = tmp_path / "out"
    lines = (outdir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, Any]] = [json.loads(line) for line in lines if line.strip()]
    answers = sorted(p.stem for p in outdir.glob("*.md"))
    assert answers, "the first request should have finished before the cancel"
    assert len(answers) < len(IDS), "the cancel should have stopped the run early"
    for item_id in answers:
        position = next(
            n for n, row in enumerate(rows) if row.get("id") == item_id and row["status"] == "ok"
        )
        assert any(row.get("type") == "request" for row in rows[:position]), item_id


@pytest.mark.parametrize("batched", [False, True], ids=["single", "batched"])
async def test_a_cancelled_run_returns_an_interrupted_outcome(
    tmp_path: Path, batched: bool
) -> None:
    outcome = await _cancelled_run(tmp_path, batched)
    assert outcome is not None
    assert outcome.abort_reason == "interrupted"
    assert outcome.tripped_budget is None
    assert outcome.exit_code == 3
    assert outcome.ok == len(list((tmp_path / "out").glob("*.md")))
    assert outcome.charged == 7 * len(_charge_rows(tmp_path))
    receipt = map_receipt(outcome, model="m")
    assert receipt.splitlines()[0] == "enmaas map interrupted"
    assert "  resume    re-run with --resume to continue" in receipt


def _charge_rows(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return [row for row in rows if row.get("type") == "request"]
