"""Tests for costing a run.

The gateway publishes no prices: its /v1/models catalog carries only id,
object, created and owned_by. So the tool cannot derive spend and must not
invent it. What it can do is price what it has been told, say plainly when
it has not been told, and separately report what the same tokens would have
cost on a reference model, which is the number that makes delegation worth
measuring at all.
"""

from __future__ import annotations

from enmaas_cli.pricing import (
    OPUS_REFERENCE,
    ModelPrice,
    cost_for,
    opus_equivalent,
    price_for,
)


def test_hosted_rits_models_are_known_free() -> None:
    """The one price the tool can assert without being told."""
    price = price_for("rits/zai-org/glm-5-3", {})

    assert price is not None
    assert price.input_per_mtok == 0.0
    assert price.output_per_mtok == 0.0


def test_an_unconfigured_model_has_no_price_rather_than_zero() -> None:
    """Unknown must not read as free. Zero would understate every report."""
    assert price_for("gpt-6-sol", {}) is None


def test_a_configured_price_is_used() -> None:
    table = {"gpt-6-sol": ModelPrice(input_per_mtok=1.25, output_per_mtok=10.0)}

    price = price_for("gpt-6-sol", table)

    assert price == table["gpt-6-sol"]


def test_a_configured_prefix_covers_its_models() -> None:
    """Prices are usually per family, not per exact id."""
    table = {"gemini-": ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0)}

    price = price_for("gemini-3.1-pro-preview", table)

    assert price is not None
    assert price.input_per_mtok == 2.0


def test_an_exact_id_beats_a_prefix() -> None:
    table = {
        "gemini-": ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0),
        "gemini-3.1-pro-preview": ModelPrice(
            input_per_mtok=5.0, output_per_mtok=20.0
        ),
    }

    price = price_for("gemini-3.1-pro-preview", table)

    assert price is not None
    assert price.input_per_mtok == 5.0


def test_cost_for_a_free_model_is_zero_not_unknown() -> None:
    assert cost_for("rits/zai-org/glm-5-3", 1_000_000, 1_000_000, {}) == 0.0


def test_cost_for_an_unpriced_model_is_none() -> None:
    assert cost_for("gpt-6-sol", 1_000_000, 1_000_000, {}) is None


def test_cost_uses_both_input_and_output_rates() -> None:
    table = {"m": ModelPrice(input_per_mtok=2.0, output_per_mtok=10.0)}

    # 2,000,000 input at $2 is $4; 500,000 output at $10 is $5.
    assert cost_for("m", 2_000_000, 500_000, table) == 9.0


def test_opus_equivalent_prices_tokens_at_the_reference_rate() -> None:
    """The comparison figure: what this work would have cost on Opus."""
    expected = (
        1_000_000 / 1e6 * OPUS_REFERENCE.input_per_mtok
        + 1_000_000 / 1e6 * OPUS_REFERENCE.output_per_mtok
    )

    assert opus_equivalent(1_000_000, 1_000_000) == expected


def test_the_reference_rate_is_stated_not_hidden() -> None:
    """A dollar figure nobody can reproduce is worse than none."""
    assert OPUS_REFERENCE.input_per_mtok > 0
    assert OPUS_REFERENCE.output_per_mtok > OPUS_REFERENCE.input_per_mtok
    assert OPUS_REFERENCE.label


def test_the_summary_reports_cost_and_the_reference_comparison() -> None:
    """usage has to answer "what did this cost" and "what did it save".

    Before this, the ledger held tokens only and every dollar figure had to
    be worked out by hand in a shell, which is not reproducible and was in
    fact wrong at least once.
    """
    from datetime import UTC, datetime

    from enmaas_cli.usage import UsageEvent, format_summary, summarize_usage

    events = [
        UsageEvent(
            timestamp=datetime.now(UTC), command="ask",
            model="rits/zai-org/glm-5-3", dialect="openai",
            input_tokens=1_000_000, cached_tokens=0, output_tokens=100_000,
            reasoning_tokens=0, elapsed=1.0, status="ok", schema_used=False,
            error_kind=None, items_total=None, items_ok=None,
            items_failed=None,
        ),
    ]

    text = format_summary(summarize_usage(events), since_days=None)

    assert "$0.00" in text, "a free model should report zero spend"
    assert "7.50" in text, (
        "1M input and 100k output at the Opus reference is $7.50"
    )
    assert "Opus" in text


def test_the_summary_names_models_it_cannot_price() -> None:
    """Silence would read as free and understate the total."""
    from datetime import UTC, datetime

    from enmaas_cli.usage import UsageEvent, format_summary, summarize_usage

    events = [
        UsageEvent(
            timestamp=datetime.now(UTC), command="ask", model="gpt-6-sol",
            dialect="openai", input_tokens=1000, cached_tokens=0,
            output_tokens=100, reasoning_tokens=0, elapsed=1.0, status="ok",
            schema_used=False, error_kind=None, items_total=None,
            items_ok=None, items_failed=None,
        ),
    ]

    text = format_summary(summarize_usage(events), since_days=None)

    assert "unpriced" in text.lower()
    assert "gpt-6-sol" in text
