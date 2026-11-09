"""Gateway text is redacted, then made printable, then cut.

`cut_redacted` cuts text that is already redacted without splitting the
placeholder. `printable` makes text safe to print on one terminal line.
"""

from __future__ import annotations

import pytest

from enmaas_cli.auth import REDACTION, cut_redacted, printable


def test_cut_keeps_a_placeholder_that_straddles_the_limit() -> None:
    text = "a" * 10 + REDACTION + "b" * 10
    cut = cut_redacted(text, 15)
    assert cut == "a" * 10 + REDACTION


def test_cut_keeps_text_before_the_placeholder_at_the_limit() -> None:
    text = "a" * 10 + REDACTION + "b" * 10
    assert cut_redacted(text, 5) == "a" * 5
    assert cut_redacted(text, 10) == "a" * 10


def test_cut_at_a_placeholder_that_ends_before_the_limit() -> None:
    text = REDACTION + "b" * 20
    assert cut_redacted(text, len(REDACTION)) == REDACTION
    assert cut_redacted(text, len(REDACTION) + 3) == REDACTION + "bbb"


def test_cut_ignores_a_placeholder_after_the_limit() -> None:
    text = "a" * 20 + REDACTION
    assert cut_redacted(text, 20) == "a" * 20
    assert cut_redacted(text, 5) == "a" * 5


def test_cut_of_short_text_is_unchanged() -> None:
    assert cut_redacted("abc", 80) == "abc"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\nb", "a b"),
        ("a\rb", "a b"),
        ("a\tb", "a b"),
        ("a\x1bb", "a?b"),
        ("a\x07b", "a?b"),
        ("a\x7fb", "a?b"),
        ("a\x9bb", "a?b"),
        ("a‮b", "a?b"),
        ("a b", "a?b"),
        ("a b", "a?b"),
        ("a\x00b", "a?b"),
    ],
)
def test_printable_replaces_characters_that_are_not_printable(raw: str, expected: str) -> None:
    assert printable(raw) == expected


def test_printable_keeps_ordinary_non_ascii_letters() -> None:
    text = "héllo ñandú Ωmega 日本語 Привет çà"
    assert printable(text) == text


def test_printable_output_holds_only_printable_characters() -> None:
    text = "".join(chr(n) for n in range(0x00, 0x300)) + "  ‮​﻿"
    assert printable(text).isprintable()
