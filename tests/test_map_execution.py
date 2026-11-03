import json
from pathlib import Path

import httpx

from enmaas_cli.runner import MapOutcome, run_map

OK = {
    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _items(tmp_path: Path, count: int) -> Path:
    path = tmp_path / "items.jsonl"
    path.write_text(
        "\n".join(json.dumps({"id": f"i{n}", "x": n}) for n in range(count)) + "\n",
        encoding="utf-8",
    )
    return path


def _template(tmp_path: Path) -> Path:
    path = tmp_path / "t.txt"
    path.write_text("do {x}", encoding="utf-8")
    return path


async def _run(tmp_path: Path, handler, count: int = 5,  # type: ignore[no-untyped-def]
               template_path_override: Path | None = None, **kw) -> MapOutcome:
    outdir = tmp_path / "out"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
            model="rits/zai-org/glm-5-3",
            items_path=_items(tmp_path, count),
            template_path=template_path_override or _template(tmp_path),
            outdir=outdir,
            concurrency=2,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect=None,
            resume=False,
            **kw,
        )


async def test_all_succeed_writes_files_and_index(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, lambda r: httpx.Response(200, json=OK))
    assert outcome.ok == 5
    assert outcome.failed == 0
    assert outcome.exit_code == 0
    assert (tmp_path / "out" / "i0.md").read_text(encoding="utf-8") == "done"
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 5
    assert all(r["status"] == "ok" for r in rows)


async def test_partial_failure_exits_two(tmp_path: Path) -> None:
    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        if seen == 1:
            return httpx.Response(400, text="bad")
        return httpx.Response(200, json=OK)

    outcome = await _run(tmp_path, handler)
    assert outcome.failed == 1
    assert outcome.exit_code == 2


async def test_systemic_failure_trips_breaker_and_exits_three(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, lambda r: httpx.Response(401, text="unauthorized"), count=60)
    assert outcome.exit_code == 3
    assert outcome.aborted is True
    # The breaker must stop the run well short of all 60 items.
    assert outcome.attempted < 60


async def test_key_is_redacted_in_recorded_errors(tmp_path: Path) -> None:
    key = "sk-oai-topsecret"
    outdir = tmp_path / "out"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text=f"bad {key}"))
    ) as client:
        await run_map(
            client=client,
            base_url="https://g.test",
            key=key,
            model="rits/zai-org/glm-5-3",
            items_path=_items(tmp_path, 2),
            template_path=_template(tmp_path),
            outdir=outdir,
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect=None,
            resume=False,
        )
    assert key not in (outdir / "results.jsonl").read_text(encoding="utf-8")


async def test_resume_skips_completed_items(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "i0.md").write_text("already", encoding="utf-8")
    (outdir / "results.jsonl").write_text('{"id": "i0", "status": "ok"}\n', encoding="utf-8")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
            model="rits/zai-org/glm-5-3",
            items_path=_items(tmp_path, 5),
            template_path=_template(tmp_path),
            outdir=outdir,
            concurrency=2,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect=None,
            resume=True,
        )
    assert calls == 4
    assert outcome.skipped == 1
    assert (outdir / "i0.md").read_text(encoding="utf-8") == "already"


async def test_write_failure_is_recorded_not_raised(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pathlib import Path as PathClass

    original_write = PathClass.write_text

    def failing_write(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "i1.md":
            raise OSError("disk full")
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(PathClass, "write_text", failing_write)
    outcome = await _run(tmp_path, lambda r: httpx.Response(200, json=OK), count=3)
    assert outcome.failed == 1
    assert outcome.ok == 2
    assert outcome.exit_code == 2


async def test_skipped_count_scoped_to_current_items(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "old1.md").write_text("old", encoding="utf-8")
    (outdir / "old2.md").write_text("old", encoding="utf-8")
    (outdir / "results.jsonl").write_text(
        '{"id": "old1", "status": "ok"}\n{"id": "old2", "status": "ok"}\n',
        encoding="utf-8",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=OK))
    ) as client:
        outcome = await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
            model="rits/zai-org/glm-5-3",
            items_path=_items(tmp_path, 3),
            template_path=_template(tmp_path),
            outdir=outdir,
            concurrency=2,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect=None,
            resume=True,
        )
    assert outcome.total == 3
    assert outcome.skipped <= outcome.total
    assert outcome.skipped == 0


def test_map_records_cached_and_reasoning_tokens() -> None:
    """map discarded the two counts that matter most for cost.

    The gateway returns cached_tokens and reasoning_tokens on every call,
    the runner summed neither, and the ledger stored None for both. Caching
    is the largest cost lever on bulk work and it was invisible on the one
    command built for bulk work. Measured consequence: three map jobs over
    154 items reported no cache data at all, so there was no way to tell a
    cache miss from an unrecorded hit.
    """
    from enmaas_cli.runner import MapOutcome

    fields = MapOutcome.__dataclass_fields__
    assert "cached_tokens" in fields, "MapOutcome carries no cached token count"
    assert "reasoning_tokens" in fields, (
        "MapOutcome carries no reasoning token count"
    )


def _batch_template(tmp_path: Path) -> Path:
    from enmaas_cli.batching import PER_ITEM_MARKER

    path = tmp_path / "bt.txt"
    path.write_text(
        f"shared instructions\n{PER_ITEM_MARKER}\ndo {{x}}\n", encoding="utf-8"
    )
    return path


def _answers(ids: list[str]) -> dict[str, object]:
    body = "".join(f"=== ANSWER {i} ===\nanswer for {i}\n" for i in ids)
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": body}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10},
    }


async def test_batching_makes_one_call_per_group_not_per_item(
    tmp_path: Path,
) -> None:
    """The whole point: four items in two calls, not four.

    One call per item re-sends the template every time, which measured 75%
    of each prompt on a real job.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        ids = [f"i{n}" for n in range(4) if f"=== ITEM i{n} ===" in prompt]
        calls.append(len(ids))
        return httpx.Response(200, json=_answers(ids))

    outcome = await _run(
        tmp_path, handler, count=4,
        template_path_override=_batch_template(tmp_path), batch=2,
    )

    assert len(calls) == 2, f"made {len(calls)} calls, expected 2"
    assert outcome.ok == 4
    assert (tmp_path / "out" / "i0.md").read_text(encoding="utf-8").strip() == (
        "answer for i0"
    )


async def test_an_item_the_model_skipped_is_failed_not_lost(
    tmp_path: Path,
) -> None:
    """A short reply must not quietly drop an item from the run.

    Reporting ok for an item with no answer would leave a missing output
    file that nothing flags, and --resume would then skip it forever.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        ids = [f"i{n}" for n in range(4) if f"=== ITEM i{n} ===" in prompt]
        return httpx.Response(200, json=_answers(ids[:1]))

    outcome = await _run(
        tmp_path, handler, count=4,
        template_path_override=_batch_template(tmp_path), batch=4,
    )

    assert outcome.ok == 1
    assert outcome.failed == 3
    assert outcome.exit_code != 0
    assert any("no answer" in msg.lower() for _, msg in outcome.failures)
