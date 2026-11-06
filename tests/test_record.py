"""Tests for `enmaas record`, the ledger entry point for other tools.

The ledger only sees calls this CLI made. A tool that talks to the gateway
itself is invisible by construction, and one does: the bug-hunt engine's
provider pushed 1,067,793 input tokens through the gateway in a single run
and `enmaas usage` reported none of it. Usage was understated by roughly an
order of magnitude and nothing said so.

`record` makes the ledger a shared service rather than a private file. It
is a subcommand rather than an importable function on purpose: a caller in
any language can shell out once per run, and nothing has to take a Python
dependency on this package to be counted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enmaas_cli.cli import main


def _ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "enmaas" / "runs" / "usage.jsonl"


def _rows(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_record_appends_a_row_another_tool_can_be_counted_by(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path, monkeypatch)

    rc = main(
        [
            "record",
            "--model",
            "rits/zai-org/glm-5-3",
            "--command",
            "bug-hunt",
            "--input",
            "1067793",
            "--output",
            "49242",
            "--elapsed",
            "612.5",
        ]
    )

    assert rc == 0
    rows = _rows(ledger)
    assert len(rows) == 1
    assert rows[0]["model"] == "rits/zai-org/glm-5-3"
    assert rows[0]["command"] == "bug-hunt"
    assert rows[0]["input_tokens"] == 1067793
    assert rows[0]["output_tokens"] == 49242
    assert rows[0]["status"] == "ok"


def test_a_recorded_run_shows_up_in_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The point of the feature: the number stops being invisible."""
    _ledger(tmp_path, monkeypatch)
    main(
        [
            "record",
            "--model",
            "rits/zai-org/glm-5-3",
            "--command",
            "bug-hunt",
            "--input",
            "1000000",
            "--output",
            "50000",
        ]
    )
    capsys.readouterr()

    main(["usage"])

    out = capsys.readouterr().out
    assert "1,000,000" in out
    assert "bug-hunt" in out


def test_record_carries_cached_and_reasoning_when_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path, monkeypatch)

    main(
        [
            "record",
            "--model",
            "m",
            "--command",
            "c",
            "--input",
            "10",
            "--output",
            "2",
            "--cached",
            "7",
            "--reasoning",
            "3",
        ]
    )

    row = _rows(ledger)[0]
    assert row["cached_tokens"] == 7
    assert row["reasoning_tokens"] == 3


def test_a_failed_run_can_be_recorded_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger that only holds successes overstates how well things went."""
    ledger = _ledger(tmp_path, monkeypatch)

    main(
        [
            "record",
            "--model",
            "m",
            "--command",
            "c",
            "--input",
            "5",
            "--output",
            "0",
            "--status",
            "error",
            "--error-kind",
            "GatewayError",
        ]
    )

    row = _rows(ledger)[0]
    assert row["status"] == "error"
    assert row["error_kind"] == "GatewayError"


def test_record_rejects_a_negative_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage in the ledger is worse than a gap in it.

    Usage errors exit 1 here rather than returning, which is this CLI's
    established contract: exit 2 is reserved for partial failure.
    """
    ledger = _ledger(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "record",
                "--model",
                "m",
                "--command",
                "c",
                "--input",
                "-1",
                "--output",
                "2",
            ]
        )

    assert excinfo.value.code == 1
    assert not ledger.exists(), "a rejected row must not reach the ledger"


def test_zero_tokens_are_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call that produced nothing still happened and still counts."""
    ledger = _ledger(tmp_path, monkeypatch)

    assert (
        main(
            [
                "record",
                "--model",
                "m",
                "--command",
                "c",
                "--input",
                "0",
                "--output",
                "0",
                "--status",
                "error",
            ]
        )
        == 0
    )
    assert _rows(ledger)[0]["input_tokens"] == 0
