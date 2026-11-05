import pytest

from enmaas_cli.dialect import EmptyResponseError, parse_response


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
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": "fine", "reasoning_content": "hmm \ud800"},
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    completion = parse_response(data, "openai")
    completion.reasoning.encode("utf-8")
    assert completion.replaced == 1


def test_clean_text_is_left_exactly_alone() -> None:
    """Sanitising must not touch astral planes, emoji, or combining marks."""
    tricky = "héllo 👩‍👩‍👧‍👦 \U0010FFFF ﷽ é"
    data = {
        "choices": [{"finish_reason": "stop", "message": {"content": tricky}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    completion = parse_response(data, "openai")
    assert completion.text == tricky
    assert completion.replaced == 0


def test_several_bad_characters_are_all_counted() -> None:
    data = {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": "\ud800 a \udbff b \udfff"},
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    completion = parse_response(data, "openai")
    assert completion.text == "� a � b �"
    assert completion.replaced == 3
