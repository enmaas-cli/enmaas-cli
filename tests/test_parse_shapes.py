"""A reply field of the wrong type is a BadReplyError that keeps the charged usage.

A missing or null field still reads as empty, as it always has. Every error
carries the usage the reply reported, because those tokens were billed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import dialect as dialect_module
from enmaas_cli.cli import ExitCode, run_ask
from enmaas_cli.dialect import Completion, parse_response
from enmaas_cli.errors import BadReplyError, CallError
from enmaas_cli.tokens import Usage

OPENAI_USAGE = {"prompt_tokens": 11, "completion_tokens": 7}
ANTHROPIC_USAGE = {"input_tokens": 11, "output_tokens": 7}
EXPECTED = Usage(input_tokens=11, output_tokens=7)
MARK = "REPLY-CONTENT-MARK"


def _openai(**fields: Any) -> dict[str, Any]:
    return {"usage": OPENAI_USAGE, **fields}


def _openai_message(message: Any) -> dict[str, Any]:
    return _openai(choices=[{"finish_reason": "stop", "message": message}])


def _anthropic(**fields: Any) -> dict[str, Any]:
    return {"usage": ANTHROPIC_USAGE, "stop_reason": "end_turn", **fields}


def _bad(data: dict[str, Any], dialect: str) -> BadReplyError:
    with pytest.raises(BadReplyError) as exc:
        parse_response(data, dialect)  # type: ignore[arg-type]
    assert exc.value.usage == EXPECTED
    assert MARK not in str(exc.value)
    return exc.value


# OpenAI: choices


@pytest.mark.parametrize("choices", ["missing", None, []])
def test_openai_choices_missing_null_or_empty_is_empty(choices: Any) -> None:
    data = _openai() if choices == "missing" else _openai(choices=choices)
    c = parse_response(data, "openai")
    assert c.text == ""
    assert c.usage == EXPECTED


def test_openai_choices_list_of_objects_is_parsed() -> None:
    c = parse_response(_openai_message({"content": "hello"}), "openai")
    assert c.text == "hello"
    assert c.stop == "stop"
    assert c.usage == EXPECTED


@pytest.mark.parametrize("choices", [{}, MARK, 5, True, [MARK], [None], [[{}]]])
def test_openai_choices_of_any_other_shape_is_bad_reply(choices: Any) -> None:
    _bad(_openai(choices=choices), "openai")


# OpenAI: message


@pytest.mark.parametrize("message", ["missing", None])
def test_openai_message_missing_or_null_is_empty(message: Any) -> None:
    choice: dict[str, Any] = {"finish_reason": "stop"}
    if message != "missing":
        choice["message"] = message
    c = parse_response(_openai(choices=[choice]), "openai")
    assert c.text == ""
    assert c.usage == EXPECTED


def test_openai_message_object_is_parsed() -> None:
    c = parse_response(_openai_message({"content": "hi", "reasoning_content": "why"}), "openai")
    assert (c.text, c.reasoning) == ("hi", "why")


@pytest.mark.parametrize("message", [MARK, 5, [MARK], False])
def test_openai_message_of_any_other_type_is_bad_reply(message: Any) -> None:
    _bad(_openai_message(message), "openai")


# OpenAI: content


@pytest.mark.parametrize(("content", "text"), [("missing", ""), (None, ""), ("words", "words")])
def test_openai_content_missing_null_or_string_is_kept(content: Any, text: str) -> None:
    message = {} if content == "missing" else {"content": content}
    c = parse_response(_openai_message(message), "openai")
    assert c.text == text
    assert c.usage == EXPECTED


def test_openai_content_parts_join_text_parts_in_order() -> None:
    content = [
        {"type": "text", "text": "a"},
        {"type": "image_url", "image_url": {"url": "x"}},
        7,
        {"type": "text", "text": "b"},
    ]
    c = parse_response(_openai_message({"content": content}), "openai")
    assert c.text == "ab"
    assert c.usage == EXPECTED


@pytest.mark.parametrize(
    "content",
    [
        [],
        [1, 2],
        [{"type": "image_url", "image_url": {"url": MARK}}],
        [{"type": "text", "text": 5}],
        [{"type": "text"}],
        5,
        {"text": MARK},
        True,
    ],
)
def test_openai_content_without_a_text_part_or_of_another_type_is_bad_reply(
    content: Any,
) -> None:
    _bad(_openai_message({"content": content}), "openai")


# OpenAI: reasoning


def test_openai_reasoning_strings_are_kept() -> None:
    c = parse_response(_openai_message({"content": "x", "reasoning": "r"}), "openai")
    assert c.reasoning == "r"


@pytest.mark.parametrize(
    ("fields", "reasoning"),
    [
        ({"reasoning_content": 5}, ""),
        ({"reasoning": [MARK]}, ""),
        ({"reasoning_content": {"a": 1}, "reasoning": "fallback"}, "fallback"),
    ],
)
def test_openai_reasoning_of_another_type_reads_as_empty(
    fields: dict[str, Any], reasoning: str
) -> None:
    c = parse_response(_openai_message({"content": "x", **fields}), "openai")
    assert c.reasoning == reasoning
    assert c.text == "x"


# OpenAI: finish_reason


@pytest.mark.parametrize("stop", ["stop", None])
def test_openai_finish_reason_string_or_null_is_kept(stop: Any) -> None:
    data = _openai(choices=[{"finish_reason": stop, "message": {"content": "x"}}])
    assert parse_response(data, "openai").stop == stop


@pytest.mark.parametrize("stop", [5, ["length"], {"r": "length"}])
def test_openai_finish_reason_of_another_type_reads_as_null(stop: Any) -> None:
    data = _openai(choices=[{"finish_reason": stop, "message": {"content": "x"}}])
    assert parse_response(data, "openai").stop is None


# Anthropic: content


@pytest.mark.parametrize("content", ["missing", None])
def test_anthropic_content_missing_or_null_is_empty(content: Any) -> None:
    data = _anthropic() if content == "missing" else _anthropic(content=content)
    c = parse_response(data, "anthropic")
    assert c.text == ""
    assert c.usage == EXPECTED


def test_anthropic_content_list_of_objects_is_parsed() -> None:
    content = [
        {"type": "thinking", "thinking": "hm"},
        {"type": "text", "text": "a"},
        {"type": "tool_use", "input": {"k": 1}},
    ]
    c = parse_response(_anthropic(content=content), "anthropic")
    assert c.text == 'a{"k": 1}'
    assert c.reasoning == "hm"
    assert c.usage == EXPECTED


@pytest.mark.parametrize("content", [MARK, {}, 5, [MARK], [None], [{"type": "text"}, 3]])
def test_anthropic_content_not_a_list_of_objects_is_bad_reply(content: Any) -> None:
    _bad(_anthropic(content=content), "anthropic")


# Anthropic: text block


@pytest.mark.parametrize(("block", "text"), [({"text": "t"}, "t"), ({}, ""), ({"text": None}, "")])
def test_anthropic_text_string_missing_or_null_is_kept(block: dict[str, Any], text: str) -> None:
    content = [{"type": "text", **block}, {"type": "text", "text": "!"}]
    c = parse_response(_anthropic(content=content), "anthropic")
    assert c.text == text + "!"


@pytest.mark.parametrize("value", [5, [MARK], {"t": MARK}, True])
def test_anthropic_text_of_another_type_is_bad_reply(value: Any) -> None:
    _bad(_anthropic(content=[{"type": "text", "text": value}]), "anthropic")


# Anthropic: thinking block


def test_anthropic_thinking_string_is_kept() -> None:
    content = [{"type": "thinking", "thinking": "t"}, {"type": "text", "text": "x"}]
    assert parse_response(_anthropic(content=content), "anthropic").reasoning == "t"


@pytest.mark.parametrize("value", [5, [MARK], None, {"a": 1}])
def test_anthropic_thinking_of_another_type_reads_as_empty(value: Any) -> None:
    content = [
        {"type": "thinking", "thinking": value},
        {"type": "redacted_thinking", "thinking": "kept"},
        {"type": "text", "text": "x"},
    ]
    c = parse_response(_anthropic(content=content), "anthropic")
    assert c.reasoning == "kept"
    assert c.text == "x"


# Anthropic: stop_reason


@pytest.mark.parametrize("stop", ["end_turn", None])
def test_anthropic_stop_reason_string_or_null_is_kept(stop: Any) -> None:
    data = _anthropic(content=[{"type": "text", "text": "x"}])
    data["stop_reason"] = stop
    assert parse_response(data, "anthropic").stop == stop


@pytest.mark.parametrize("stop", [5, ["max_tokens"], {"r": 1}])
def test_anthropic_stop_reason_of_another_type_reads_as_null(stop: Any) -> None:
    data = _anthropic(content=[{"type": "text", "text": "x"}])
    data["stop_reason"] = stop
    assert parse_response(data, "anthropic").stop is None


# Safety net


ODD_REPLIES: list[dict[str, Any]] = [
    {"choices": [{"message": {"content": [1, 2]}}]},
    {"usage": []},
    {"content": [{"type": "text", "text": 5}]},
    {"usage": "x", "choices": "y", "content": "z"},
    {"choices": [{"message": {"content": [{"type": "text", "text": ["a"]}]}}]},
    {"choices": [{"message": {"reasoning_content": 1, "reasoning": 2}, "finish_reason": {}}]},
    {"content": [{"type": ["text"], "text": "x"}]},
    {"content": [{"type": {}, "thinking": "x"}]},
    {"content": [{"type": "tool_use", "input": float("nan")}]},
    {"usage": {"prompt_tokens": "9", "completion_tokens_details": [], "input_tokens": -1}},
    {"stop_reason": 7, "content": None},
]


@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
@pytest.mark.parametrize("data", ODD_REPLIES)
def test_odd_replies_give_only_a_completion_or_a_call_error(
    data: dict[str, Any], dialect: str
) -> None:
    try:
        result = parse_response(data, dialect)  # type: ignore[arg-type]
    except CallError:
        return
    assert isinstance(result, Completion)


@pytest.mark.parametrize(
    ("dialect", "data", "parser"),
    [
        ("openai", _openai(choices=[]), "_parse_openai"),
        ("anthropic", _anthropic(content=[]), "_parse_anthropic"),
    ],
)
def test_unexpected_parser_error_becomes_bad_reply_with_usage(
    monkeypatch: pytest.MonkeyPatch, dialect: str, data: dict[str, Any], parser: str
) -> None:
    def broken(data: dict[str, Any]) -> Completion:
        raise TypeError(MARK)

    monkeypatch.setattr(dialect_module, parser, broken)
    exc = _bad(data, dialect)
    assert str(exc) == "reply could not be read (TypeError)"


# run_ask


async def test_ask_with_parts_but_no_text_part_fails_and_keeps_usage(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_openai_message({"content": [{"type": "image_url", "image_url": {"url": MARK}}]}),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_ask(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model="rits/zai-org/glm-5-3",
            prompt="hi",
            system=None,
            dialect=None,
            thinking=False,
            max_tokens=None,
            timeout=5,
            retries=0,
            output=None,
            run_root=tmp_path,
            save_thinking=False,
        )
    assert result.exit_code == ExitCode.ERROR
    assert int(result.exit_code) == 1
    assert result.usage == EXPECTED
    assert result.completion is None
    assert MARK not in result.receipt
