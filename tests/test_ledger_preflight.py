"""A configured token budget that cannot be tracked refuses the run before any request.

A missing ledger reads as no spend. When it also cannot be created, every
run would spend, fail to record it, and let the next run start from zero
again. So with a rolling budget, `ask` and `map` first prove the ledger can
be appended, and refuse when it cannot.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import main

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can write a read-only file or directory"
)

MODEL = "rits/zai-org/glm-5-3"
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "the answer"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    on_request: Callable[[], None] | None = None,
) -> list[httpx.Request]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if on_request is not None:
            on_request()
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


def _config(tmp_path: Path, *, budget: bool) -> list[str]:
    config = tmp_path / "config.toml"
    config.write_text(
        "[budget]\nwindow_days = 7\ntokens = 1000\n" if budget else "", encoding="utf-8"
    )
    return ["--config", str(config)]


def _ask(tmp_path: Path) -> list[str]:
    return ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"] + [
        "-o",
        str(tmp_path / "answer.md"),
    ]


def _map(tmp_path: Path) -> list[str]:
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n', encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    return ["map", "-m", MODEL, "--dialect", "openai", "--retries", "0"] + [
        "--items",
        str(items),
        "--template",
        str(template),
        "--outdir",
        str(tmp_path / "out"),
    ]


COMMANDS = {"ask": _ask, "map": _map}


def _refusal(ledger: Path) -> str:
    return (
        f"the usage ledger {ledger} cannot be written, so the token budget "
        "cannot be tracked; fix it or remove the [budget] table from the config"
    )


@pytest.fixture
def read_only_dirs() -> Iterator[list[Path]]:
    """Directories a test made read-only. Made writable again, so cleanup can remove them."""
    made: list[Path] = []
    yield made
    for path in made:
        path.chmod(0o700)


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_state_directory_that_cannot_be_written_refuses_a_budgeted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_only_dirs: list[Path],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    state = _state(tmp_path)
    state.chmod(0o500)
    read_only_dirs.append(state)
    argv = [*_config(tmp_path, budget=True), *COMMANDS[command](tmp_path)]
    assert main(argv) == 1
    assert seen == []
    err = capsys.readouterr().err
    assert err.splitlines() == [f"error: {_refusal(state / 'usage.jsonl')}"]
    assert not (state / "usage.jsonl").exists()


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_read_only_ledger_refuses_a_budgeted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    ledger = _state(tmp_path) / "usage.jsonl"
    ledger.write_text("", encoding="utf-8")
    ledger.chmod(0o400)
    argv = [*_config(tmp_path, budget=True), *COMMANDS[command](tmp_path)]
    assert main(argv) == 1
    assert seen == []
    assert capsys.readouterr().err.splitlines() == [f"error: {_refusal(ledger)}"]


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_the_refusal_is_the_json_error_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_only_dirs: list[Path],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    state = _state(tmp_path)
    state.chmod(0o500)
    read_only_dirs.append(state)
    argv = [*_config(tmp_path, budget=True), *COMMANDS[command](tmp_path), "--json"]
    assert main(argv) == 1
    assert seen == []
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": _refusal(state / "usage.jsonl"),
    }


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_without_a_budget_the_run_goes_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    ledger = _state(tmp_path) / "usage.jsonl"
    ledger.write_text("", encoding="utf-8")
    ledger.chmod(0o400)
    argv = [*_config(tmp_path, budget=False), *COMMANDS[command](tmp_path)]
    assert main(argv) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert ledger.read_text(encoding="utf-8") == ""


def test_without_a_budget_map_goes_on_when_the_state_directory_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_only_dirs: list[Path],
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    state = _state(tmp_path)
    state.chmod(0o500)
    read_only_dirs.append(state)
    assert main([*_config(tmp_path, budget=False), *_map(tmp_path)]) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert not (state / "usage.jsonl").exists()


def test_a_budgeted_run_creates_a_missing_ledger_before_it_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "xdg" / "enmaas" / "runs" / "usage.jsonl"
    sizes: list[int] = []
    seen = _gateway(tmp_path, monkeypatch, lambda: sizes.append(ledger.stat().st_size))
    assert main([*_config(tmp_path, budget=True), *_ask(tmp_path)]) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert sizes == [0]
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_with_the_ledger_off_a_budgeted_run_is_refused_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    assert main([*_config(tmp_path, budget=True), *_ask(tmp_path)]) == 3
    assert seen == []
    err = capsys.readouterr().err
    assert "ENMAAS_NO_USAGE_LOG disables the ledger" in err
    assert not (tmp_path / "xdg" / "enmaas" / "runs" / "usage.jsonl").exists()
