"""What a run has spent, and when to stop.

Counted in tokens rather than dollars. The gateway reports token counts on
every call, so a token ceiling needs no price table and never meets the
unpriced-model case that makes `pricing.cost_for` return None.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from enmaas_cli.dialect import Usage
from enmaas_cli.usage import UsageEvent


def metered(usage: Usage) -> int:
    """Tokens charged against a ceiling.

    Named "metered" rather than "billable" because cached input *is* billed,
    at a discount. This is a budget policy, not an invoice.

    No reasoning term, deliberately. `output_tokens` is OpenAI's
    `completion_tokens`, which already includes reasoning; `reasoning_tokens`
    is a breakdown of it (dialect.py:228). On the Anthropic dialect it is
    always None (dialect.py:269) because thinking is folded into
    `output_tokens`. Adding it would double-charge.

    Cached input is excluded: it is the saving that batching exists to
    produce, and a ceiling blind to it would make the tool's main
    optimization invisible. Cache *writes* stay charged, because writing a
    cache entry is real work, and they need no special handling: dialect.py
    puts them in `input_tokens` but not in `cached_tokens`.

    Dialect-neutral. dialect.py already normalises OpenAI (where
    `prompt_tokens` includes the cached portion) and Anthropic (where
    `input_tokens` does not) into one shape where `input_tokens` is the
    inclusive total.

    Clamped per call, never on aggregates: max(0, sum_in - sum_cached) is
    not sum of max(0, in_i - cached_i), so every consumer must charge the
    stored per-request figure rather than recompute from totals.
    """
    uncached_input = max(0, _count(usage.input_tokens) - _count(usage.cached_tokens))
    # Prefer the complete output count. When a gateway omits it but still
    # reports the reasoning breakdown, charge that as a lower bound rather
    # than charging zero for work that demonstrably happened.
    output = _count(usage.output_tokens) or _count(usage.reasoning_tokens)
    return uncached_input + output


def _count(value: object) -> int:
    """One usage field as a nonnegative int.

    Coerces rather than trusting. A gateway reporting `10.0` would otherwise
    produce a float charge that json writes as `10.0` and
    `charge_from_rows` then rejects, turning real spend into a silent zero.
    """
    # bool first: it subclasses int, so True would otherwise charge 1.
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def charge_from_rows(rows: Iterable[dict[str, Any]]) -> int:
    """Sum the charge rows run_map wrote to results.jsonl.

    Exact, because it reads what each request actually cost rather than
    recomputing from aggregates. Recomputing would clamp in the wrong
    place: max(0, sum_in - sum_cached) is not sum of the per-call clamps.

    Hostile to malformed rows. A negative or non-integer charge is skipped
    rather than trusted, since a negative one would hand budget back.
    """
    total = 0
    for row in rows:
        if row.get("type") != "request":
            continue
        charge = row.get("charge")
        if isinstance(charge, int) and not isinstance(charge, bool) and charge >= 0:
            total += charge
    return total


class TokenBudget:
    """A ceiling and a running total. A sibling to CircuitBreaker.

    The breaker answers "is this run broken". This answers "has this run
    spent what it was given". Both are consulted after each completed
    request, inside the same lock, and either can stop the run.

    Holds no I/O, so it is testable without a gateway.

    Args:
        ceiling: Metered tokens this run may spend.
        spent: Tokens already charged to this job, used when resuming.
    """

    def __init__(self, ceiling: int, *, spent: int = 0) -> None:
        if ceiling <= 0:
            raise ValueError("token budget ceiling must be positive")
        self.ceiling = ceiling
        self.spent = spent

    def record(self, tokens: int) -> None:
        """Add one completed request's charge.

        Negative charges are ignored rather than credited. A budget that
        could be refilled by a malformed usage report is not a budget, and
        charge_from_rows already rejects them on the way back in.
        """
        self.spent += max(0, tokens)

    def should_abort(self) -> bool:
        """True once the run has spent its ceiling.

        Reaching the ceiling is enough. A budget that required exceeding it
        would always overshoot by at least one request.
        """
        return self.spent >= self.ceiling


def rolling_spend(
    events: Iterable[UsageEvent],
    *,
    window_days: int,
    now: datetime | None = None,
) -> int:
    """Metered tokens recorded inside the window.

    `now` is injectable so tests do not depend on the wall clock.

    No status filter, deliberately. A call that burned its whole budget
    on reasoning and returned no text was still charged, and that is the
    most expensive failure the README documents. Counting only successes
    would be blind to exactly the case a ceiling exists to catch.

    Mirrors metered() rather than calling it, because a UsageEvent is not
    a Usage. The two must agree; test_rolling_spend_matches_metered pins
    that they do.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=window_days)
    return sum(
        event_charge(event) for event in events if event.timestamp >= cutoff
    )


def event_charge(event: UsageEvent) -> int:
    """What one ledger event cost.

    Prefers the recorded charge, which was summed per request as each one
    completed. Deriving it from the aggregate counts instead is wrong in
    three ways that only show up together:

    - Clamping after aggregation is not the sum of per-request clamps.
      Two requests of (10 in, 100 cached, 1 out) and (100 in, 0 cached,
      1 out) cost 102, but their combined event derives 12.
    - The aggregate counters record successful requests only, so a map
      run's failures, which were still charged, vanish between runs.
    - `output_tokens` can be absent where a reasoning breakdown is not.

    The fallback exists for rows written before `charge` was recorded. It
    mirrors metered(), reasoning fallback included, and is the best that
    can be done with what those rows kept.
    """
    if event.charge is not None:
        return _count(event.charge)
    uncached = max(0, _count(event.input_tokens) - _count(event.cached_tokens))
    return uncached + (_count(event.output_tokens) or _count(event.reasoning_tokens))
