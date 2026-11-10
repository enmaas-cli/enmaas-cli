"""An output directory written by enmaas 0.7.1 resumes under this version.

The directories in `tests/data/v0.7.1-map/` were written by `run_map` of
0.7.1 against a fake gateway, one in each send mode. Their charge rows
have no run field. A resume skips the items they record as done, retries
the rest, seeds the per-run budget from their charge rows, and appends
after their bytes without changing them.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import httpx

from enmaas_cli.runner import MapOutcome, resume_skips, run_map

FIXTURE = Path(__file__).parent / "data" / "v0.7.1-map"
RUN = "0123456789abcdef"
ANSWER = '{"answer": "yes"}'
_ITEM = re.compile(r"=== ITEM (\S+) ===")


def _handler(request: httpx.Request) -> httpx.Response:
    """Answer every item. Each item costs 36 metered tokens."""
    prompt = json.loads(request.content)["messages"][-1]["content"]
    ids = _ITEM.findall(prompt)
    count = max(1, len(ids))
    text = "".join(f"=== ANSWER {i} ===\n{ANSWER}\n" for i in ids) if ids else ANSWER
    usage = {
        "prompt_tokens": 40 * count,
        "completion_tokens": 6 * count,
        "prompt_tokens_details": {"cached_tokens": 10 * count},
    }
    body = {"choices": [{"finish_reason": "stop", "message": {"content": text}}], "usage": usage}
    return httpx.Response(200, json=body)


async def _resume(tmp_path: Path, mode: str, ids: str) -> tuple[Path, str, MapOutcome]:
    outdir = tmp_path / "out"
    shutil.copytree(FIXTURE / mode, outdir)
    old = (outdir / "results.jsonl").read_text(encoding="utf-8")
    items = tmp_path / "items.jsonl"
    items.write_text(
        "".join(json.dumps({"id": i, "task": "ok"}) + "\n" for i in ids), encoding="utf-8"
    )
    template = tmp_path / "t.txt"
    if mode == "batched":
        template.write_text("Answer each item.\n===PER ITEM===\ntask {task}\n", encoding="utf-8")
    else:
        template.write_text("task {task}", encoding="utf-8")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        outcome = await run_map(
            client=client,
            base_url="https://gw.example.test",
            key="sk-resume-test-key",
            model="rits/zai-org/glm-5-3",
            items_path=items,
            template_path=template,
            outdir=outdir,
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect="openai",
            resume=True,
            batch=2 if mode == "batched" else None,
            token_budget=100_000,
            run_id=RUN,
        )
    return outdir, old, outcome


def test_the_fixture_records_what_resume_reads() -> None:
    # Repaired items and failed items are retried; the rest are skipped.
    assert resume_skips(FIXTURE / "single") == {"a", "d"}
    assert resume_skips(FIXTURE / "batched") == {"a", "b", "d"}


async def test_a_single_mode_directory_from_0_7_1_resumes(tmp_path: Path) -> None:
    outdir, old, outcome = await _resume(tmp_path, "single", "abcde")
    assert (outcome.skipped, outcome.attempted, outcome.ok, outcome.failed) == (2, 3, 3, 0)
    # 127 from the 0.7.1 charge rows, then 36 for each of b, c and e.
    assert outcome.spent == 127 + 3 * 36
    rows = "".join(
        f'{{"type": "request", "charge": 36, "items": 1, "run": "{RUN}"}}\n'
        f'{{"id": "{i}", "status": "ok", "path": "{outdir}/{i}.md", '
        f'"tokens": {{"in": 40, "out": 6}}, "error": null}}\n'
        for i in "bce"
    )
    assert (outdir / "results.jsonl").read_text(encoding="utf-8") == old + rows
    # The repaired answer is replaced by a clean one.
    assert (outdir / "c.md").read_text(encoding="utf-8") == ANSWER


async def test_a_batched_directory_from_0_7_1_resumes(tmp_path: Path) -> None:
    outdir, old, outcome = await _resume(tmp_path, "batched", "abcde")
    assert (outcome.skipped, outcome.attempted, outcome.ok, outcome.failed) == (3, 2, 2, 0)
    # 193 from the 0.7.1 charge rows, then one request for c and e.
    assert outcome.spent == 193 + 72
    rows = f'{{"type": "request", "charge": 72, "items": 2, "run": "{RUN}"}}\n' + "".join(
        f'{{"id": "{i}", "status": "ok", "path": "{outdir}/{i}.md", '
        f'"tokens": null, "batch_size": 2, "error": null}}\n'
        for i in "ce"
    )
    assert (outdir / "results.jsonl").read_text(encoding="utf-8") == old + rows
    assert (outdir / "e.md").read_text(encoding="utf-8") == ANSWER
