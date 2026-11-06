"""Map a model ID to a wire protocol, and build/parse requests for it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Dialect = Literal["anthropic", "openai"]

ANTHROPIC_VERSION = "2023-06-01"

_ANTHROPIC_PREFIXES = ("claude-",)
_OPENAI_PREFIXES = ("gpt-", "gemini-", "codex-", "rits/")


class DialectError(ValueError):
    """The model ID does not map to a known wire protocol."""


def resolve_dialect(model: str, override: Dialect | None = None) -> Dialect:
    """Return the wire protocol for a model ID.

    ``rits/*`` models work on both paths; OpenAI is the default because the
    EnMaaS documentation reports that path as verified.
    """
    if override is not None:
        return override
    if model.startswith(_ANTHROPIC_PREFIXES):
        return "anthropic"
    if model.startswith(_OPENAI_PREFIXES):
        return "openai"
    known = ", ".join(_ANTHROPIC_PREFIXES + _OPENAI_PREFIXES)
    raise DialectError(
        f"cannot determine dialect for model {model!r}; "
        f"known prefixes are {known}. Pass --dialect to force one."
    )


DEFAULT_MAX_TOKENS = 8192

# Measured 2026-10-24. No single knob is portable: chat_template_kwargs does not
# silence GLM, and reasoning_effort is not accepted by every upstream.
_OPENAI_THINKING_OFF: dict[str, dict[str, Any]] = {
    "rits/zai-org/glm-5-3": {"reasoning_effort": "none"},
    "rits/deepseek-ai/": {"chat_template_kwargs": {"enable_thinking": False}},
}
_OPENAI_THINKING_OFF_DEFAULT: dict[str, Any] = {"reasoning_effort": "none"}
_ANTHROPIC_THINKING_OFF: dict[str, Any] = {"thinking": {"type": "disabled"}}

# Models with a reasoning floor rather than an off switch. Measured 2026-11-03
# against the gateway: gemini rejects "none" with a 400 naming the values it
# will take, and "minimal" still spends about 59 reasoning tokens. Disabling
# is therefore impossible, and saying so beats sending a value that fails.
_OPENAI_THINKING_FLOOR: dict[str, tuple[dict[str, Any], str]] = {
    "gemini-": (
        {"reasoning_effort": "minimal"},
        "gemini rejects reasoning_effort 'none'; sending 'minimal', which is "
        "the floor for this model and still spends reasoning tokens",
    ),
    # Measured 2026-11-05: gpt-5-mini returns 400 for "none". "minimal" is
    # the lowest value it accepts. gpt-6-* is in neither map and takes the
    # unverified "none" fallback with a warning, which works for it, so the
    # prefix is the hyphenated 5-series only.
    "gpt-5-": (
        {"reasoning_effort": "minimal"},
        "gpt-5-* rejects reasoning_effort 'none'; sending 'minimal', which is "
        "the floor for this family and still spends reasoning tokens",
    ),
}

# Reasoning-family models took max_completion_tokens when max_tokens was
# retired. Sending the old name is a 400 at request validation, before any
# inference, so the model is unusable rather than merely degraded.
_OPENAI_COMPLETION_TOKEN_PREFIXES = ("gpt-",)


def openai_token_field(model: str) -> str:
    """The name this model wants for its output-token ceiling."""
    if model.startswith(_OPENAI_COMPLETION_TOKEN_PREFIXES):
        return "max_completion_tokens"
    return "max_tokens"


@dataclass(frozen=True)
class Request:
    """A built HTTP request, independent of any client."""

    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __repr__(self) -> str:
        safe_headers = {k: "<redacted>" for k in self.headers}
        return (
            f"Request(path={self.path!r}, headers={safe_headers!r}, "
            f"body={self.body!r}, warnings={self.warnings!r})"
        )


def default_max_tokens(*, thinking: bool) -> int:
    """Flat default.

    Raising this for reasoning was measured not to help: 32768 still produced
    zero text on a 45 KB prompt, it just took five minutes to fail. The
    parameter is kept so callers need not special-case it.
    """
    del thinking  # Kept for API consistency but not used
    return DEFAULT_MAX_TOKENS


def thinking_off_payload(dialect: Dialect, model: str) -> tuple[dict[str, Any], str | None]:
    """Return the body fragment that disables reasoning, plus any warning."""
    if dialect == "anthropic":
        return dict(_ANTHROPIC_THINKING_OFF), None
    for prefix, (payload, note) in _OPENAI_THINKING_FLOOR.items():
        if model.startswith(prefix):
            return dict(payload), note
    for prefix, payload in _OPENAI_THINKING_OFF.items():
        if model.startswith(prefix):
            return dict(payload), None
    return (
        dict(_OPENAI_THINKING_OFF_DEFAULT),
        f"no verified reasoning-disable knob for {model!r}; "
        f"applying the OpenAI default, which may have no effect",
    )


def build_request(
    *,
    model: str,
    prompt: str,
    dialect: Dialect,
    max_tokens: int,
    thinking: bool,
    system: str | None,
    key: str,
    schema: dict[str, Any] | None = None,
    schema_name: str | None = None,
) -> Request:
    """Build a dialect-correct request. The key goes in headers, never the body."""
    body: dict[str, Any] = {"model": model}

    if dialect == "anthropic":
        body["max_tokens"] = max_tokens
        path = "/v1/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body["messages"] = [{"role": "user", "content": prompt}]
        if system is not None:
            body["system"] = system
    else:
        body[openai_token_field(model)] = max_tokens
        path = "/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "content-type": "application/json",
        }
        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body["messages"] = messages

    warnings: list[str] = []
    if not thinking:
        payload, warning = thinking_off_payload(dialect, model)
        body.update(payload)
        if warning is not None:
            warnings.append(warning)

    if schema is not None:
        if schema_name is None:
            schema_name = "StructuredOutput"
        if dialect == "anthropic":
            body["tools"] = [{"name": schema_name, "input_schema": schema}]
            body["tool_choice"] = {"type": "tool", "name": schema_name}
            warnings.append(
                "schema enforcement is advisory on the Anthropic dialect; "
                "client-side validation will run on the response"
            )
        else:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            }

    return Request(path=path, headers=headers, body=body, warnings=tuple(warnings))


_TRUNCATED_STOPS = frozenset({"length", "max_tokens"})


@dataclass(frozen=True)
class Usage:
    """Token counts normalised across dialects. None means 'not reported'.

    `input_tokens` is the inclusive total on both dialects and
    `cached_tokens` the subset of it that was a cache read.

    `output_tokens` is likewise complete: on OpenAI it is
    `completion_tokens`, which already contains reasoning, and
    `reasoning_tokens` is a breakdown of it rather than an addition. On
    Anthropic `reasoning_tokens` is always None because thinking is inside
    `output_tokens`. Never sum the two.
    """

    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None


class EmptyResponseError(Exception):
    """HTTP 200 but no usable text. Treated as a failure, never a success.

    Carries the usage, because these tokens were charged. The gateway bills
    a run that reasoned itself to death just as it bills a useful one, so a
    budget that could not see this would be blind to the most expensive
    failure the README documents.

    Defined below Usage so the annotation resolves without a forward
    reference.
    """

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage if usage is not None else Usage()


@dataclass(frozen=True)
class Completion:
    """A parsed response.

    Anything built by `parse_response` has `text` and `reasoning` that
    are UTF-8 encodable, which is what the five places that write or
    print them rely on. Constructing one directly bypasses that, so
    don't: go through the parsers.

    The guarantee is not enforced in __post_init__ on purpose. Raising
    there would turn a model quirk back into the crash this exists to
    prevent, and re-encoding every response to check would repeat work
    the parsers have already done.
    """

    text: str
    reasoning: str
    stop: str | None
    usage: Usage
    # How many codepoints had to be replaced to keep that guarantee.
    # Carried rather than discarded: silently altering a model's output
    # and saying nothing is its own kind of wrong.
    replaced: int = 0


# The only codepoints UTF-8 cannot encode. Verified exhaustively: of the
# 1,114,112 codepoints, exactly these 2,048 raise UnicodeEncodeError, so
# replacing them makes the encodable guarantee total rather than partial.
_UNENCODABLE = re.compile("[\ud800-\udfff]")

# U+FFFD REPLACEMENT CHARACTER, the standard marker for "something was
# here that could not be represented". Chosen over dropping the
# character, which would hide the damage, and over errors="replace" on
# encode, which yields a bare "?" indistinguishable from real content.
_REPLACEMENT = "�"


def _utf8_safe(text: str) -> tuple[str, int]:
    """Return text that can be encoded as UTF-8, and how much was replaced.

    A model can emit a lone surrogate: `\\ud800` is plain ASCII in the
    JSON body, so it survives transport and json.loads intact and only
    fails later, at the point of writing the response to disk. That
    raised UnicodeEncodeError, which is a ValueError rather than an
    OSError and so matched none of the handlers, taking down an entire
    map run over one bad character in one item.

    Fast path first: the overwhelming majority of responses are clean,
    and encoding is a C-level scan, so the regex only runs on the rare
    broken one.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return _UNENCODABLE.subn(_REPLACEMENT, text)
    return text, 0


def _parse_openai(data: dict[str, Any]) -> Completion:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    text, text_replaced = _utf8_safe(message.get("content") or "")
    reasoning, reasoning_replaced = _utf8_safe(
        message.get("reasoning_content") or message.get("reasoning") or ""
    )
    return Completion(
        text=text,
        reasoning=reasoning,
        replaced=text_replaced + reasoning_replaced,
        stop=choice.get("finish_reason"),
        usage=Usage(
            # prompt_tokens already includes the cached portion.
            input_tokens=usage.get("prompt_tokens"),
            cached_tokens=prompt_details.get("cached_tokens"),
            output_tokens=usage.get("completion_tokens"),
            reasoning_tokens=details.get("reasoning_tokens"),
        ),
    )


def _parse_anthropic(data: dict[str, Any]) -> Completion:
    import json as json_module

    blocks = data.get("content") or []
    usage = data.get("usage") or {}

    base = usage.get("input_tokens")
    cache_read = usage.get("cache_read_input_tokens")
    cache_write = usage.get("cache_creation_input_tokens")
    # input_tokens EXCLUDES cached tokens on this dialect, so sum what is present.
    total_input: int | None
    if base is None and cache_read is None and cache_write is None:
        total_input = None
    else:
        total_input = (base or 0) + (cache_read or 0) + (cache_write or 0)

    text_parts: list[str] = []
    for b in blocks:
        if b.get("type") == "text":
            text_parts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            tool_input = b.get("input", {})
            text_parts.append(json_module.dumps(tool_input))

    text, text_replaced = _utf8_safe("".join(text_parts))
    reasoning, reasoning_replaced = _utf8_safe(
        "".join(
            b.get("thinking", "")
            for b in blocks
            if b.get("type") in ("thinking", "redacted_thinking")
        )
    )
    return Completion(
        text=text,
        reasoning=reasoning,
        replaced=text_replaced + reasoning_replaced,
        stop=data.get("stop_reason"),
        usage=Usage(
            input_tokens=total_input,
            cached_tokens=cache_read,
            output_tokens=usage.get("output_tokens"),
            reasoning_tokens=None,
        ),
    )


def parse_response(data: dict[str, Any], dialect: Dialect) -> Completion:
    """Parse a 200 response, raising when it contains nothing usable."""
    completion = _parse_anthropic(data) if dialect == "anthropic" else _parse_openai(data)
    if not completion.text and completion.stop in _TRUNCATED_STOPS:
        out = completion.usage.output_tokens
        raise EmptyResponseError(
            f"exhausted max_tokens ({out if out is not None else 'unknown'} output "
            f"tokens, stop={completion.stop}) during reasoning, no text produced; "
            f"raise --max-tokens or set --thinking off",
            completion.usage,
        )
    return completion
