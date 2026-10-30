from pathlib import Path

import pytest

from enmaas_cli.cli import ExitCode, assemble_prompt, build_parser, resolve_thinking


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
