import pytest

from enmaas_cli.dialect import DialectError, resolve_dialect


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
