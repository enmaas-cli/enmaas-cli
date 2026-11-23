"""Tests for the dialect module: requests, replies and token counts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import tokens
from enmaas_cli.cli import main
from enmaas_cli.dialect import (
    ANTHROPIC_VERSION,
    DEFAULT_MAX_TOKENS,
    DialectError,
    build_request,
    parse_response,
    resolve_dialect,
    thinking_off_payload,
)
from enmaas_cli.errors import EmptyResponseError
from enmaas_cli.runner import run_map


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5-5", "anthropic"),
        ("claude-haiku-4-5", "anthropic"),
        ("gpt-6-sol", "openai"),
        ("gpt-4o-mini", "openai"),
        ("gemini-3.1-pro-preview", "openai"),
        ("codex-auto-review", "openai"),
        ("rits/zai-org/glm-5-3", "openai"),
        ("rits/deepseek-ai/DeepSeek-V4.1-Flash-1017", "openai"),
    ],
)
def test_known_prefixes(model: str, expected: str) -> None:
    assert resolve_dialect(model) == expected


def test_override_wins() -> None:
    assert resolve_dialect("rits/zai-org/glm-5-3", override="anthropic") == "anthropic"
    assert resolve_dialect("claude-opus-5-5", override="openai") == "openai"


def test_unknown_prefix_is_an_error_not_a_guess() -> None:
    with pytest.raises(DialectError, match="cannot determine dialect"):
        resolve_dialect("llama-4-70b")


def test_unknown_prefix_with_override_is_allowed() -> None:
    assert resolve_dialect("llama-4-70b", override="openai") == "openai"


def test_error_message_lists_known_prefixes() -> None:
    with pytest.raises(DialectError) as exc:
        resolve_dialect("mystery-model")
    assert "--dialect" in str(exc.value)


def test_openai_text_and_usage() -> None:
    data = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": "hello", "reasoning_content": "hmm"}}
        ],
        "usage": {
            "prompt_tokens": 9935,
            "completion_tokens": 1430,
            "completion_tokens_details": {"reasoning_tokens": 1088},
            "prompt_tokens_details": {"cached_tokens": 9216},
        },
    }
    c = parse_response(data, "openai")
    assert c.text == "hello"
    assert c.reasoning == "hmm"
    assert c.stop == "stop"
    # OpenAI prompt_tokens already includes the cached portion.
    assert c.usage.input_tokens == 9935
    assert c.usage.cached_tokens == 9216
    assert c.usage.output_tokens == 1430
    assert c.usage.reasoning_tokens == 1088


def test_anthropic_input_tokens_exclude_cache_and_must_be_summed() -> None:
    data = {
        "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "hello"}],
        "usage": {
            "input_tokens": 25,
            "output_tokens": 1430,
            "cache_read_input_tokens": 10240,
            "cache_creation_input_tokens": 0,
        },
    }
    c = parse_response(data, "anthropic")
    assert c.text == "hello"
    assert c.reasoning == "hmm"
    # 25 alone would understate real input by three orders of magnitude.
    assert c.usage.input_tokens == 10265
    assert c.usage.cached_tokens == 10240


def test_anthropic_concatenates_multiple_text_blocks() -> None:
    data = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    assert parse_response(data, "anthropic").text == "ab"


def test_missing_usage_reports_none_not_zero() -> None:
    data = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "hi"}]}
    u = parse_response(data, "anthropic").usage
    assert u.input_tokens is None
    assert u.output_tokens is None


def test_openai_empty_text_at_length_limit_raises() -> None:
    data = {
        "choices": [
            {"finish_reason": "length", "message": {"content": "", "reasoning_content": "x" * 100}}
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 8192,
            "completion_tokens_details": {"reasoning_tokens": 8192},
        },
    }
    with pytest.raises(EmptyResponseError) as exc:
        parse_response(data, "openai")
    assert "max_tokens" in str(exc.value)
    assert "--thinking off" in str(exc.value)


def test_anthropic_empty_text_at_max_tokens_raises() -> None:
    data = {
        "stop_reason": "max_tokens",
        "content": [{"type": "thinking", "thinking": "x" * 100}],
        "usage": {"input_tokens": 25, "output_tokens": 8192},
    }
    with pytest.raises(EmptyResponseError):
        parse_response(data, "anthropic")


def test_empty_text_with_normal_stop_is_not_an_error() -> None:
    """A model legitimately replying with nothing is odd but not a budget failure."""
    data = {
        "stop_reason": "end_turn",
        "content": [],
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    assert parse_response(data, "anthropic").text == ""


def test_text_at_length_limit_is_not_an_error() -> None:
    """Truncated but non-empty output is usable; the receipt flags the stop reason."""
    data = {
        "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    assert parse_response(data, "openai").text == "partial"


def test_empty_response_error_carries_the_usage_it_cost() -> None:
    """The README's DeepSeek case: reasoning burned the budget, no text came back.

    Those tokens were charged. An exception that drops the usage makes the
    most expensive failure mode invisible to any budget.
    """
    data = {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 32768,
            "completion_tokens_details": {"reasoning_tokens": 32768},
        },
    }
    with pytest.raises(EmptyResponseError) as exc:
        parse_response(data, "openai")
    assert exc.value.usage.input_tokens == 500
    assert exc.value.usage.output_tokens == 32768


# A lone surrogate is the one thing UTF-8 cannot encode, and a model can
# emit one: `\ud800` is a plain ASCII escape in the JSON body, so it
# survives transport and json.loads intact. Every downstream write
# assumed otherwise.
LONE_SURROGATE_BODY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "ok \ud800 bad"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
}


def test_openai_text_is_always_utf8_encodable() -> None:
    """The invariant every consumer of Completion.text relies on."""
    completion = parse_response(LONE_SURROGATE_BODY, "openai")
    completion.text.encode("utf-8")  # must not raise
    assert completion.text == "ok � bad"
    assert completion.replaced == 1


def test_anthropic_text_is_always_utf8_encodable() -> None:
    data = {
        "content": [{"type": "text", "text": "a \udfff b"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }
    completion = parse_response(data, "anthropic")
    completion.text.encode("utf-8")
    assert completion.text == "a � b"
    assert completion.replaced == 1


def test_reasoning_is_sanitised_too() -> None:
    """thinking.md is written with the same encoding as the response."""
    data = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "fine", "reasoning_content": "hmm \ud800"},
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    completion = parse_response(data, "openai")
    completion.reasoning.encode("utf-8")
    assert completion.replaced == 1


def test_clean_text_is_left_exactly_alone() -> None:
    """Sanitising must not touch astral planes, emoji, or combining marks."""
    tricky = "héllo 👩‍👩‍👧‍👦 \U0010ffff ﷽ é"
    data = {
        "choices": [{"finish_reason": "stop", "message": {"content": tricky}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    completion = parse_response(data, "openai")
    assert completion.text == tricky
    assert completion.replaced == 0


def test_several_bad_characters_are_all_counted() -> None:
    data = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "\ud800 a \udbff b \udfff"},
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    completion = parse_response(data, "openai")
    assert completion.text == "� a � b �"
    assert completion.replaced == 3


GLM = "rits/zai-org/glm-5-3"
DS = "rits/deepseek-ai/DeepSeek-V4.1-Flash-1017"
KEY = "sk-oai-test"


def test_default_max_tokens_is_flat() -> None:
    """A bigger budget was measured not to help; it only slows the failure."""
    assert DEFAULT_MAX_TOKENS == 8192


def test_glm_openai_uses_reasoning_effort() -> None:
    payload, warning = thinking_off_payload("openai", GLM)
    assert payload == {"reasoning_effort": "none"}
    assert warning is None


def test_deepseek_openai_uses_chat_template_kwargs() -> None:
    payload, warning = thinking_off_payload("openai", DS)
    assert payload == {"chat_template_kwargs": {"enable_thinking": False}}
    assert warning is None


def test_anthropic_always_uses_thinking_disabled() -> None:
    payload, warning = thinking_off_payload("anthropic", GLM)
    assert payload == {"thinking": {"type": "disabled"}}
    assert warning is None


def test_unknown_openai_model_warns_and_uses_dialect_default() -> None:
    payload, warning = thinking_off_payload("openai", "gpt-6-sol")
    assert payload == {"reasoning_effort": "none"}
    assert warning is not None
    assert "gpt-6-sol" in warning


def test_anthropic_request_shape() -> None:
    req = build_request(
        model="claude-opus-5-5",
        prompt="hi",
        dialect="anthropic",
        max_tokens=100,
        thinking=True,
        system=None,
        key=KEY,
    )
    assert req.path == "/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == ANTHROPIC_VERSION
    assert "Authorization" not in req.headers
    assert req.body["messages"] == [{"role": "user", "content": "hi"}]
    assert req.body["max_tokens"] == 100


def test_openai_request_shape() -> None:
    req = build_request(
        model=GLM,
        prompt="hi",
        dialect="openai",
        max_tokens=100,
        thinking=True,
        system=None,
        key=KEY,
    )
    assert req.path == "/v1/chat/completions"
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    assert "x-api-key" not in req.headers


def test_system_prompt_goes_to_top_level_on_anthropic() -> None:
    req = build_request(
        model="claude-opus-5-5",
        prompt="hi",
        dialect="anthropic",
        max_tokens=100,
        thinking=True,
        system="be terse",
        key=KEY,
    )
    assert req.body["system"] == "be terse"
    assert req.body["messages"] == [{"role": "user", "content": "hi"}]


def test_system_prompt_becomes_a_message_on_openai() -> None:
    req = build_request(
        model=GLM,
        prompt="hi",
        dialect="openai",
        max_tokens=100,
        thinking=True,
        system="be terse",
        key=KEY,
    )
    assert req.body["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]


def test_thinking_off_merges_knob_into_body() -> None:
    req = build_request(
        model=GLM,
        prompt="hi",
        dialect="openai",
        max_tokens=100,
        thinking=False,
        system=None,
        key=KEY,
    )
    assert req.body["reasoning_effort"] == "none"


def test_thinking_on_adds_no_knob() -> None:
    req = build_request(
        model=GLM,
        prompt="hi",
        dialect="openai",
        max_tokens=100,
        thinking=True,
        system=None,
        key=KEY,
    )
    assert "reasoning_effort" not in req.body
    assert "thinking" not in req.body


def test_key_is_never_in_the_body() -> None:
    for dialect in ("openai", "anthropic"):
        req = build_request(
            model=GLM,
            prompt="hi",
            dialect=dialect,
            max_tokens=100,
            thinking=True,
            system=None,
            key=KEY,
        )
        assert KEY not in str(req.body)


def test_unknown_model_with_thinking_off_carries_warning() -> None:
    req = build_request(
        model="gpt-6-sol",
        prompt="hi",
        dialect="openai",
        max_tokens=100,
        thinking=False,
        system=None,
        key=KEY,
    )
    assert len(req.warnings) == 1
    assert "gpt-6-sol" in req.warnings[0]


def test_known_model_with_thinking_off_has_no_warning() -> None:
    req = build_request(
        model=GLM,
        prompt="hi",
        dialect="openai",
        max_tokens=100,
        thinking=False,
        system=None,
        key=KEY,
    )
    assert req.warnings == ()


def test_thinking_on_has_no_warning() -> None:
    req = build_request(
        model="gpt-6-sol",
        prompt="hi",
        dialect="openai",
        max_tokens=100,
        thinking=True,
        system=None,
        key=KEY,
    )
    assert req.warnings == ()


def test_repr_does_not_leak_key() -> None:
    req = build_request(
        model=GLM,
        prompt="hi",
        dialect="openai",
        max_tokens=100,
        thinking=True,
        system=None,
        key=KEY,
    )
    r = repr(req)
    assert KEY not in r
    assert "Authorization" in r
    assert req.path in r


def test_gpt_models_get_max_completion_tokens() -> None:
    """gpt-6-sol rejects max_tokens with HTTP 400 and requires the other name.

    Confirmed live against the gateway: sending max_tokens makes the model
    unusable through this CLI, which is the model the skill recommends for a
    second opinion on code.
    """
    request = build_request(
        model="gpt-6-sol",
        prompt="hi",
        dialect="openai",
        max_tokens=256,
        thinking=True,
        system=None,
        key="k",
    )

    assert request.body["max_completion_tokens"] == 256
    assert "max_tokens" not in request.body


def test_codex_models_get_max_completion_tokens() -> None:
    """codex-auto-review rejects max_tokens with HTTP 400.

    Measured 2026-11-23 against the gateway:

        400 Unsupported parameter: 'max_tokens' is not supported with this
        model. Use 'max_completion_tokens' instead.

    The prefix list covered gpt- only, so this model was unusable through the
    CLI. It is the one an independent code or content review would reach for,
    because it is a different family from the model under review.
    """
    request = build_request(
        model="codex-auto-review",
        prompt="hi",
        dialect="openai",
        max_tokens=256,
        thinking=True,
        system=None,
        key="k",
    )

    assert request.body["max_completion_tokens"] == 256
    assert "max_tokens" not in request.body


def test_other_openai_models_keep_max_tokens() -> None:
    """The rename is per-model, not a blanket change to the dialect."""
    request = build_request(
        model=GLM,
        prompt="hi",
        dialect="openai",
        max_tokens=256,
        thinking=True,
        system=None,
        key="k",
    )

    assert request.body["max_tokens"] == 256
    assert "max_completion_tokens" not in request.body


def test_gemini_thinking_off_uses_minimal_not_none() -> None:
    """Gemini rejects reasoning_effort 'none' outright.

    Confirmed live: 'none' returns HTTP 400 naming the valid values as high,
    low, max, medium and minimal. 'minimal' is accepted and is the floor;
    it still spends about 59 reasoning tokens, so there is no true off
    switch for this model.
    """
    payload, warning = thinking_off_payload("openai", "gemini-3.1-pro-preview")

    assert payload == {"reasoning_effort": "minimal"}
    assert warning is not None
    assert "minimal" in warning


def test_gpt5_family_floors_at_minimal_and_warns() -> None:
    """gpt-5-mini rejects reasoning_effort 'none'; 'minimal' is its floor.

    Measured 2026-11-05: 40 of 40 batched items failed with the default
    knob, which is the silent path a caller cannot see.
    """
    payload, warning = thinking_off_payload("openai", "gpt-5-mini")
    assert payload == {"reasoning_effort": "minimal"}
    assert warning is not None
    assert "minimal" in warning


def test_gpt5_floor_does_not_catch_gpt5_point_one() -> None:
    """The floor prefix is the hyphenated 5-series only.

    gpt-5.1 models accept "none", so a bare "gpt-5" prefix would send them
    "minimal" and spend reasoning tokens the caller asked to switch off.
    """
    payload, warning = thinking_off_payload("openai", "gpt-5.1-mini")
    assert payload == {"reasoning_effort": "none"}
    assert warning is not None
    assert "minimal" not in warning


# A token count above 10**12 reads as not reported.
#
# Python refuses to print an integer of more than 4,300 digits. A count near
# that size, once added to another, could not be written to the index or the
# ledger, so one such reply ended the whole run.


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
