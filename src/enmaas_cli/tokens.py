"""Token counts reported by the gateway, normalised across dialects."""

from __future__ import annotations

from dataclasses import dataclass

# The largest single count taken from a reply or from a command-line
# argument. A larger one from a reply reads as not reported; `record`
# refuses one.
MAX_TOKEN_COUNT = 10**12
# The largest count or charge in an index row, a ledger row or an open-run
# sum. Writers clamp to it and readers accept up to it, so a row enmaas
# writes is always one it reads back. Sums of single counts reach far above
# MAX_TOKEN_COUNT, and this stays far below the 4,300 digits Python can
# print.
MAX_RECORDED = 10**18


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
