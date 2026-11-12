"""Tests for the cli module: the parser, helpers and the checks before a request."""

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from enmaas_cli.budget import rolling_check
from enmaas_cli.cli import (
    _resolve_dialect_override,
    _resolve_model,
    assemble_prompt,
    build_parser,
    main,
    resolve_thinking,
    run_models,
)
from enmaas_cli.config import Config, load_config
from enmaas_cli.errors import ExitCode


def test_files_are_wrapped_in_labelled_blocks(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("contents here", encoding="utf-8")
    out = assemble_prompt(prompt="review this", files=[f])
    assert "review this" in out
    assert f"--- file: {f} ---" in out
    assert "contents here" in out
    assert f"--- end: {f} ---" in out


def test_multiple_files_keep_order(tmp_path: Path) -> None:
    first, second = tmp_path / "1.txt", tmp_path / "2.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    out = assemble_prompt(prompt="p", files=[first, second])
    assert out.index("one") < out.index("two")


def test_missing_file_fails_before_any_request(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        assemble_prompt(prompt="p", files=[tmp_path / "nope.txt"])


def test_undecodable_bytes_are_replaced_not_fatal(tmp_path: Path) -> None:
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\xff\xfe valid tail")
    out = assemble_prompt(prompt="p", files=[f])
    assert "valid tail" in out


def test_parser_rejects_stdout_for_map() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            ["map", "-m", "x", "--items", "i", "--template", "t", "--outdir", "o", "--stdout"]
        )
    assert exc_info.value.code == 1


def test_unknown_flag_exits_1() -> None:
    """Unknown flags should exit 1, not 2."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["ask", "-p", "hi", "--unknown-flag"])
    assert exc_info.value.code == 1


def test_ask_has_retries_flag() -> None:
    """ask should accept --retries flag."""
    parser = build_parser()
    args = parser.parse_args(["ask", "-p", "hi", "--retries", "0"])
    assert args.retries == 0
    args = parser.parse_args(["ask", "-p", "hi"])
    assert args.retries == 3  # default


def test_thinking_flag_defaults_to_none() -> None:
    """Thinking flag should default to None so we can distinguish unset from explicit."""
    args = build_parser().parse_args(["ask", "-p", "hi"])
    assert args.thinking is None


def test_resolve_thinking_with_rits_model_unset() -> None:
    """Unset flag plus a rits/* model gives False."""
    assert resolve_thinking(None, "rits/zai-org/glm-5-3") is False


def test_resolve_thinking_with_frontier_model_unset() -> None:
    """Unset flag plus a frontier model gives True."""
    assert resolve_thinking(None, "gpt-6-sol") is True
    assert resolve_thinking(None, "claude-opus-5-5") is True


def test_resolve_thinking_with_explicit_off() -> None:
    """Explicit off gives False for any model."""
    assert resolve_thinking("off", "rits/zai-org/glm-5-3") is False
    assert resolve_thinking("off", "gpt-6-sol") is False
    assert resolve_thinking("off", "claude-opus-5-5") is False


def test_resolve_thinking_with_explicit_on() -> None:
    """Explicit on gives True for any model."""
    assert resolve_thinking("on", "rits/zai-org/glm-5-3") is True
    assert resolve_thinking("on", "gpt-6-sol") is True
    assert resolve_thinking("on", "claude-opus-5-5") is True


def test_exit_codes_are_distinct() -> None:
    values = [c.value for c in ExitCode]
    assert values == sorted(set(values))
    assert ExitCode.OK.value == 0
    assert ExitCode.ERROR.value == 1
    assert ExitCode.PARTIAL.value == 2
    assert ExitCode.ABORTED.value == 3


@pytest.mark.parametrize(
    "argv",
    [
        ["models"],
        ["ask", "-p", "hi"],
        ["map", "-m", "x", "--items", "i", "--template", "t", "--outdir", "o"],
    ],
)
def test_every_subcommand_defines_what_dispatch_reads(argv: list[str]) -> None:
    """_dispatch reads these for every command, so every subparser must define them.

    Caught live: `models` omitted --timeout and `enmaas models` crashed with
    AttributeError before reaching the gateway.
    """
    args = build_parser().parse_args(argv)
    for attribute in ("timeout", "json", "dialect", "command"):
        assert hasattr(args, attribute), f"{argv[0]} is missing {attribute}"


def _spend_row(tokens: int, *, days_ago: int = 0) -> str:
    """One serialised ok event costing exactly `tokens` metered tokens."""
    return json.dumps(
        {
            "timestamp": (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
            "command": "ask",
            "model": "m",
            "dialect": "openai",
            "input_tokens": tokens,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "elapsed": 1.0,
            "status": "ok",
            "schema_used": False,
            "error_kind": None,
            "items_total": None,
            "items_ok": None,
            "items_failed": None,
        }
    )


def test_rolling_check_refuses_once_the_budget_is_spent(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(_spend_row(60_000) + "\n", encoding="utf-8")
    config = Config(budget_window_days=30, budget_tokens=50_000)

    check = rolling_check(config, ledger)

    assert check.message is not None
    assert "60,000 of 50,000" in check.message


def test_rolling_check_allows_work_under_the_ceiling(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(_spend_row(10_000) + "\n", encoding="utf-8")
    config = Config(budget_window_days=30, budget_tokens=50_000)

    check = rolling_check(config, ledger)

    assert check.message is None
    assert check.spent == 10_000
    assert check.ceiling == 50_000


def test_spend_outside_the_window_does_not_count(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(_spend_row(60_000, days_ago=31) + "\n", encoding="utf-8")
    config = Config(budget_window_days=30, budget_tokens=50_000)

    check = rolling_check(config, ledger)
    assert check.message is None
    # Not just "allowed": the ceiling must survive, because run_map
    # seeds its rolling accumulator from it. Returning (0, None, None)
    # would pass a bare message check while silently dropping it.
    assert check.spent == 0
    assert check.ceiling == 50_000


def test_a_corrupt_ledger_blocks_only_when_a_budget_is_configured(
    tmp_path: Path,
) -> None:
    """Enforcement that cannot read its own state must not wave work through.

    With no budget configured the same ledger is just a notebook, and a
    damaged notebook must not stop work.
    """
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("not json at all\n", encoding="utf-8")

    enforced = Config(budget_window_days=30, budget_tokens=50_000)
    assert rolling_check(enforced, ledger).message is not None
    assert rolling_check(Config(), ledger).message is None


def test_a_missing_ledger_never_blocks(tmp_path: Path) -> None:
    """A fresh install has spent nothing."""
    config = Config(budget_window_days=30, budget_tokens=50_000)
    check = rolling_check(config, tmp_path / "nope.jsonl")
    assert check.message is None
    assert check.spent == 0
    assert check.ceiling == 50_000


def test_disabled_logging_blocks_a_configured_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spend that is never recorded means a ceiling that can never bind."""
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(_spend_row(10) + "\n", encoding="utf-8")

    enforced = Config(budget_window_days=30, budget_tokens=50_000)
    message = rolling_check(enforced, ledger).message
    assert message is not None
    assert "ENMAAS_NO_USAGE_LOG" in message
    # Without a budget it is merely a preference, so it must not block.
    assert rolling_check(Config(), ledger).message is None


# Model and dialect resolution shared by ask and map.


def test_explicit_model_wins() -> None:
    model, defaulted = _resolve_model("gpt-6-sol", Config())
    assert model == "gpt-6-sol"
    assert defaulted is False


def test_missing_model_uses_config_default() -> None:
    # Not the built-in default: a resolver that ignored config and
    # hardcoded GLM would pass otherwise.
    cfg = Config(default_model="claude-haiku-5-5")
    model, defaulted = _resolve_model(None, cfg)
    assert model == "claude-haiku-5-5"
    assert defaulted is True


def test_dialect_flag_wins_over_override() -> None:
    cfg = Config(dialect_overrides={"m": "anthropic"})
    assert _resolve_dialect_override("m", "openai", cfg) == "openai"


def test_dialect_override_applies_when_flag_absent() -> None:
    cfg = Config(dialect_overrides={"m": "anthropic"})
    assert _resolve_dialect_override("m", None, cfg) == "anthropic"


def test_invalid_override_is_ignored_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[dialect_overrides]\nm = "cobol"\n', encoding="utf-8")
    cfg = load_config(path)
    assert _resolve_dialect_override("m", None, cfg) is None
    assert cfg.warnings == ["invalid dialect override 'cobol' for m, ignoring"]


def test_invalid_override_is_reported_when_the_config_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The warning comes before the gateway URL is resolved, so it is printed
    # even though the command stops there and never resolves a dialect.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ENMAAS_BASE_URL", raising=False)
    path = tmp_path / "config.toml"
    path.write_text('[dialect_overrides]\nm = "cobol"\n', encoding="utf-8")
    assert main(["--config", str(path), "ask", "-m", "m", "--dialect", "openai", "-p", "hi"]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "warning: invalid dialect override 'cobol' for m, ignoring"


# Tests for numeric flag validation.


def test_timeout_zero_exits_1() -> None:
    """--timeout 0 should exit 1 with a message."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["ask", "-p", "hi", "--timeout", "0"])
    assert exc_info.value.code == 1


def test_concurrency_zero_exits_1() -> None:
    """--concurrency 0 should exit 1 with a message."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "map",
                "-m",
                "x",
                "--items",
                "i",
                "--template",
                "t",
                "--outdir",
                "o",
                "--concurrency",
                "0",
            ]
        )
    assert exc_info.value.code == 1


def test_max_tokens_zero_exits_1() -> None:
    """--max-tokens 0 should exit 1 with a message."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["ask", "-p", "hi", "--max-tokens", "0"])
    assert exc_info.value.code == 1


def test_positive_values_work() -> None:
    """Valid positive values should work."""
    parser = build_parser()
    args = parser.parse_args(["ask", "-p", "hi", "--timeout", "5.0", "--max-tokens", "100"])
    assert args.timeout == 5.0
    assert args.max_tokens == 100


# Tests for models command error handling.


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_models_401_raises_http_error() -> None:
    """Verify that a 401 response raises HTTPError (to be caught by _dispatch)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    async with _client(handler) as client:
        try:
            await run_models(
                client=client,
                base_url="https://g.test",
                key="sk-test",
                dialect="both",
                timeout=5.0,
            )
            raise AssertionError("Expected HTTPError to be raised")
        except httpx.HTTPError:
            pass  # Expected


# Tests for filesystem error handling.


def test_permission_error_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PermissionError should exit 1 with an error message, not a traceback."""
    # The CLI reads ENMAAS_KEY. This previously set ENMAAS_API_KEY, which it
    # ignores, so the test only passed on a machine that happened to have a key
    # at the ~/.secrets/enmaas fallback. On a clean runner it failed with "no
    # key found" long before reaching the permission error it meant to check.
    monkeypatch.setenv("ENMAAS_KEY", "sk-test-key")

    # Create a config file
    config = tmp_path / "config.toml"
    config.write_text(
        """
base_url = "https://mock.example.com"
default_model = "rits/zai-org/glm-5-3"
""",
        encoding="utf-8",
    )

    # Capture stderr
    stderr_capture = StringIO()
    monkeypatch.setattr(sys, "stderr", stderr_capture)

    # Simulate a permission error when creating the output directory
    output_path = str(tmp_path / "ro" / "out.md")
    with patch("pathlib.Path.mkdir", side_effect=PermissionError("Permission denied")):
        exit_code = main(["--config", str(config), "ask", "-p", "hi", "-o", output_path])

    assert exit_code == 1
    stderr_output = stderr_capture.getvalue()
    assert "error:" in stderr_output
    assert "Permission denied" in stderr_output
    # Should not contain Python traceback markers
    assert "Traceback" not in stderr_output
