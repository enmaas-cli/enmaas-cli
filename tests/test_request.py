from enmaas_cli.dialect import (
    ANTHROPIC_VERSION,
    build_request,
    default_max_tokens,
    thinking_off_payload,
)

GLM = "rits/zai-org/glm-5-3"
DS = "rits/deepseek-ai/DeepSeek-V4.1-Flash-1017"
KEY = "sk-oai-test"


def test_default_max_tokens_is_flat() -> None:
    """A bigger budget was measured not to help; it only slows the failure."""
    assert default_max_tokens(thinking=True) == 8192
    assert default_max_tokens(thinking=False) == 8192


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
