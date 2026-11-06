"""Token budget accounting."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from enmaas_cli.budget import (
    TokenBudget,
    charge_from_rows,
    metered,
    rolling_spend,
)
from enmaas_cli.dialect import Usage, parse_response
from enmaas_cli.usage import UsageEvent


def test_metered_does_not_double_count_openai_reasoning() -> None:
    """completion_tokens ALREADY includes reasoning_tokens.

    Mirrors the fixture in tests/test_parse.py. The charge is
    (9935 - 9216) + 1430 = 2149. Adding the reasoning breakdown on top
    would give 3237 and overcharge every OpenAI call that reasoned.
    """
    data = {
        "choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
        "usage": {
            "prompt_tokens": 9935,
            "completion_tokens": 1430,
            "completion_tokens_details": {"reasoning_tokens": 1088},
            "prompt_tokens_details": {"cached_tokens": 9216},
        },
    }
    assert metered(parse_response(data, "openai").usage) == 2149


def test_metered_charges_anthropic_cache_writes_but_not_reads() -> None:
    """A cache write is work; a cache read is the saving.

    dialect.py sums base + read + write into input_tokens and puts only the
    read into cached_tokens, so 200 + 300 + 500 = 1000 inclusive, minus the
    300 read, plus 60 output.
    """
    data = {
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 200,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 500,
            "output_tokens": 60,
        },
    }
    assert metered(parse_response(data, "anthropic").usage) == 760


def test_metered_treats_none_as_zero() -> None:
    """'Not reported' must not crash the accumulator."""
    assert metered(Usage()) == 0
    assert metered(Usage(output_tokens=5)) == 5


def test_metered_never_credits_the_budget() -> None:
    """A malformed response claiming more cached than input must not pay back."""
    assert metered(Usage(input_tokens=100, cached_tokens=5000, output_tokens=40)) == 40


def test_budget_trips_on_reaching_not_exceeding() -> None:
    """Requiring excess would always overshoot by one request."""
    budget = TokenBudget(ceiling=1000)
    budget.record(999)
    assert not budget.should_abort()
    budget.record(1)
    assert budget.should_abort()


def test_budget_accumulates() -> None:
    budget = TokenBudget(ceiling=100)
    for _ in range(9):
        budget.record(11)
    assert budget.spent == 99
    assert not budget.should_abort()


def test_budget_can_be_seeded_with_prior_spend() -> None:
    """--resume continues a job's budget rather than granting a fresh one."""
    budget = TokenBudget(ceiling=1000, spent=950)
    budget.record(50)
    assert budget.should_abort()


def test_budget_seeded_over_the_ceiling_trips_immediately() -> None:
    """Resuming a run that already stopped on budget must send nothing."""
    assert TokenBudget(ceiling=1000, spent=1200).should_abort()


def test_budget_rejects_a_nonpositive_ceiling() -> None:
    """Zero is a typo, not a request to block everything."""
    with pytest.raises(ValueError, match="positive"):
        TokenBudget(ceiling=0)


def test_metered_coerces_float_counts_to_int() -> None:
    """A gateway reporting floats must not produce a float charge.

    json writes 30.0 verbatim, and charge_from_rows rejects non-ints, so an
    uncoerced float turns real spend into a silent zero on resume.
    """
    charge = metered(Usage(input_tokens=10.0, output_tokens=20.0))  # type: ignore[arg-type]
    assert charge == 30
    assert isinstance(charge, int)


def test_metered_falls_back_to_reasoning_when_output_is_missing() -> None:
    """completion_tokens absent but its reasoning breakdown present.

    Charging zero would discard work the response says happened. The
    breakdown is a lower bound, which beats nothing.
    """
    data = {
        "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens_details": {"reasoning_tokens": 20},
        },
    }
    assert metered(parse_response(data, "openai").usage) == 30


def test_metered_ignores_a_negative_count() -> None:
    assert metered(Usage(input_tokens=-500, output_tokens=40)) == 40


def test_budget_ignores_a_negative_charge() -> None:
    """A budget refillable by a malformed usage report is not a budget."""
    budget = TokenBudget(ceiling=100, spent=90)
    budget.record(-1000)
    assert budget.spent == 90


def test_charge_from_rows_sums_only_request_rows() -> None:
    assert (
        charge_from_rows(
            [
                {"id": "a", "status": "ok", "tokens": {"in": 999, "out": 999}},
                {"type": "request", "charge": 200, "items": 1},
                {"type": "request", "charge": 50, "items": 5},
            ]
        )
        == 250
    )


def test_charge_from_rows_rejects_charges_that_would_hand_budget_back() -> None:
    assert (
        charge_from_rows(
            [
                {"type": "request", "charge": -100, "items": 1},
                {"type": "request", "charge": 30.0, "items": 1},
                {"type": "request", "charge": True, "items": 1},
                {"type": "request", "charge": "200", "items": 1},
                {"type": "request", "charge": 10, "items": 1},
            ]
        )
        == 10
    )


def _event(
    now: datetime,
    *,
    days_ago: int = 0,
    inp: int = 0,
    cached: int = 0,
    out: int = 0,
    status: str = "ok",
) -> UsageEvent:
    return UsageEvent(
        timestamp=now - timedelta(days=days_ago),
        command="ask",
        model="m",
        dialect="openai",
        input_tokens=inp,
        cached_tokens=cached,
        output_tokens=out,
        reasoning_tokens=0,
        elapsed=1.0,
        status=status,
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )


def test_rolling_spend_counts_only_events_inside_the_window() -> None:
    now = datetime.now(UTC)
    events = [
        _event(now, days_ago=1, out=100),
        _event(now, days_ago=29, out=200),
        _event(now, days_ago=31, out=9999),
    ]
    assert rolling_spend(events, window_days=30, now=now) == 300


def test_rolling_spend_excludes_cached_input_like_metered_does() -> None:
    now = datetime.now(UTC)
    event = _event(now, inp=1000, cached=900, out=100)
    assert rolling_spend([event], window_days=30, now=now) == 200


def test_rolling_spend_counts_tokens_from_calls_that_failed() -> None:
    """A call that burned tokens and returned nothing was still charged.

    Filtering on status would be blind to the most expensive failure the
    README documents.
    """
    now = datetime.now(UTC)
    partial = _event(now, inp=500, out=32768, status="error")
    assert rolling_spend([partial], window_days=30, now=now) == 33268


def test_rolling_spend_matches_metered_on_the_same_numbers() -> None:
    """The two must not drift: one reads a Usage, the other a UsageEvent."""
    now = datetime.now(UTC)
    event = _event(now, inp=7000, cached=4321, out=765)
    usage = Usage(input_tokens=7000, cached_tokens=4321, output_tokens=765)
    assert rolling_spend([event], window_days=30, now=now) == metered(usage)


def test_rolling_spend_on_no_events_is_zero() -> None:
    assert rolling_spend([], window_days=30) == 0


def test_a_recorded_charge_beats_rederiving_it_from_aggregates() -> None:
    """Clamping after aggregation is not the sum of per-request clamps.

    Two requests of (10 in, 100 cached, 1 out) and (100 in, 0 cached,
    1 out) cost 102. Their combined event derives 12, a 90% undercount.
    """
    now = datetime.now(UTC)
    per_request = metered(Usage(input_tokens=10, cached_tokens=100, output_tokens=1))
    per_request += metered(Usage(input_tokens=100, cached_tokens=0, output_tokens=1))
    assert per_request == 102

    event = _event(now, inp=110, cached=100, out=2)
    derived = rolling_spend([event], window_days=30, now=now)
    assert derived == 12, "the old derivation, kept only for legacy rows"

    recorded = replace(event, charge=per_request)
    assert rolling_spend([recorded], window_days=30, now=now) == 102


def test_a_recorded_charge_captures_failures_the_counters_miss() -> None:
    """A map run's charged failures are invisible to input/output counters."""
    now = datetime.now(UTC)
    # Two items succeeded for 50, one failed after burning 300.
    event = _event(now, inp=100, cached=0, out=0)
    assert rolling_spend([replace(event, charge=350)], window_days=30, now=now) == 350


def test_legacy_rows_fall_back_to_the_reasoning_breakdown() -> None:
    """Rows predating `charge` must not read as free when output is absent."""
    now = datetime.now(UTC)
    legacy = UsageEvent(
        timestamp=now,
        command="ask",
        model="m",
        dialect="openai",
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        reasoning_tokens=100,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )
    assert legacy.charge is None
    assert rolling_spend([legacy], window_days=30, now=now) == 100
