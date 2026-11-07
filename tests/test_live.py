"""Hit the real gateway. Free hosted models only. Run with: pytest -m live"""

import json
import os
from pathlib import Path

import httpx
import pytest

from enmaas_cli.auth import resolve_key
from enmaas_cli.cli import run_ask, run_models
from enmaas_cli.client import call
from enmaas_cli.dialect import Dialect, build_request, parse_response
from enmaas_cli.runner import run_map

BASE = os.environ.get("ENMAAS_BASE_URL", "").rstrip("/")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not BASE, reason="ENMAAS_BASE_URL is not set; live tests need the gateway URL"
    ),
]
GLM = "rits/zai-org/glm-5-3"
DS = "rits/deepseek-ai/DeepSeek-V4.1-Flash-1017"


@pytest.fixture
def key() -> str:
    return resolve_key(key_file=None, env=dict(os.environ), home=Path.home()).value


async def test_both_catalogs_reachable(key: str) -> None:
    """Neither dialect's catalog is complete, so the merge is the real view."""
    async with httpx.AsyncClient() as client:
        rows = await run_models(client=client, base_url=BASE, key=key, dialect="both", timeout=60)
    ids = {r["id"] for r in rows}
    assert GLM in ids
    assert any(i.startswith("claude-") for i in ids)
    assert any(i.startswith("gpt-") for i in ids)
    by_id = {r["id"]: r for r in rows}
    assert by_id[GLM]["dialects"] == ["anthropic", "openai"]


@pytest.mark.parametrize(
    ("model", "dialect"),
    [(GLM, "openai"), (GLM, "anthropic"), (DS, "openai"), (DS, "anthropic")],
)
async def test_thinking_off_produces_text_and_no_reasoning(
    key: str, model: str, dialect: Dialect
) -> None:
    """Two of these four knob cells were never verified. This is that check."""
    request = build_request(
        model=model,
        prompt="Reply with exactly: OK",
        dialect=dialect,
        max_tokens=256,
        thinking=False,
        system=None,
        key=key,
    )
    async with httpx.AsyncClient() as client:
        data = await call(client, BASE, request, timeout=120, retries=2)
    completion = parse_response(data, dialect)
    assert "OK" in completion.text
    assert completion.reasoning == "", (
        f"{model} on {dialect} still produced {len(completion.reasoning)} chars of reasoning"
    )


async def test_ask_writes_a_real_response(key: str, tmp_path: Path) -> None:
    async with httpx.AsyncClient() as client:
        result = await run_ask(
            client=client,
            base_url=BASE,
            key=key,
            model=GLM,
            prompt="Name the capital of France in one word.",
            system=None,
            dialect=None,
            thinking=False,
            max_tokens=64,
            timeout=120,
            retries=2,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
        )
    assert result.exit_code == 0
    assert "Paris" in result.path.read_text(encoding="utf-8")


async def test_usage_reports_nonzero_input_tokens(key: str, tmp_path: Path) -> None:
    """Anthropic's input_tokens excludes cache; a naive read reports ~25."""
    request = build_request(
        model=DS,
        prompt="Reply with exactly: OK",
        dialect="anthropic",
        max_tokens=64,
        thinking=False,
        system=None,
        key=key,
    )
    async with httpx.AsyncClient() as client:
        data = await call(client, BASE, request, timeout=120, retries=2)
    usage = parse_response(data, "anthropic").usage
    assert usage.input_tokens is not None
    assert usage.input_tokens > 0


async def test_small_map_run_completes(key: str, tmp_path: Path) -> None:
    items = tmp_path / "items.jsonl"
    items.write_text(
        "\n".join(json.dumps({"id": f"n{n}", "number": n}) for n in range(1, 6)) + "\n",
        encoding="utf-8",
    )
    template = tmp_path / "t.txt"
    template.write_text(
        "Reply with only the number {number} doubled. Digits only, no words.",
        encoding="utf-8",
    )
    async with httpx.AsyncClient() as client:
        outcome = await run_map(
            client=client,
            base_url=BASE,
            key=key,
            model=GLM,
            items_path=items,
            template_path=template,
            outdir=tmp_path / "out",
            concurrency=3,
            retries=2,
            timeout=120,
            thinking=False,
            max_tokens=64,
            dialect=None,
            resume=False,
        )
    assert outcome.exit_code == 0
    assert outcome.ok == 5

    # Assert the mechanics of run_map, not the model's arithmetic. An earlier
    # version of this test checked that GLM correctly doubled 1 and got "11",
    # which made the suite flaky about something we do not control.
    outdir = tmp_path / "out"
    for n in range(1, 6):
        body = (outdir / f"n{n}.md").read_text(encoding="utf-8")
        assert body.strip(), f"n{n}.md is empty"

    rows = [
        json.loads(line)
        for line in (outdir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # The index interleaves per-item rows with one charge row per request.
    item_rows = [r for r in rows if r.get("type") != "request"]
    charges = [r for r in rows if r.get("type") == "request"]
    assert len(item_rows) == 5
    assert all(r["status"] == "ok" for r in item_rows)
    assert all(r["tokens"]["out"] for r in item_rows)
    # Five serial requests, each charged something against a real gateway.
    assert len(charges) == 5
    assert all(c["charge"] > 0 for c in charges)


async def test_bad_model_fails_fast_without_retrying(key: str, tmp_path: Path) -> None:
    """A rejected model is a request fault, so it must not be retried."""
    async with httpx.AsyncClient() as client:
        result = await run_ask(
            client=client,
            base_url=BASE,
            key=key,
            model="rits/zai-org/does-not-exist",
            prompt="hi",
            system=None,
            dialect="openai",
            thinking=False,
            max_tokens=32,
            timeout=60,
            retries=2,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
        )
    assert result.exit_code == 1
    assert "FAILED" in result.receipt


async def test_token_budget_stops_a_real_run(key: str, tmp_path: Path) -> None:
    """A tiny ceiling against the free model. Costs nothing.

    Calls run_map directly rather than shelling out to the CLI: a
    subprocess would read the real config and append to the real usage
    ledger, and after phase 2 an exhausted rolling budget could reject the
    test itself.
    """
    items = tmp_path / "items.jsonl"
    items.write_text(
        "\n".join(
            json.dumps({"id": f"i{n}", "q": "Name one colour. One word."}) for n in range(10)
        ),
        encoding="utf-8",
    )
    template = tmp_path / "t.txt"
    template.write_text("{q}", encoding="utf-8")

    async with httpx.AsyncClient() as client:
        outcome = await run_map(
            client=client,
            base_url=BASE,
            key=key,
            model=GLM,
            items_path=items,
            template_path=template,
            outdir=tmp_path / "out",
            concurrency=1,
            retries=0,
            timeout=120,
            thinking=False,
            max_tokens=None,
            dialect=None,
            resume=False,
            token_budget=50,
        )

    assert outcome.abort_reason == "budget", outcome
    assert outcome.exit_code == 3
    assert outcome.attempted < 10
    assert outcome.spent >= 50


async def test_default_batch_on_the_default_model_finishes_in_time(
    key: str, tmp_path: Path
) -> None:
    """Ten short items, one request, well inside the 280 s client deadline."""
    items = tmp_path / "items.jsonl"
    items.write_text(
        "\n".join(
            json.dumps({"id": f"i{n}", "word": w})
            for n, w in enumerate(
                ["red", "blue", "green", "cyan", "black", "white", "grey", "pink", "gold", "teal"]
            )
        )
        + "\n",
        encoding="utf-8",
    )
    template = tmp_path / "t.txt"
    template.write_text(
        "Answer with one word: is the colour warm or cool?\n===PER ITEM===\nCOLOUR: {word}\n",
        encoding="utf-8",
    )
    async with httpx.AsyncClient() as client:
        outcome = await run_map(
            client=client,
            base_url=BASE,
            key=key,
            model="rits/zai-org/glm-5-3",
            items_path=items,
            template_path=template,
            outdir=tmp_path / "out",
            concurrency=1,
            retries=0,
            timeout=280,
            thinking=False,
            max_tokens=None,
            dialect=None,
            resume=False,
        )
    assert outcome.batch == 10
    assert outcome.ok == 10
    assert outcome.elapsed < 280
    print(f"default batch live run: {outcome.elapsed:.1f}s for 10 items in 1 request")
