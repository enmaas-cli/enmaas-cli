import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx

from enmaas_cli.budget import TokenBudget
from enmaas_cli.runner import MapOutcome, run_map

# The two handler shapes httpx.MockTransport accepts.
Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)

OK = {
    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _usage_response(*, prompt: int, cached: int = 0, completion: int) -> dict[str, Any]:
    """An OK response reporting specific token counts.

    OpenAI dialect, so prompt_tokens is the inclusive total and the cached
    portion is nested in prompt_tokens_details (dialect.py:219-227).
    """
    usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
        "usage": usage,
    }


def _charges(outdir: Path) -> list[int]:
    """Charge rows from results.jsonl, in order."""
    lines = (outdir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return [r["charge"] for r in rows if r.get("type") == "request"]


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


async def _run(
    tmp_path: Path,
    handler: Handler,
    count: int = 5,
    template_path_override: Path | None = None,
    concurrency: int = 2,
    resume: bool = False,
    **kw: Any,
) -> MapOutcome:
    # concurrency and resume are explicit parameters rather than **kw
    # entries because run_map is called with both below, and passing them
    # through kw would raise "got multiple values".
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
            concurrency=concurrency,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect=None,
            resume=resume,
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
    # The index interleaves per-item rows with one charge row per request,
    # so count the item rows rather than the lines.
    items = [r for r in rows if r.get("type") != "request"]
    assert len(items) == 5
    assert all(r["status"] == "ok" for r in items)


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
    assert outcome.abort_reason == "breaker"
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
    assert "reasoning_tokens" in fields, "MapOutcome carries no reasoning token count"


def _batch_template(tmp_path: Path) -> Path:
    from enmaas_cli.batching import PER_ITEM_MARKER

    path = tmp_path / "bt.txt"
    path.write_text(f"shared instructions\n{PER_ITEM_MARKER}\ndo {{x}}\n", encoding="utf-8")
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
        tmp_path,
        handler,
        count=4,
        template_path_override=_batch_template(tmp_path),
        batch=2,
    )

    assert len(calls) == 2, f"made {len(calls)} calls, expected 2"
    assert outcome.ok == 4
    assert (tmp_path / "out" / "i0.md").read_text(encoding="utf-8").strip() == ("answer for i0")


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
        tmp_path,
        handler,
        count=4,
        template_path_override=_batch_template(tmp_path),
        batch=4,
    )

    assert outcome.ok == 1
    assert outcome.failed == 3
    assert outcome.exit_code != 0
    assert any("no answer" in msg.lower() for _, msg in outcome.failures)


async def test_each_request_writes_a_charge_row(tmp_path: Path) -> None:
    """The budget's source of truth, recorded once per request."""
    body = _usage_response(prompt=1000, cached=900, completion=100)
    await _run(tmp_path, lambda r: httpx.Response(200, json=body), count=3)
    # (1000 - 900) + 100 = 200 per request.
    assert _charges(tmp_path / "out") == [200, 200, 200]


async def test_a_failed_request_that_cost_tokens_is_still_charged(
    tmp_path: Path,
) -> None:
    """The README's DeepSeek case: reasoning spent the budget, no text back.

    Charged nothing before this, because EmptyResponseError discarded the
    usage and the single-item path only accounted on success.
    """
    body = {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 32768},
    }
    outcome = await _run(tmp_path, lambda r: httpx.Response(200, json=body), count=1)
    assert outcome.failed == 1
    assert _charges(tmp_path / "out") == [33268]


async def test_a_batched_request_writes_one_charge_row_for_the_group(
    tmp_path: Path,
) -> None:
    """Per-item batch rows carry "tokens": None, so the charge row is the
    only record of what a group cost."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        ids = [f"i{n}" for n in range(10) if f"=== ITEM i{n} ===" in prompt]
        return httpx.Response(200, json=_answers(ids))

    await _run(
        tmp_path,
        handler,
        count=10,
        template_path_override=_batch_template(tmp_path),
        batch=5,
    )
    # Two groups, each 40 prompt + 10 completion, none cached.
    assert _charges(tmp_path / "out") == [50, 50]


async def test_charge_rows_do_not_disturb_resume(tmp_path: Path) -> None:
    """resume_skips reads id and status; a charge row carries neither."""
    from enmaas_cli.runner import resume_skips

    body = _usage_response(prompt=100, completion=10)
    await _run(tmp_path, lambda r: httpx.Response(200, json=body), count=3)
    assert resume_skips(tmp_path / "out") == {"i0", "i1", "i2"}


async def test_spent_tokens_sums_the_charge_rows(tmp_path: Path) -> None:
    """What a resumed run seeds its budget from."""
    from enmaas_cli.runner import spent_tokens

    body = _usage_response(prompt=1000, cached=900, completion=100)
    await _run(tmp_path, lambda r: httpx.Response(200, json=body), count=3)
    assert spent_tokens(tmp_path / "out") == 600


def test_spent_tokens_on_a_missing_index_is_zero(tmp_path: Path) -> None:
    from enmaas_cli.runner import spent_tokens

    assert spent_tokens(tmp_path) == 0


async def test_map_stops_once_the_token_budget_is_spent(tmp_path: Path) -> None:
    """Ten items at 400 metered tokens each against a 1000 ceiling.

    Serial, so the stop point is exact: items 1 and 2 reach 800, item 3
    reaches 1200 and trips it. Items 4-10 never run.
    """
    body = _usage_response(prompt=200, completion=200)
    outcome = await _run(
        tmp_path,
        lambda r: httpx.Response(200, json=body),
        count=10,
        concurrency=1,
        token_budget=1000,
    )
    assert outcome.abort_reason == "budget"
    assert outcome.exit_code == 3
    assert outcome.attempted == 3


async def test_map_without_a_budget_runs_every_item(tmp_path: Path) -> None:
    """The flag is opt-in. Absent it, nothing changes."""
    body = _usage_response(prompt=200, completion=200)
    outcome = await _run(
        tmp_path, lambda r: httpx.Response(200, json=body), count=10, concurrency=1
    )
    assert outcome.abort_reason is None
    assert outcome.attempted == 10


async def test_cached_tokens_do_not_consume_the_budget(tmp_path: Path) -> None:
    """The point of the rule: batching's saving must let a run go further.

    1000 prompt of which 900 cached, plus 100 output, is 200 metered. Ten
    items cost 2000, under a 3000 ceiling, though raw prompt is 10,000.
    """
    body = _usage_response(prompt=1000, cached=900, completion=100)
    outcome = await _run(
        tmp_path,
        lambda r: httpx.Response(200, json=body),
        count=10,
        concurrency=1,
        token_budget=3000,
    )
    assert outcome.abort_reason is None
    assert outcome.attempted == 10
    # The assertion that bites: 2000, not the 10,100 raw prompt tokens
    # would give. Without the cached subtraction this run would have
    # tripped the ceiling at the third item.
    assert outcome.spent == 2000


async def test_the_breaker_wins_when_both_conditions_trip(tmp_path: Path) -> None:
    """First cause wins, tested at a genuine collision.

    Each response is empty-with-length, so it both fails and costs 1000
    metered tokens. The breaker trips on the 6th consecutive failure; the
    ceiling of 6000 is reached by that same 6th request, inside the same
    locked block. The breaker is checked first, so "breaker" must survive.

    Remove either `abort_reason is None` guard and this reports "budget",
    hiding the more actionable failure.
    """
    body = {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 500},
    }
    outcome = await _run(
        tmp_path,
        lambda r: httpx.Response(200, json=body),
        count=20,
        concurrency=1,
        token_budget=6000,
    )
    assert outcome.attempted == 6
    assert outcome.spent == 6000
    assert outcome.abort_reason == "breaker"


async def test_overshoot_is_bounded_by_concurrency(tmp_path: Path) -> None:
    """A stop-scheduling threshold, not a hard wall.

    A synchronous handler never overlaps requests, so this uses an async
    one gated on a barrier to force four genuinely concurrent in-flight
    calls. attempted is at least 4 because the counter increments inside
    the semaphore before any request returns.
    """
    gate = asyncio.Event()
    inflight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight
        inflight += 1
        if inflight >= 4:
            gate.set()
        await gate.wait()
        return httpx.Response(200, json=_usage_response(prompt=200, completion=200))

    outcome = await _run(tmp_path, handler, count=50, concurrency=4, token_budget=1000)
    assert outcome.abort_reason == "budget"
    # The 3rd completion crosses 1000, and at most 3 others are
    # already in flight, so 6 is the documented ceiling. A looser
    # bound would accept an implementation that overshoots further.
    assert 4 <= outcome.attempted <= 6, outcome.attempted


async def test_resuming_a_budget_stopped_run_retrips_immediately(tmp_path: Path) -> None:
    """Deliberate. The budget was for the job, not for one invocation.

    This test exists to stop a future "fix" that resets the count per run,
    which would make the ceiling bypassable by rerunning the command.
    """
    body = _usage_response(prompt=200, completion=200)

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    first = await _run(tmp_path, handler, count=10, concurrency=1, token_budget=1000)
    assert first.abort_reason == "budget"

    second = await _run(tmp_path, handler, count=10, concurrency=1, token_budget=1000, resume=True)
    assert second.abort_reason == "budget"
    assert second.attempted == 0


async def test_raising_the_ceiling_lets_a_resumed_run_continue(tmp_path: Path) -> None:
    """The documented escape hatch."""
    body = _usage_response(prompt=200, completion=200)

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    await _run(tmp_path, handler, count=10, concurrency=1, token_budget=1000)
    second = await _run(tmp_path, handler, count=10, concurrency=1, token_budget=10000, resume=True)
    assert second.abort_reason is None
    assert second.ok + second.skipped == 10


async def test_a_resumed_batch_run_does_not_get_a_fresh_budget(tmp_path: Path) -> None:
    """The defect charge rows exist to fix.

    Batch item rows carry "tokens": None, so seeding from them would read
    zero and hand out a full budget on every resume.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        ids = [f"i{n}" for n in range(20) if f"=== ITEM i{n} ===" in prompt]
        return httpx.Response(200, json=_answers(ids))

    template = _batch_template(tmp_path)
    first = await _run(
        tmp_path,
        handler,
        count=20,
        concurrency=1,
        template_path_override=template,
        batch=5,
        token_budget=60,
    )
    assert first.abort_reason == "budget"

    second = await _run(
        tmp_path,
        handler,
        count=20,
        concurrency=1,
        template_path_override=template,
        batch=5,
        token_budget=60,
        resume=True,
    )
    assert second.abort_reason == "budget"
    assert second.attempted == 0


async def test_outcome_spent_is_the_job_total_not_the_invocation(tmp_path: Path) -> None:
    """A resumed run's receipt must not report 0 of 1000 while tripping."""
    body = _usage_response(prompt=200, completion=200)

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    await _run(tmp_path, handler, count=10, concurrency=1, token_budget=1000)
    second = await _run(tmp_path, handler, count=10, concurrency=1, token_budget=1000, resume=True)
    assert second.attempted == 0
    assert second.spent == 1200


async def test_spent_is_job_wide_even_without_a_ceiling(tmp_path: Path) -> None:
    """A resumed run with no --token-budget must still report the job total.

    Three 400-token requests stop at a 1000 ceiling; resuming the other
    seven without a ceiling spends 2800 more. Reporting 2800 would describe
    the invocation, not the job that cost 4000.
    """
    body = _usage_response(prompt=200, completion=200)

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    await _run(tmp_path, handler, count=10, concurrency=1, token_budget=1000)
    second = await _run(tmp_path, handler, count=10, concurrency=1, resume=True)
    assert second.attempted == 7
    assert second.spent == 4000


async def test_an_exhausted_rolling_budget_sends_nothing(tmp_path: Path) -> None:
    """Pre-flight covers both ceilings.

    Checking only the per-run budget here would pay for one request per
    concurrency slot before noticing the rolling one was already spent.
    """
    calls = 0

    def handler(r: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_usage_response(prompt=200, completion=200))

    outcome = await _run(
        tmp_path, handler, count=10, concurrency=1, rolling_budget=TokenBudget(1000, spent=1000)
    )
    assert outcome.abort_reason == "budget"
    assert outcome.attempted == 0
    assert calls == 0


async def test_rolling_ceiling_stops_a_run_that_crosses_it_midway(
    tmp_path: Path,
) -> None:
    """Pre-flight passed, but the run itself exhausts what was left.

    9000 of 10000 already spent in the window, so three 400-token
    requests reach 10200 and stop it. Without an in-run check this would
    run all ten items and only be caught next invocation.
    """
    body = _usage_response(prompt=200, completion=200)
    outcome = await _run(
        tmp_path,
        lambda r: httpx.Response(200, json=body),
        count=10,
        concurrency=1,
        rolling_budget=TokenBudget(10_000, spent=9_000),
    )
    assert outcome.abort_reason == "budget"
    assert outcome.tripped_budget == "rolling"
    assert outcome.attempted == 3


async def test_the_per_run_ceiling_is_named_when_it_is_the_one_that_trips(
    tmp_path: Path,
) -> None:
    """Both active, per-run tighter. The remedy differs, so the name must."""
    body = _usage_response(prompt=200, completion=200)
    outcome = await _run(
        tmp_path,
        lambda r: httpx.Response(200, json=body),
        count=10,
        concurrency=1,
        token_budget=1000,
        rolling_budget=TokenBudget(10_000_000),
    )
    assert outcome.tripped_budget == "per_run"


async def test_no_budget_means_no_tripped_name(tmp_path: Path) -> None:
    body = _usage_response(prompt=200, completion=200)
    outcome = await _run(tmp_path, lambda r: httpx.Response(200, json=body), count=3)
    assert outcome.tripped_budget is None


async def test_outcome_charged_equals_the_charge_rows_the_run_wrote(
    tmp_path: Path,
) -> None:
    """The invariant the ledger's charge field depends on.

    outcome.charged is what cli.py writes to the usage ledger, and the
    charge rows are what a resumed run reads back. If they disagree, the
    rolling window and the per-run ceiling are counting different things.

    Mixed on purpose: cached input, and a failure that still cost tokens.
    """
    calls = 0

    def handler(r: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            # Consumed tokens, returned no text. Charged, not counted by
            # the input/output counters.
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                    "usage": {"prompt_tokens": 300, "completion_tokens": 50},
                },
            )
        return httpx.Response(200, json=_usage_response(prompt=1000, cached=900, completion=100))

    outcome = await _run(tmp_path, handler, count=4, concurrency=1)

    rows = _charges(tmp_path / "out")
    assert len(rows) == 4
    assert outcome.charged == sum(rows)
    # 3 x ((1000-900)+100) + ((300-0)+50)
    assert outcome.charged == 950
    # And it is genuinely not the same as the counters, which miss the
    # failure and clamp after aggregating.
    assert (
        outcome.charged
        != max(0, outcome.input_tokens - outcome.cached_tokens) + outcome.output_tokens
    )


def _raw_body(content_escape: str, *, prompt: int = 5, completion: int = 1) -> bytes:
    """A response exactly as a gateway would put it on the wire.

    The surrogate is an ASCII escape here, so the body is valid UTF-8 in
    transit and only becomes a lone surrogate after json.loads. Building
    this with httpx's json= would fail inside httpx instead, which tests
    httpx rather than this code.
    """
    return (
        '{"choices":[{"finish_reason":"stop","message":{"content":"'
        + content_escape
        + '"}}],"usage":{"prompt_tokens":'
        + str(prompt)
        + ',"completion_tokens":'
        + str(completion)
        + "}}"
    ).encode("ascii")


def _json_response(raw: bytes) -> httpx.Response:
    return httpx.Response(200, content=raw, headers={"content-type": "application/json"})


async def test_one_unencodable_character_does_not_kill_the_run(
    tmp_path: Path,
) -> None:
    """A lone surrogate in one item used to take down the whole map.

    UnicodeEncodeError is a ValueError, not an OSError, so it matched
    none of the handlers and escaped through asyncio.gather. Five good
    items were lost to one bad character in the third.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Keyed off the prompt, not a call counter, so the poisoned item
        # is the same one whatever order concurrency delivers them in.
        prompt = json.loads(request.content)["messages"][-1]["content"]
        bad = prompt.strip() == "do 2"
        return _json_response(_raw_body(r"ok \ud800 bad" if bad else "fine"))

    outcome = await _run(tmp_path, handler, count=6)

    assert outcome.ok == 6
    assert outcome.failed == 0
    assert len(sorted((tmp_path / "out").glob("*.md"))) == 6
    assert (tmp_path / "out" / "i2.md").read_text(encoding="utf-8") == "ok � bad"


async def test_a_batched_run_survives_an_unencodable_character(
    tmp_path: Path,
) -> None:
    """The batched path writes per item and had the same hole."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        ids = [f"i{n}" for n in range(6) if f"=== ITEM i{n} ===" in prompt]
        answers = "".join(f"=== ANSWER {i} ===\\nbad \\ud800 here\\n" for i in ids)
        return _json_response(
            (
                '{"choices":[{"finish_reason":"stop","message":{"content":"'
                + answers
                + '"}}],"usage":{"prompt_tokens":40,"completion_tokens":10}}'
            ).encode("ascii")
        )

    outcome = await _run(
        tmp_path,
        handler,
        count=6,
        template_path_override=_batch_template(tmp_path),
        batch=3,
    )

    assert outcome.ok == 6
    assert (tmp_path / "out" / "i0.md").read_text(encoding="utf-8") == "bad � here"


async def test_a_repaired_item_is_marked_in_the_index(tmp_path: Path) -> None:
    """A bulk run prints no per-item receipt, so the row is the only trace."""

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        bad = prompt.strip() == "do 1"
        return _json_response(_raw_body(r"x \ud800 y" if bad else "fine"))

    await _run(tmp_path, handler, count=3)

    # The index interleaves per-item rows with charge rows, which carry
    # no id, so filter to the item rows before keying on it.
    parsed = [
        json.loads(line)
        for line in (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = {r["id"]: r for r in parsed if r.get("type") != "request"}
    assert rows["i1"]["replaced"] == 1
    assert "replaced" not in rows["i0"]


async def test_a_repaired_batch_item_is_marked_in_the_index(tmp_path: Path) -> None:
    """A batched reply is repaired before it is split, so the count is
    recorded per request rather than per item, and must still appear."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        ids = [f"i{n}" for n in range(3) if f"=== ITEM i{n} ===" in prompt]
        answers = "".join(f"=== ANSWER {i} ===\\nbad \\ud800 here\\n" for i in ids)
        return _json_response(
            (
                '{"choices":[{"finish_reason":"stop","message":{"content":"'
                + answers
                + '"}}],"usage":{"prompt_tokens":40,"completion_tokens":10}}'
            ).encode("ascii")
        )

    await _run(
        tmp_path,
        handler,
        count=3,
        template_path_override=_batch_template(tmp_path),
        batch=3,
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Charge rows share this file and carry no status, so filter first.
    items = [r for r in rows if r.get("type") != "request"]
    assert len(items) == 3
    assert all(r["status"] == "ok" for r in items)
    # Three items in one request, three repairs in that one reply.
    assert all(r["replaced_in_request"] == 3 for r in items)


async def test_a_repaired_item_is_retried_on_resume(tmp_path: Path) -> None:
    """Repaired output is kept, but the item is not treated as finished.

    The saved text differs from what the model sent, so skipping it
    forever would mean a rerun could never obtain a clean version.
    """
    state = {"poison": True}

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        bad = state["poison"] and prompt.strip() == "do 1"
        return _json_response(_raw_body(r"x \ud800 y" if bad else "clean"))

    first = await _run(tmp_path, handler, count=3)
    assert first.ok == 3
    assert (tmp_path / "out" / "i1.md").read_text(encoding="utf-8") == "x � y"

    # The model behaves on the retry.
    state["poison"] = False
    second = await _run(tmp_path, handler, count=3, resume=True)

    assert second.skipped == 2, "the two clean items stay done"
    assert second.attempted == 1, "the repaired one is offered again"
    assert (tmp_path / "out" / "i1.md").read_text(encoding="utf-8") == "clean"


async def test_clean_items_are_still_skipped_on_resume(tmp_path: Path) -> None:
    """The repair exception must not disturb ordinary resume behaviour."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_raw_body("fine"))

    await _run(tmp_path, handler, count=4)
    second = await _run(tmp_path, handler, count=4, resume=True)

    assert second.skipped == 4
    assert second.attempted == 0
