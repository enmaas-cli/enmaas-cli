from enmaas_cli.runner import CircuitBreaker


def test_does_not_trip_before_the_window_is_full() -> None:
    """A 5-item run must never abort on rate; the window is never full."""
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(5):
        breaker.record(ok=False)
    assert breaker.should_abort() is False


def test_trips_when_majority_of_full_window_failed() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(5):
        breaker.record(ok=True)
    for _ in range(15):
        breaker.record(ok=False)
    assert breaker.should_abort() is True


def test_does_not_trip_at_exactly_the_threshold() -> None:
    """Strictly greater than, so a 50/50 run keeps going.

    Outcomes alternate rather than arriving in two blocks, so this exercises
    the rate rule without also tripping the consecutive-failure rule.
    """
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(10):
        breaker.record(ok=True)
        breaker.record(ok=False)
    assert breaker.should_abort() is False


def test_six_consecutive_failures_trip_even_on_a_short_run() -> None:
    """Total breakage should not have to wait for a full 20-item window."""
    breaker = CircuitBreaker(window=20, threshold=0.5, consecutive=6)
    for _ in range(6):
        breaker.record(ok=False)
    assert breaker.should_abort() is True


def test_five_consecutive_failures_do_not_trip() -> None:
    """An unlucky burst of rate limiting is not the same as a dead gateway."""
    breaker = CircuitBreaker(window=20, threshold=0.5, consecutive=6)
    for _ in range(5):
        breaker.record(ok=False)
    assert breaker.should_abort() is False


def test_one_success_resets_the_consecutive_run() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5, consecutive=6)
    for _ in range(5):
        breaker.record(ok=False)
    breaker.record(ok=True)
    for _ in range(5):
        breaker.record(ok=False)
    assert breaker.should_abort() is False


def test_isolated_failures_do_not_trip_a_healthy_run() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for i in range(100):
        breaker.record(ok=(i % 10 != 0))
    assert breaker.should_abort() is False


def test_window_slides_so_recovery_clears_the_breaker() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(20):
        breaker.record(ok=False)
    assert breaker.should_abort() is True
    for _ in range(20):
        breaker.record(ok=True)
    assert breaker.should_abort() is False


def test_all_failures_trips() -> None:
    breaker = CircuitBreaker(window=20, threshold=0.5)
    for _ in range(20):
        breaker.record(ok=False)
    assert breaker.should_abort() is True


def test_outcome_reports_why_it_aborted() -> None:
    """'aborted' alone sends users looking for failures that did not happen."""
    from pathlib import Path

    from enmaas_cli.runner import MapOutcome

    outcome = MapOutcome(
        ok=0,
        failed=6,
        skipped=0,
        attempted=6,
        total=20,
        abort_reason="breaker",
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        elapsed=1.0,
        outdir=Path("out"),
        failures=[],
    )
    assert outcome.abort_reason == "breaker"
    assert outcome.exit_code == 3


def test_outcome_without_an_abort_reason_is_not_aborted() -> None:
    from pathlib import Path

    from enmaas_cli.runner import MapOutcome

    outcome = MapOutcome(
        ok=20,
        failed=0,
        skipped=0,
        attempted=20,
        total=20,
        abort_reason=None,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        elapsed=1.0,
        outdir=Path("out"),
        failures=[],
    )
    assert outcome.exit_code == 0
