"""Tests for the runner module: inputs, the breaker, resume and recording a map run."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import openruns
from enmaas_cli.cli import main
from enmaas_cli.receipts import map_receipt
from enmaas_cli.runner import (
    CircuitBreaker,
    MapOutcome,
    _append_index,
    load_items,
    render,
    resume_skips,
    run_map,
)
from enmaas_cli.schema import ValidationError


def _items(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "items.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_load_items_reads_jsonl(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "a", "x": 1}, {"id": "b", "x": 2}])
    assert [i["id"] for i in load_items(path)] == ["a", "b"]


def test_missing_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"x": 1}])
    with pytest.raises(ValidationError, match="line 1"):
        load_items(path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "a"}, {"id": "a"}])
    with pytest.raises(ValidationError, match="duplicate"):
        load_items(path)


def test_malformed_line_is_rejected_with_line_number(tmp_path: Path) -> None:
    path = tmp_path / "items.jsonl"
    path.write_text('{"id": "a"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="line 2"):
        load_items(path)


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "items.jsonl"
    path.write_text('{"id": "a"}\n\n{"id": "b"}\n', encoding="utf-8")
    assert len(load_items(path)) == 2


def test_render_substitutes_fields() -> None:
    assert render("Check {name}", {"id": "a", "name": "thing"}) == "Check thing"


def test_render_rejects_missing_field_naming_the_item() -> None:
    with pytest.raises(ValidationError) as exc:
        render("Check {name}", {"id": "item-7"})
    assert "item-7" in str(exc.value)
    assert "name" in str(exc.value)


def test_resume_skips_only_items_recorded_ok_with_a_file(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "a.md").write_text("done", encoding="utf-8")
    (outdir / "c.md").write_text("done", encoding="utf-8")
    index = outdir / "results.jsonl"
    index.write_text(
        '{"id": "a", "status": "ok"}\n'
        '{"id": "b", "status": "ok"}\n'
        '{"id": "c", "status": "error"}\n',
        encoding="utf-8",
    )
    # a: ok + file present -> skip. b: ok but no file -> redo. c: error -> redo.
    assert resume_skips(outdir) == {"a"}


def test_resume_with_no_index_skips_nothing(tmp_path: Path) -> None:
    assert resume_skips(tmp_path) == set()


def test_path_traversal_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "../../escaped"}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_subdir_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "sub/dir/item"}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_dotdot_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": ".."}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_empty_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": ""}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_absolute_path_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "/etc/passwd"}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_ordinary_id_is_accepted(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "item-7"}])
    items = load_items(path)
    assert len(items) == 1
    assert items[0]["id"] == "item-7"


def test_id_with_dots_is_accepted(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "file.v2"}])
    items = load_items(path)
    assert len(items) == 1
    assert items[0]["id"] == "file.v2"


def test_does_not_trip_before_the_window_is_full() -> None:
    """A 5-item run must never abort on rate; the window is never full."""
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(5):
        breaker.record(ok=False)
    assert breaker.should_abort() is False


def test_trips_when_majority_of_full_window_failed() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(5):
        breaker.record(ok=True)
    for _ in range(15):
        breaker.record(ok=False)
    assert breaker.should_abort() is True


def test_does_not_trip_at_exactly_the_threshold() -> None:
    """Strictly greater than, so a 50/50 run keeps going.

    Outcomes alternate rather than arriving in two blocks, so this exercises
    the rate rule without also tripping the consecutive-failure rule.
    """
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(10):
        breaker.record(ok=True)
        breaker.record(ok=False)
    assert breaker.should_abort() is False


def test_six_consecutive_failures_trip_even_on_a_short_run() -> None:
    """Total breakage should not have to wait for a full 20-item window."""
    breaker = CircuitBreaker(window=20, threshold=0.5, consecutive=6)
    for _ in range(6):
        breaker.record(ok=False)
    assert breaker.should_abort() is True


def test_five_consecutive_failures_do_not_trip() -> None:
    """An unlucky burst of rate limiting is not the same as a dead gateway."""
    breaker = CircuitBreaker(window=20, threshold=0.5, consecutive=6)
    for _ in range(5):
        breaker.record(ok=False)
    assert breaker.should_abort() is False


def test_one_success_resets_the_consecutive_run() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5, consecutive=6)
    for _ in range(5):
        breaker.record(ok=False)
    breaker.record(ok=True)
    for _ in range(5):
        breaker.record(ok=False)
    assert breaker.should_abort() is False


def test_isolated_failures_do_not_trip_a_healthy_run() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for i in range(100):
        breaker.record(ok=(i % 10 != 0))
    assert breaker.should_abort() is False


def test_window_slides_so_recovery_clears_the_breaker() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(20):
        breaker.record(ok=False)
    assert breaker.should_abort() is True
    for _ in range(20):
        breaker.record(ok=True)
    assert breaker.should_abort() is False


def test_all_failures_trips() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(20):
        breaker.record(ok=False)
    assert breaker.should_abort() is True


def test_outcome_reports_why_it_aborted() -> None:
    """'aborted' alone sends users looking for failures that did not happen."""
    from pathlib import Path

    from enmaas_cli.runner import MapOutcome

    outcome = MapOutcome(
        ok=0,
        failed=6,
        skipped=0,
        attempted=6,
        total=20,
        abort_reason="breaker",
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        elapsed=1.0,
        outdir=Path("out"),
        failures=[],
    )
    assert outcome.abort_reason == "breaker"
    assert outcome.exit_code == 3


def test_outcome_without_an_abort_reason_is_not_aborted() -> None:
    from pathlib import Path

    from enmaas_cli.runner import MapOutcome

    outcome = MapOutcome(
        ok=20,
        failed=0,
        skipped=0,
        attempted=20,
        total=20,
        abort_reason=None,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        elapsed=1.0,
        outdir=Path("out"),
        failures=[],
    )
    assert outcome.exit_code == 0


# Tests for JSON Schema support in map command.


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_map_schema_invalid_item_recorded_as_failed(tmp_path: Path) -> None:
    """map records a schema-invalid item as failed in results.jsonl."""
    items_file = tmp_path / "items.jsonl"
    items_file.write_text('{"id": "one", "prompt": "hi"}\n')

    template_file = tmp_path / "template.txt"
    template_file.write_text("{prompt}")

    schema_file = tmp_path / "schema.json"
    schema_file.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }
        )
    )

    outdir = tmp_path / "out"

    call_count = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["count"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"wrong": "field"}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    async with _client(handler) as client:
        outcome = await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
            model="gpt-6-sol",
            items_path=items_file,
            template_path=template_file,
            outdir=outdir,
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=100,
            dialect=None,
            resume=False,
            schema_file=schema_file,
            schema_name="Test",
        )

    assert outcome.failed == 1
    assert outcome.ok == 0
    assert call_count["count"] == 1

    rows = [
        json.loads(line)
        for line in (outdir / "results.jsonl").read_text().splitlines()
        if line.strip()
    ]
    # One charge row follows the item row, and its charge is nonzero: a
    # response the schema rejects still cost the tokens that produced it.
    charges = [r for r in rows if r.get("type") == "request"]
    assert len(charges) == 1
    assert charges[0]["charge"] > 0
    result_line = next(r for r in rows if r.get("type") != "request")
    assert result_line["id"] == "one"
    assert result_line["status"] == "error"
    assert "schema" in result_line["error"].lower() or "validation" in result_line["error"].lower()


async def test_map_schema_invalid_counts_toward_circuit_breaker(tmp_path: Path) -> None:
    """map counts schema failures toward the circuit breaker."""
    items_file = tmp_path / "items.jsonl"
    items_file.write_text("\n".join(f'{{"id": "item{i}"}}' for i in range(10)))

    template_file = tmp_path / "template.txt"
    template_file.write_text("test")

    schema_file = tmp_path / "schema.json"
    schema_file.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"valid": {"type": "boolean"}},
                "required": ["valid"],
            }
        )
    )

    outdir = tmp_path / "out"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"invalid": true}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    async with _client(handler) as client:
        outcome = await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
            model="gpt-6-sol",
            items_path=items_file,
            template_path=template_file,
            outdir=outdir,
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=100,
            dialect=None,
            resume=False,
            schema_file=schema_file,
            schema_name="Test",
        )

    assert outcome.abort_reason == "breaker"
    assert outcome.attempted < 10


# A cancel never separates an answer file from its index rows.
#
# Nothing awaits between a reply arriving and its rows being written, so a
# cancel that lands mid-run leaves every answer on disk with an ok row and a
# charge row before it.


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


# An output directory written by enmaas 0.7.1 resumes under this version.
#
# The directories in `tests/data/v0.7.1-map/` were written by `run_map` of
# 0.7.1 against a fake gateway, one in each send mode. Their charge rows
# have no run field. A resume skips the items they record as done, retries
# the rest, seeds the per-run budget from their charge rows, and appends
# after their bytes without changing them.


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


# The map index is never written through a link or into something not a file.


OK = {
    "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n{"id": "b", "x": 2}\n', encoding="utf-8")
    template = tmp_path / "template.txt"
    template.write_text("do {x}", encoding="utf-8")
    return items, template


async def _run(tmp_path: Path, outdir: Path, calls: list[Any], **kw: Any) -> Any:
    items, template = _inputs(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
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
            **kw,
        )


@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
async def test_an_index_that_is_a_link_is_refused_before_any_request(
    tmp_path: Path, resume: bool
) -> None:
    outdir = tmp_path / "out"
    outdir.mkdir()
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep me\n", encoding="utf-8")
    (outdir / "results.jsonl").symlink_to(target)
    calls: list[Any] = []
    with pytest.raises(OSError) as caught:
        await _run(tmp_path, outdir, calls, resume=resume)
    assert str(caught.value) == (
        f"{outdir / 'results.jsonl'} is not a regular file; "
        "enmaas does not write its index through a link"
    )
    assert calls == []
    assert target.read_text(encoding="utf-8") == "keep me\n"


async def test_an_index_that_is_a_directory_is_refused_before_any_request(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "out"
    (outdir / "results.jsonl").mkdir(parents=True)
    calls: list[Any] = []
    with pytest.raises(OSError, match="is not a regular file"):
        await _run(tmp_path, outdir, calls, resume=False)
    assert calls == []


def test_append_index_does_not_follow_a_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep me\n", encoding="utf-8")
    index = tmp_path / "results.jsonl"
    index.symlink_to(target)
    with pytest.raises(OSError):
        _append_index(index, "row\n")
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_append_index_creates_a_private_file_and_appends(tmp_path: Path) -> None:
    index = tmp_path / "results.jsonl"
    old = os.umask(0o022)
    try:
        _append_index(index, "one\n")
        _append_index(index, "two\n")
    finally:
        os.umask(old)
    assert index.read_text(encoding="utf-8") == "one\ntwo\n"
    assert stat.S_IMODE(index.stat().st_mode) == 0o600


def test_map_through_main_refuses_a_linked_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=OK)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    items, template = _inputs(tmp_path)
    outdir = tmp_path / "out"
    outdir.mkdir()
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(json.dumps({"type": "request", "charge": 50}) + "\n", encoding="utf-8")
    (outdir / "results.jsonl").symlink_to(target)
    argv = ["--config", str(tmp_path / "none.toml"), "map", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "--items", str(items), "--template", str(template)]
    argv += ["--outdir", str(outdir), "--retries", "0"]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert [line for line in err.splitlines() if line.startswith("error:")] == [
        f"error: {outdir / 'results.jsonl'} is not a regular file; "
        "enmaas does not write its index through a link"
    ]
    assert calls == []
    state = tmp_path / "xdg" / "enmaas" / "runs"
    assert not (state / "usage.jsonl").exists()
    for run in openruns._load(state):
        assert openruns.charge_since(run).charge == 0
