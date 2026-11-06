"""Tests for usage ledger. All offline, no live calls."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enmaas_cli.usage import (
    UsageEvent,
    append_event,
    load_events,
    load_events_strict,
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


def _ok_row(**overrides: object) -> dict[str, object]:
    """A valid serialised ledger row, before any damage is applied."""
    row: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "command": "ask",
        "model": "m",
        "dialect": "openai",
        "input_tokens": 100,
        "cached_tokens": 0,
        "output_tokens": 10,
        "reasoning_tokens": 0,
        "elapsed": 1.0,
        "status": "ok",
        "schema_used": False,
        "error_kind": None,
        "items_total": None,
        "items_ok": None,
        "items_failed": None,
    }
    row.update(overrides)
    return row


def test_a_null_row_does_not_crash_either_reader(tmp_path: Path) -> None:
    """Pre-existing bug: data.pop() on None raises AttributeError, which is
    NOT in load_events' suppressed set, so `enmaas usage` crashed on it."""
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("null\n", encoding="utf-8")

    assert load_events(ledger) == []
    events, problems = load_events_strict(ledger)
    assert events == []
    assert len(problems) == 1


def test_strict_reader_rejects_negative_counts(tmp_path: Path) -> None:
    """A negative count would subtract from recorded spend."""
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(json.dumps(_ok_row(output_tokens=-100000)) + "\n", encoding="utf-8")

    events, problems = load_events_strict(ledger)
    assert events == []
    assert any("nonnegative" in p for p in problems)


def test_strict_reader_rejects_string_counts(tmp_path: Path) -> None:
    """ "100" passes the dataclass and breaks arithmetic downstream."""
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(json.dumps(_ok_row(input_tokens="100")) + "\n", encoding="utf-8")

    events, problems = load_events_strict(ledger)
    assert events == []
    assert problems


def test_strict_reader_rejects_naive_timestamps(tmp_path: Path) -> None:
    """A naive datetime raises when compared against an aware window cutoff."""
    ledger = tmp_path / "usage.jsonl"
    naive = datetime.now().replace(tzinfo=None).isoformat()  # noqa: DTZ005
    ledger.write_text(json.dumps(_ok_row(timestamp=naive)) + "\n", encoding="utf-8")

    events, problems = load_events_strict(ledger)
    assert events == []
    assert any("naive" in p for p in problems)


def test_strict_reader_reports_invalid_utf8(tmp_path: Path) -> None:
    """UnicodeDecodeError is not an OSError and would escape the handler."""
    ledger = tmp_path / "usage.jsonl"
    # A complete, valid row first. With an incomplete one the test would
    # pass on a missing-fields error even if decoding were handled, so it
    # would not be testing decoding at all.
    ledger.write_bytes(json.dumps(_ok_row()).encode() + b"\n\xff\xfe\n")

    events, problems = load_events_strict(ledger)
    assert events == []
    assert len(problems) == 1
    assert "could not be read" in problems[0]


def test_strict_reader_agrees_with_the_forgiving_one_when_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    ledger = tmp_path / "usage.jsonl"
    for out in (10, 20):
        append_event(
            ledger,
            UsageEvent(
                timestamp=datetime.now(UTC),
                command="ask",
                model="m",
                dialect="openai",
                input_tokens=100,
                cached_tokens=0,
                output_tokens=out,
                reasoning_tokens=0,
                elapsed=1.0,
                status="ok",
                schema_used=False,
                error_kind=None,
                items_total=None,
                items_ok=None,
                items_failed=None,
            ),
        )
    events, problems = load_events_strict(ledger)
    assert problems == []
    assert len(events) == len(load_events(ledger)) == 2


def test_strict_reader_on_a_missing_ledger_is_clean(tmp_path: Path) -> None:
    """A fresh install has no ledger and has spent nothing."""
    events, problems = load_events_strict(tmp_path / "nope.jsonl")
    assert events == []
    assert problems == []


def test_append_event_reports_a_failure_at_close_not_just_at_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A buffered line only reaches disk on close.

    Combining contextlib.suppress and open in one `with` caught the
    close error and then carried on to report success, so dropped spend
    looked recorded and the budget warning never fired.
    """
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    class FailsOnClose:
        def __enter__(self) -> "FailsOnClose":
            return self

        def write(self, text: str) -> int:
            return len(text)

        def __exit__(self, *exc: object) -> None:
            raise OSError("disk full on close")

    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="m",
        dialect="openai",
        input_tokens=1,
        cached_tokens=0,
        output_tokens=1,
        reasoning_tokens=0,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )
    monkeypatch.setattr(Path, "open", lambda *a, **k: FailsOnClose())

    assert append_event(tmp_path / "usage.jsonl", event) is False


def test_append_event_coerces_floats_so_its_own_reader_accepts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway reporting 10.0 must not make the next budgeted run
    refuse because this tool's own ledger looks corrupt."""
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    ledger = tmp_path / "usage.jsonl"
    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="m",
        dialect="openai",
        input_tokens=10.0,  # type: ignore[arg-type]
        cached_tokens=0,
        output_tokens=20.0,  # type: ignore[arg-type]
        reasoning_tokens=0,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
        charge=30.0,  # type: ignore[arg-type]
    )
    assert append_event(ledger, event) is True

    events, problems = load_events_strict(ledger)
    assert problems == []
    assert events[0].charge == 30


def test_a_ledger_mixing_old_and_new_rows_totals_correctly(tmp_path: Path) -> None:
    """Rows written before `charge` existed must still count."""
    from enmaas_cli.budget import rolling_spend

    now = datetime.now(UTC)
    ledger = tmp_path / "usage.jsonl"
    old_row = _ok_row(input_tokens=10, cached_tokens=0, output_tokens=0, reasoning_tokens=3)
    new_row = _ok_row(
        input_tokens=10, cached_tokens=0, output_tokens=0, reasoning_tokens=3, charge=100
    )
    ledger.write_text(json.dumps(old_row) + "\n" + json.dumps(new_row) + "\n", encoding="utf-8")

    events, problems = load_events_strict(ledger)
    assert problems == []
    # Old row derives 10 + reasoning fallback 3 = 13; new row uses 100.
    assert rolling_spend(events, window_days=30, now=now) == 113
