"""Token counts reported by the gateway, normalised across dialects."""

from __future__ import annotations

from dataclasses import dataclass


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
