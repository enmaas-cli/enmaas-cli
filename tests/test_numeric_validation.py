"""Tests for numeric flag validation."""

import pytest

from enmaas_cli.cli import build_parser


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
