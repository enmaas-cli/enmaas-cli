"""Tests for batching several map items into one request.

One call per item re-sends the whole template every time. Measured on a
real job: a 435 token template against 141 token items, so 75% of every
prompt was boilerplate. Batching ten items cuts input by about two thirds
and, as a side effect, lifts the prompt over the roughly 1,024 token floor
below which the gateway will not cache a prefix at all.
"""

from __future__ import annotations

import pytest

from enmaas_cli.batching import (
    PER_ITEM_MARKER,
    build_batch_prompt,
    split_batch_response,
    split_template,
)
from enmaas_cli.schema import ValidationError

TEMPLATE = f"""You classify findings. Answer A, B or C.

{PER_ITEM_MARKER}
TEXT: {{text}}
WHERE: {{where}}
"""

ITEMS = [
    {"id": "a1", "text": "first text", "where": "ch1"},
    {"id": "b2", "text": "second text", "where": "ch2"},
]


def test_split_template_separates_preamble_from_per_item() -> None:
    preamble, per_item = split_template(TEMPLATE)

    assert "You classify findings" in preamble
    assert "{text}" not in preamble, "the preamble must not vary per item"
    assert "{text}" in per_item
    assert "{where}" in per_item


def test_a_template_without_the_marker_is_rejected() -> None:
    """Batching a template with no marker would silently re-send everything."""
    with pytest.raises(ValidationError) as excinfo:
        split_template("no marker here {text}")

    assert PER_ITEM_MARKER in str(excinfo.value)


def test_the_batch_prompt_carries_the_preamble_once() -> None:
    """This is the whole point: the shared part is sent one time."""
    preamble, per_item = split_template(TEMPLATE)

    prompt = build_batch_prompt(preamble, per_item, ITEMS)

    assert prompt.count("You classify findings") == 1
    assert "first text" in prompt
    assert "second text" in prompt


def test_the_batch_prompt_is_smaller_than_separate_prompts() -> None:
    """Sized like the real job this was built for, not a toy.

    The triage template measured 1,743 characters of shared instructions
    against items averaging 566. A fixture with a two-line preamble would
    show almost no saving and prove nothing, because the saving is entirely
    a function of how much of each prompt is shared.
    """
    realistic = (
        "You classify findings. " + ("Consider the following rule. " * 60)
        + f"\n\n{PER_ITEM_MARKER}\nTEXT: {{text}}\nWHERE: {{where}}\n"
    )
    preamble, per_item = split_template(realistic)
    many = [
        {"id": f"i{n}", "text": f"text {n}" * 8, "where": f"ch{n}"}
        for n in range(10)
    ]

    batched = len(build_batch_prompt(preamble, per_item, many))
    separate = sum(len(realistic.format(**item)) for item in many)

    assert batched < separate * 0.5, (
        f"batched {batched} vs separate {separate}, saving too small to bother"
    )


def test_every_item_id_appears_so_answers_can_be_matched_back() -> None:
    preamble, per_item = split_template(TEMPLATE)

    prompt = build_batch_prompt(preamble, per_item, ITEMS)

    for item in ITEMS:
        assert item["id"] in prompt


def test_split_batch_response_returns_an_answer_per_item() -> None:
    response = (
        "=== ANSWER a1 ===\nCATEGORY: A\n"
        "=== ANSWER b2 ===\nCATEGORY: B\n"
    )

    answers = split_batch_response(response, ["a1", "b2"])

    assert answers["a1"].strip() == "CATEGORY: A"
    assert answers["b2"].strip() == "CATEGORY: B"


def test_a_missing_answer_is_absent_rather_than_guessed() -> None:
    """A short reply must not shift answers onto the wrong items.

    Returning two answers for three items and matching by position would
    silently attribute b2's answer to c3. Matching by id cannot do that.
    """
    response = "=== ANSWER a1 ===\nCATEGORY: A\n=== ANSWER c3 ===\nCATEGORY: C\n"

    answers = split_batch_response(response, ["a1", "b2", "c3"])

    assert set(answers) == {"a1", "c3"}
    assert "b2" not in answers


def test_an_unasked_id_in_the_response_is_ignored() -> None:
    """The model inventing an id must not create an output file for it."""
    response = "=== ANSWER a1 ===\nA\n=== ANSWER zz ===\nnonsense\n"

    answers = split_batch_response(response, ["a1", "b2"])

    assert set(answers) == {"a1"}


def test_answer_text_keeps_its_internal_formatting() -> None:
    response = "=== ANSWER a1 ===\nline one\n\n  indented\nline three\n"

    answers = split_batch_response(response, ["a1"])

    assert "line one" in answers["a1"]
    assert "  indented" in answers["a1"]
    assert "line three" in answers["a1"]
