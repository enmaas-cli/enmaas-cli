"""A ledger row that cannot be written is always reported.

A link at the ledger path is refused, so a user who made the ledger a link
in an older version gets no rows. Without a budget that went unsaid. Now
any command that tries to write a row and fails prints one line on stderr:
`warning: usage was not recorded: <reason>`. The exit code and the --json
output do not change. With ENMAAS_NO_USAGE_LOG no write is tried, so
nothing is printed.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import main
from enmaas_cli.usage import UsageEvent, write_event

MODEL = "rits/zai-org/glm-5-3"
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "the answer"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}
UNTRACKED = (
    "warning: a token budget is configured but this run's usage was "
    "not recorded, so it will not count against the ceiling"
)


def _gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPLY)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    return seen


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "xdg" / "enmaas" / "runs"
    state.mkdir(parents=True)
    return state


def _linked_ledger(tmp_path: Path) -> tuple[Path, Path]:
    """A ledger path that is a link to a file elsewhere, as an older version allowed."""
    target = tmp_path / "elsewhere" / "usage.jsonl"
    target.parent.mkdir()
    target.write_text("keep me\n", encoding="utf-8")
    ledger = _state(tmp_path) / "usage.jsonl"
    ledger.symlink_to(target)
    return ledger, target


NO_CONFIG = ["--config", "/nonexistent/enmaas-config.toml"]


def _ask(tmp_path: Path) -> tuple[list[str], Path]:
    output = tmp_path / "answer.md"
    argv = ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"]
    return [*argv, "-o", str(output)], output


def _map(tmp_path: Path) -> tuple[list[str], Path]:
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n', encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    outdir = tmp_path / "out"
    argv = ["map", "-m", MODEL, "--dialect", "openai", "--retries", "0"]
    argv += ["--items", str(items), "--template", str(template), "--outdir", str(outdir)]
    return argv, outdir / "results.jsonl"


def _record(tmp_path: Path) -> tuple[list[str], Path | None]:
    argv = ["record", "--model", "m", "--input", "5", "--output", "2", "--command", "other"]
    return argv, None


COMMANDS = {"ask": _ask, "map": _map, "record": _record}


def _not_recorded(reason: str) -> str:
    return f"warning: usage was not recorded: {reason}"


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_linked_ledger_without_a_budget_is_reported_and_left_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    ledger, target = _linked_ledger(tmp_path)
    argv, answer = COMMANDS[command](tmp_path)
    assert main([*NO_CONFIG, *argv]) == 0
    err = capsys.readouterr().err
    assert err.splitlines() == [_not_recorded(f"{ledger} is not a regular file")]
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert ledger.is_symlink()
    if answer is not None:
        assert len(seen) == 1
        assert answer.exists()


@pytest.mark.parametrize("command", ["ask", "map"])
def test_the_json_output_is_unchanged_and_the_warning_goes_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    _gateway(tmp_path, monkeypatch)
    ledger, _ = _linked_ledger(tmp_path)
    argv, _ = COMMANDS[command](tmp_path)
    assert main([*NO_CONFIG, *argv, "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "ok"
    assert "not recorded" not in captured.out
    assert captured.err.splitlines() == [_not_recorded(f"{ledger} is not a regular file")]


def test_a_path_that_is_not_a_regular_file_is_printed_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch)
    xdg = tmp_path / "x\x1b]0;T\x07"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    state = xdg / "enmaas" / "runs"
    state.mkdir(parents=True)
    (state / "usage.jsonl").symlink_to(tmp_path / "nowhere")
    argv, _ = _record(tmp_path)
    assert main([*NO_CONFIG, *argv]) == 0
    err = capsys.readouterr().err
    shown = f"{tmp_path}/x?]0;T?/enmaas/runs/usage.jsonl"
    assert err.splitlines() == [_not_recorded(f"{shown} is not a regular file")]


def test_a_fifo_with_no_reader_names_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch)
    ledger = _state(tmp_path) / "usage.jsonl"
    os.mkfifo(ledger)
    argv, _ = _record(tmp_path)
    assert main([*NO_CONFIG, *argv]) == 0
    err = capsys.readouterr().err
    assert err.splitlines() == [_not_recorded(f"{ledger} is not a regular file")]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_another_failure_gives_the_error_class_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    ledger = _state(tmp_path) / "usage.jsonl"
    ledger.write_text("", encoding="utf-8")
    ledger.chmod(0o400)
    argv, answer = _ask(tmp_path)
    assert main([*NO_CONFIG, *argv]) == 0
    assert capsys.readouterr().err.splitlines() == [_not_recorded("PermissionError")]
    assert len(seen) == 1
    assert answer.read_text(encoding="utf-8") == "the answer"


def test_with_a_budget_the_budget_sentence_follows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch)
    ledger, target = _linked_ledger(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text("[budget]\nwindow_days = 7\ntokens = 1000\n", encoding="utf-8")
    argv, _ = _record(tmp_path)
    assert main(["--config", str(config), *argv]) == 0
    assert capsys.readouterr().err.splitlines() == [
        _not_recorded(f"{ledger} is not a regular file"),
        UNTRACKED,
    ]
    assert target.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_with_the_ledger_off_nothing_is_said(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    _gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    _linked_ledger(tmp_path)
    argv, _ = COMMANDS[command](tmp_path)
    assert main([*NO_CONFIG, *argv]) == 0
    assert "not recorded" not in capsys.readouterr().err


def _event() -> UsageEvent:
    return UsageEvent(
        timestamp=datetime.now(UTC),
        command="ask",
        model="m",
        dialect="openai",
        input_tokens=5,
        cached_tokens=0,
        output_tokens=2,
        reasoning_tokens=None,
        elapsed=1.0,
        status="ok",
        schema_used=False,
        error_kind=None,
        items_total=None,
        items_ok=None,
        items_failed=None,
        charge=7,
    )


def test_write_event_says_whether_a_write_was_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    ledger = tmp_path / "usage.jsonl"
    assert tuple(write_event(ledger, _event())) == (True, None)
    link = tmp_path / "link.jsonl"
    link.symlink_to(ledger)
    assert tuple(write_event(link, _event())) == (False, f"{link} is not a regular file")
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    assert tuple(write_event(link, _event())) == (False, None)
