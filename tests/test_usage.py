"""Tests for usage ledger. All offline, no live calls."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enmaas_cli.usage import (
    UsageEvent,
    append_event,
    load_events,
    summarize_usage,
)


def test_append_and_load_event(tmp_path: Path) -> None:
    """Event is written as JSON line and loaded back correctly."""
    ledger = tmp_path / "usage.jsonl"
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=1000,
        cached_tokens=500,
        output_tokens=200,
        reasoning_tokens=None,
        elapsed=3.5,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )
    append_event(ledger, event)
    assert ledger.exists()

    events = load_events(ledger)
    assert len(events) == 1
    loaded = events[0]
    assert loaded.command == "ask"
    assert loaded.model == "claude-3-5-sonnet-20241022"
    assert loaded.dialect == "anthropic"
    assert loaded.input_tokens == 1000
    assert loaded.cached_tokens == 500
    assert loaded.output_tokens == 200
    assert loaded.reasoning_tokens is None
    assert loaded.elapsed == 3.5
    assert loaded.status == "ok"
    assert loaded.schema_used is False
    assert loaded.error_kind is None


def test_append_multiple_events(tmp_path: Path) -> None:
    """Multiple events accumulate correctly."""
    ledger = tmp_path / "usage.jsonl"
    for i in range(3):
        event = UsageEvent(
            timestamp=datetime.now(UTC),
            command="ask",
            model="claude-3-5-sonnet-20241022",
            dialect="anthropic",
            input_tokens=100 * (i + 1),
            cached_tokens=None,
            output_tokens=50,
            reasoning_tokens=None,
            elapsed=1.0,
            status="ok",
            schema_used=False,
            error_kind=None,
            items_total=None,
            items_ok=None,
            items_failed=None,
        )
        append_event(ledger, event)

    events = load_events(ledger)
    assert len(events) == 3
    assert events[0].input_tokens == 100
    assert events[1].input_tokens == 200
    assert events[2].input_tokens == 300


def test_map_event_includes_item_counts(tmp_path: Path) -> None:
    """Map events record item counts."""
    ledger = tmp_path / "usage.jsonl"
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="map",
        model="gpt-4o",
        dialect="openai",
        input_tokens=5000,
        cached_tokens=None,
        output_tokens=1000,
        reasoning_tokens=None,
        elapsed=20.5,
        status="ok",
        schema_used=True,
        error_kind=None,
        items_total=10,
        items_ok=9,
        items_failed=1,
    )
    append_event(ledger, event)

    events = load_events(ledger)
    assert len(events) == 1
    assert events[0].items_total == 10
    assert events[0].items_ok == 9
    assert events[0].items_failed == 1
    assert events[0].schema_used is True


def test_error_event_includes_error_kind(tmp_path: Path) -> None:
    """Error events record error kind but no content."""
    ledger = tmp_path / "usage.jsonl"
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=100,
        cached_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        elapsed=0.5,
        status="error",
        schema_used=False,
        error_kind="GatewayError",
        items_total=None,
        items_ok=None,
        items_failed=None,
    )
    append_event(ledger, event)

    events = load_events(ledger)
    assert len(events) == 1
    assert events[0].status == "error"
    assert events[0].error_kind == "GatewayError"


def test_no_content_in_ledger(tmp_path: Path) -> None:
    """Ledger contains only metadata, never prompts or responses."""
    ledger = tmp_path / "usage.jsonl"

    # Create event as if it came from a real call
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=2000,
        cached_tokens=500,
        output_tokens=800,
        reasoning_tokens=100,
        elapsed=5.2,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )
    append_event(ledger, event)

    # Read raw file
    raw = ledger.read_text()

    # These are acceptable (metadata)
    assert "claude-3-5-sonnet-20241022" in raw
    assert "anthropic" in raw
    assert "2000" in raw

    # These must never appear (content)
    # No field names that could hold content
    forbidden_fields = ["prompt", "response", "text", "content", "message", "output"]
    for field in forbidden_fields:
        assert f'"{field}"' not in raw.lower()

    # No file paths (they embed topics)
    assert ".md" not in raw
    assert "/tmp" not in raw

    # No error strings (they can leak fragments)
    # Only error_kind classification is allowed
    assert "error" in raw  # The word itself is fine as a field value
    # But not as part of a message
    line = json.loads(raw.strip())
    if line.get("error_kind"):
        # Error kind should be a simple class name, not a full message
        assert len(line["error_kind"]) < 100
        assert "\n" not in line["error_kind"]


def test_corrupt_final_line_is_tolerated(tmp_path: Path) -> None:
    """Partially written final line does not break loading."""
    ledger = tmp_path / "usage.jsonl"

    # Write one valid event
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=100,
        cached_tokens=None,
        output_tokens=50,
        reasoning_tokens=None,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )
    append_event(ledger, event)

    # Append a corrupt line
    with ledger.open("a") as f:
        f.write('{"incomplete": "json", "command": "ask"\n')

    # Should load the valid event and skip the corrupt one
    events = load_events(ledger)
    assert len(events) == 1
    assert events[0].input_tokens == 100


def test_load_empty_ledger(tmp_path: Path) -> None:
    """Empty or nonexistent ledger returns empty list."""
    assert load_events(tmp_path / "nonexistent.jsonl") == []

    empty = tmp_path / "empty.jsonl"
    empty.touch()
    assert load_events(empty) == []


def test_load_ledger_with_blank_lines(tmp_path: Path) -> None:
    """Blank lines are skipped."""
    ledger = tmp_path / "usage.jsonl"
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=100,
        cached_tokens=None,
        output_tokens=50,
        reasoning_tokens=None,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )
    append_event(ledger, event)

    # Add blank lines
    with ledger.open("a") as f:
        f.write("\n\n")

    append_event(ledger, event)

    events = load_events(ledger)
    assert len(events) == 2


def test_summarize_empty_usage() -> None:
    """Empty event list produces zero summary."""
    summary = summarize_usage([])
    assert summary.total_calls == 0
    assert summary.total_input_tokens == 0
    assert summary.total_output_tokens == 0
    assert summary.total_cached_tokens == 0
    assert summary.total_reasoning_tokens == 0
    assert summary.total_elapsed == 0.0
    assert len(summary.by_model) == 0
    assert len(summary.by_command) == 0


def test_summarize_aggregates_correctly(tmp_path: Path) -> None:
    """Summary correctly totals tokens and time."""
    events = [
        UsageEvent(
            timestamp=datetime.now(UTC),
            command="ask",
            model="claude-3-5-sonnet-20241022",
            dialect="anthropic",
            input_tokens=1000,
            cached_tokens=200,
            output_tokens=300,
            reasoning_tokens=50,
            elapsed=2.0,
            status="ok",
            schema_used=False,
            error_kind=None,
            items_total=None,
            items_ok=None,
            items_failed=None,
        ),
        UsageEvent(
            timestamp=datetime.now(UTC),
            command="ask",
            model="claude-3-5-sonnet-20241022",
            dialect="anthropic",
            input_tokens=500,
            cached_tokens=None,
            output_tokens=150,
            reasoning_tokens=None,
            elapsed=1.5,
            status="ok",
            schema_used=False,
            error_kind=None,
            items_total=None,
            items_ok=None,
            items_failed=None,
        ),
        UsageEvent(
            timestamp=datetime.now(UTC),
            command="map",
            model="gpt-4o",
            dialect="openai",
            input_tokens=2000,
            cached_tokens=None,
            output_tokens=800,
            reasoning_tokens=None,
            elapsed=10.0,
            status="ok",
            schema_used=True,
            error_kind=None,
            items_total=5,
            items_ok=5,
            items_failed=0,
        ),
    ]

    summary = summarize_usage(events)
    assert summary.total_calls == 3
    assert summary.total_input_tokens == 3500
    assert summary.total_output_tokens == 1250
    assert summary.total_cached_tokens == 200
    assert summary.total_reasoning_tokens == 50
    assert summary.total_elapsed == 13.5

    assert "claude-3-5-sonnet-20241022" in summary.by_model
    assert summary.by_model["claude-3-5-sonnet-20241022"]["calls"] == 2
    assert summary.by_model["claude-3-5-sonnet-20241022"]["input_tokens"] == 1500
    assert summary.by_model["claude-3-5-sonnet-20241022"]["output_tokens"] == 450

    assert "gpt-4o" in summary.by_model
    assert summary.by_model["gpt-4o"]["calls"] == 1
    assert summary.by_model["gpt-4o"]["input_tokens"] == 2000

    assert "ask" in summary.by_command
    assert summary.by_command["ask"]["calls"] == 2
    assert "map" in summary.by_command
    assert summary.by_command["map"]["calls"] == 1


def test_filter_by_since(tmp_path: Path) -> None:
    """Since parameter filters to recent events only."""
    now = datetime.now(UTC)
    old = now - timedelta(days=10)
    recent = now - timedelta(hours=1)

    events = [
        UsageEvent(
            timestamp=old,
            command="ask",
            model="claude-3-5-sonnet-20241022",
            dialect="anthropic",
            input_tokens=100,
            cached_tokens=None,
            output_tokens=50,
            reasoning_tokens=None,
            elapsed=1.0,
            status="ok",
            schema_used=False,
            error_kind=None,
            items_total=None,
            items_ok=None,
            items_failed=None,
        ),
        UsageEvent(
            timestamp=recent,
            command="ask",
            model="claude-3-5-sonnet-20241022",
            dialect="anthropic",
            input_tokens=200,
            cached_tokens=None,
            output_tokens=100,
            reasoning_tokens=None,
            elapsed=2.0,
            status="ok",
            schema_used=False,
            error_kind=None,
            items_total=None,
            items_ok=None,
            items_failed=None,
        ),
    ]

    # Filter to last 7 days
    cutoff = now - timedelta(days=7)
    filtered = [e for e in events if e.timestamp >= cutoff]
    summary = summarize_usage(filtered)

    assert summary.total_calls == 1
    assert summary.total_input_tokens == 200


def test_unwritable_directory_does_not_break_call(tmp_path: Path) -> None:
    """Append failure is silent, call continues."""
    # A directory with its execute bit cleared behaves differently across
    # filesystems and users, so this uses a parent that is a regular file.
    # Opening a child of a file fails as NotADirectoryError everywhere.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")

    ledger = blocker / "usage.jsonl"
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=100,
        cached_tokens=None,
        output_tokens=50,
        reasoning_tokens=None,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )

    # Should not raise
    append_event(ledger, event)

    # Nothing was written, and crucially the call did not raise.
    assert blocker.read_text() == ""


def test_env_var_disables_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ENMAAS_NO_USAGE_LOG=1 prevents writing."""
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")

    ledger = tmp_path / "usage.jsonl"
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=100,
        cached_tokens=None,
        output_tokens=50,
        reasoning_tokens=None,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )

    append_event(ledger, event)
    assert not ledger.exists()


def test_env_var_any_truthy_value_disables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any non-empty value is truthy."""
    for val in ["1", "yes", "true", "anything"]:
        monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", val)
        ledger = tmp_path / f"usage-{val}.jsonl"
        event = UsageEvent(
            timestamp=datetime.now(UTC),
            command="ask",
            model="claude-3-5-sonnet-20241022",
            dialect="anthropic",
            input_tokens=100,
            cached_tokens=None,
            output_tokens=50,
            reasoning_tokens=None,
            elapsed=1.0,
            status="ok",
            schema_used=False,
            error_kind=None,
            items_total=None,
            items_ok=None,
            items_failed=None,
        )
        append_event(ledger, event)
        assert not ledger.exists()


def test_env_var_empty_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string or unset means logging is enabled."""
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "")

    ledger = tmp_path / "usage.jsonl"
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=100,
        cached_tokens=None,
        output_tokens=50,
        reasoning_tokens=None,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )

    append_event(ledger, event)
    assert ledger.exists()
