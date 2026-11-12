"""A token count above 10**12 reads as not reported.

Python refuses to print an integer of more than 4,300 digits. A count near
that size, once added to another, could not be written to the index or the
ledger, so one such reply ended the whole run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import tokens
from enmaas_cli.cli import main
from enmaas_cli.dialect import parse_response
from enmaas_cli.runner import run_map

HUGE = int("9" * 4300)
OPENAI_HUGE = {
    "prompt_tokens": HUGE,
    "completion_tokens": HUGE,
    "prompt_tokens_details": {"cached_tokens": HUGE},
    "completion_tokens_details": {"reasoning_tokens": HUGE},
}
# Without a cached count the charge is the sum of both, which has 4,301 digits.
HUGE_SUM = {"prompt_tokens": HUGE, "completion_tokens": HUGE}
GOOD_USAGE = {"prompt_tokens": 5, "completion_tokens": 2}


def _openai(usage: dict[str, Any], content: str = "ok") -> dict[str, Any]:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": usage,
    }


def test_openai_counts_above_the_cap_read_as_not_reported() -> None:
    usage = parse_response(_openai(OPENAI_HUGE), "openai").usage
    assert usage.input_tokens is None
    assert usage.cached_tokens is None
    assert usage.output_tokens is None
    assert usage.reasoning_tokens is None


def test_anthropic_counts_above_the_cap_read_as_not_reported() -> None:
    data = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": HUGE,
            "cache_read_input_tokens": HUGE,
            "cache_creation_input_tokens": HUGE,
            "output_tokens": HUGE,
        },
    }
    usage = parse_response(data, "anthropic").usage
    assert usage.input_tokens is None
    assert usage.cached_tokens is None
    assert usage.output_tokens is None


def test_the_cap_itself_is_kept_and_one_more_is_not() -> None:
    assert tokens.MAX_TOKEN_COUNT == 10**12
    kept = parse_response(_openai({"prompt_tokens": 10**12}), "openai").usage
    assert kept.input_tokens == 10**12
    dropped = parse_response(_openai({"prompt_tokens": 10**12 + 1}), "openai").usage
    assert dropped.input_tokens is None


def _ids_in(request: httpx.Request) -> list[str]:
    prompt = request.content.decode("utf-8")
    return re.findall(r"=== ITEM (\w+) ===", prompt) or re.findall(r"do (\w+)", prompt)[:1]


@pytest.mark.parametrize("batched", [False, True], ids=["single", "batched"])
async def test_map_completes_every_item_when_one_reply_has_huge_counts(
    tmp_path: Path, batched: bool
) -> None:
    seen = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        ids = _ids_in(request)
        if batched:
            text = "\n".join(f"=== ANSWER {i} ===\ndone {i}" for i in ids)
        else:
            text = f"done {ids[0]}"
        usage = HUGE_SUM if seen == 2 else GOOD_USAGE
        return httpx.Response(200, json=_openai(usage, text))

    items = tmp_path / "items.jsonl"
    items.write_text(
        "\n".join(json.dumps({"id": i, "x": i}) for i in ["a", "b", "c"]) + "\n",
        encoding="utf-8",
    )
    template = tmp_path / "t.txt"
    template.write_text("shared\n===PER ITEM===\ndo {x}" if batched else "do {x}", encoding="utf-8")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        outcome = await run_map(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model="rits/zai-org/glm-5-3",
            items_path=items,
            template_path=template,
            outdir=tmp_path / "out",
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect="openai",
            resume=False,
            batch=2 if batched else 1,
        )
    assert outcome.ok == 3
    assert outcome.failed == 0
    assert seen == (2 if batched else 3)


def test_ask_with_huge_counts_exits_0_and_records_one_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai(HUGE_SUM))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "-p", "hi", "--retries", "0"]
    assert main(argv) == 0
    capsys.readouterr()
    ledger = tmp_path / "state" / "enmaas" / "runs" / "usage.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["input_tokens"] is None
