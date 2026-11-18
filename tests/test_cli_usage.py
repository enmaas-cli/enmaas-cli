"""Tests for the usage subcommand."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enmaas_cli.cli import main
from enmaas_cli.usage import UsageEvent, append_event


def test_usage_command_empty_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Usage command with no events shows zero summary."""
    monkeypatch.setattr("enmaas_cli.cli.state_root", lambda: tmp_path)

    exit_code = main(["usage"])
    assert exit_code == 0


def test_usage_command_with_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Usage command displays summary."""
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr("enmaas_cli.cli.state_root", lambda: tmp_path)

    # Create some events
    events = [
        UsageEvent(
            timestamp=datetime.now(UTC),
            command="ask",
            model="claude-3-5-sonnet-20241022",
            dialect="anthropic",
            input_tokens=1000,
            cached_tokens=200,
            output_tokens=300,
            reasoning_tokens=None,
            elapsed=2.5,
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
            input_tokens=5000,
            cached_tokens=None,
            output_tokens=1000,
            reasoning_tokens=None,
            elapsed=10.0,
            status="ok",
            schema_used=True,
            error_kind=None,
            items_total=10,
            items_ok=9,
            items_failed=1,
        ),
    ]

    for event in events:
        append_event(ledger, event)

    exit_code = main(["usage"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Total calls:" in captured.out
    assert "2" in captured.out
    assert "claude-3-5-sonnet-20241022" in captured.out
    assert "gpt-4o" in captured.out


def test_usage_command_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Usage command --json produces valid JSON."""
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr("enmaas_cli.cli.state_root", lambda: tmp_path)

    event = UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="claude-3-5-sonnet-20241022",
        dialect="anthropic",
        input_tokens=1000,
        cached_tokens=None,
        output_tokens=300,
        reasoning_tokens=None,
        elapsed=2.5,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
    )
    append_event(ledger, event)

    exit_code = main(["usage", "--json"])
    assert exit_code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["total_calls"] == 1
    assert data["total_input_tokens"] == 1000
    assert data["total_output_tokens"] == 300


def test_usage_command_since_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Usage command --since filters events."""
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr("enmaas_cli.cli.state_root", lambda: tmp_path)

    now = datetime.now(UTC)
    old = now - timedelta(days=10)
    recent = now - timedelta(hours=1)

    events = [
        UsageEvent(
            timestamp=old,
            command="ask",
            model="claude-3-5-sonnet-20241022",
            dialect="anthropic",
            input_tokens=1000,
            cached_tokens=None,
            output_tokens=300,
            reasoning_tokens=None,
            elapsed=2.5,
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
    ]

    for event in events:
        append_event(ledger, event)

    exit_code = main(["usage", "--since", "7"])
    assert exit_code == 0

    captured = capsys.readouterr()
    # Should only show the recent event
    assert "last 7 days" in captured.out
    assert "Total calls:" in captured.out
    # The summary should only include the recent event
    data_lines = [line for line in captured.out.split("\n") if "Total calls:" in line]
    assert len(data_lines) == 1
