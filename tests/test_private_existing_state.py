"""State left by an earlier version is made private by the first command that opens it.

Before 0.8.0 the state directory was created with mode 0755 and the usage
ledger with mode 0644 under the usual umask, so other users on the machine
could read them. Creating new files private does not help a user who
upgrades. Every command that opens the state directory, the ledger, the
open-run records, the lock file or a `map` index removes group and other
access from it when this user owns it. The change is made on an open
descriptor, never by path, never through a link, and a failure is ignored.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from enmaas_cli import openruns
from enmaas_cli.cli import main
from enmaas_cli.usage import UsageEvent, append_event, load_events_counted, load_events_strict

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can write a read-only file, and owns every test file"
)

MODEL = "rits/zai-org/glm-5-3"
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "the answer"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}
ASK = ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"]
RECORD = ["record", "--model", "m", "--input", "5", "--command", "other-tool", "--output", "2"]
OLD_INDEX = '{"id": "old", "status": "ok"}\n'


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


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point main at a mock gateway and a scratch state. Returns the state directory."""
    for name in list(os.environ):
        if name.startswith("ENMAAS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=REPLY))
        ),
    )
    return tmp_path / "xdg" / "enmaas" / "runs"


def _old_ledger(state: Path) -> Path:
    """A state directory and a ledger with one row, with the modes 0.7.1 left."""
    state.mkdir(parents=True, exist_ok=True)
    ledger = state / "usage.jsonl"
    assert append_event(ledger, _event())
    ledger.chmod(0o644)
    state.chmod(0o755)
    return ledger


def _old_open_runs(state: Path, tmp_path: Path) -> tuple[Path, Path]:
    """The record of a run that is still alive, and the lock file, both mode 0644.

    The record names the parent process, which is alive, and an output
    directory that does not exist, so no command settles or charges it.
    """
    run = openruns.OpenRun(
        run_id="0123456789abcdef",
        outdir=str(tmp_path / "another-run"),
        offset=0,
        started=datetime.now(UTC).isoformat(),
        pid=os.getppid(),
        model="m",
        dialect="openai",
    )
    records = state / openruns.OPEN_RUNS_FILE
    records.write_text(json.dumps(asdict(run)) + "\n", encoding="utf-8")
    records.chmod(0o644)
    lock = state / openruns.LOCK_FILE
    lock.write_text("", encoding="utf-8")
    lock.chmod(0o644)
    return records, lock


def _no_config(tmp_path: Path) -> list[str]:
    return ["--config", str(tmp_path / "none.toml")]


def _budget(tmp_path: Path) -> list[str]:
    config = tmp_path / "config.toml"
    config.write_text("[budget]\nwindow_days = 7\ntokens = 1000\n", encoding="utf-8")
    return ["--config", str(config)]


def _map(tmp_path: Path, outdir: Path) -> list[str]:
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n', encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    argv = ["map", "-m", MODEL, "--dialect", "openai", "--retries", "0"]
    return [*argv, "--items", str(items), "--template", str(template), "--outdir", str(outdir)]


# The upgrade, through each command.


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_usage_alone_makes_the_directory_and_the_ledger_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    before = ledger.read_bytes()
    assert main([*_no_config(tmp_path), "usage", *flags]) == 0
    first = capsys.readouterr()
    assert _mode(state) == 0o700
    assert _mode(ledger) == 0o600
    assert ledger.read_bytes() == before
    # Nothing is printed about the change: a second run prints the same.
    assert main([*_no_config(tmp_path), "usage", *flags]) == 0
    second = capsys.readouterr()
    assert first.err == ""
    assert first.out == second.out
    if flags:
        assert json.loads(first.out)["total_calls"] == 1


def test_record_makes_the_directory_and_the_ledger_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    before = ledger.read_bytes()
    assert main([*_no_config(tmp_path), *RECORD]) == 0
    assert capsys.readouterr() == ("", "")
    assert _mode(state) == 0o700
    assert _mode(ledger) == 0o600
    after = ledger.read_bytes()
    assert after.startswith(before)
    assert len(after.splitlines()) == 2


@pytest.mark.parametrize("budgeted", [False, True])
def test_ask_makes_every_state_file_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    budgeted: bool,
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    records, lock = _old_open_runs(state, tmp_path)
    ledger_before = ledger.read_bytes()
    records_before = records.read_bytes()
    config = _budget(tmp_path) if budgeted else _no_config(tmp_path)
    assert main([*config, *ASK, "-o", str(tmp_path / "answer.md")]) == 0
    assert capsys.readouterr().err == ""
    assert _mode(state) == 0o700
    assert _mode(ledger) == 0o600
    assert _mode(lock) == 0o600
    assert _mode(records) == 0o600
    assert records.read_bytes() == records_before
    after = ledger.read_bytes()
    assert after.startswith(ledger_before)
    assert len(after.splitlines()) == 2


def test_ask_with_the_ledger_off_makes_the_directory_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    assert main([*_no_config(tmp_path), *ASK]) == 0
    capsys.readouterr()
    assert _mode(state) == 0o700
    # The ledger is not opened, so it is not changed.
    assert _mode(ledger) == 0o644


def test_map_makes_the_state_and_an_old_index_private_and_leaves_the_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    records, lock = _old_open_runs(state, tmp_path)
    ledger_before = ledger.read_bytes()
    records_before = records.read_bytes()
    outdir = tmp_path / "out"
    outdir.mkdir()
    outdir.chmod(0o755)
    index = outdir / "results.jsonl"
    index.write_text(OLD_INDEX, encoding="utf-8")
    index.chmod(0o644)
    assert main([*_budget(tmp_path), *_map(tmp_path, outdir)]) == 0
    assert capsys.readouterr().err == ""
    assert _mode(state) == 0o700
    assert _mode(ledger) == 0o600
    assert _mode(lock) == 0o600
    assert _mode(records) == 0o600
    assert records.read_bytes() == records_before
    assert ledger.read_bytes().startswith(ledger_before)
    assert _mode(index) == 0o600
    rows = index.read_text(encoding="utf-8")
    assert rows.startswith(OLD_INDEX)
    assert len(rows.splitlines()) == 3
    # The output directory is the user's, so its mode is theirs.
    assert _mode(outdir) == 0o755


def test_map_makes_an_old_index_private_even_when_it_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every item is already done, so no row is appended and only the check opens the index."""
    _gateway(tmp_path, monkeypatch)
    outdir = tmp_path / "out"
    outdir.mkdir()
    outdir.chmod(0o755)
    index = outdir / "results.jsonl"
    index.write_text('{"id": "a", "status": "ok"}\n', encoding="utf-8")
    index.chmod(0o644)
    (outdir / "a.md").write_text("done", encoding="utf-8")
    assert main([*_no_config(tmp_path), *_map(tmp_path, outdir), "--resume"]) == 0
    capsys.readouterr()
    assert _mode(index) == 0o600
    assert index.read_text(encoding="utf-8") == '{"id": "a", "status": "ok"}\n'
    assert _mode(outdir) == 0o755


def test_an_answer_file_keeps_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    _old_ledger(state)
    answers = tmp_path / "answers"
    answers.mkdir()
    answers.chmod(0o755)
    answer = answers / "answer.md"
    answer.write_text("an older answer", encoding="utf-8")
    answer.chmod(0o644)
    assert main([*_no_config(tmp_path), *ASK, "-o", str(answer)]) == 0
    capsys.readouterr()
    assert answer.read_text(encoding="utf-8") == "the answer"
    assert _mode(answer) == 0o644
    assert _mode(answers) == 0o755


# The readers and the open-run files, without a command.


def test_both_ledger_readers_make_the_ledger_and_its_directory_private(tmp_path: Path) -> None:
    for name, read in (("strict", load_events_strict), ("counted", load_events_counted)):
        state = tmp_path / name
        ledger = _old_ledger(state)
        read(ledger)
        assert _mode(state) == 0o700
        assert _mode(ledger) == 0o600


def test_the_budget_read_of_open_runs_makes_the_records_and_the_directory_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    records, lock = _old_open_runs(state, tmp_path)
    state.chmod(0o755)
    before = records.read_bytes()
    assert openruns.open_charge(state) == (0, True)
    assert _mode(state) == 0o700
    assert _mode(records) == 0o600
    assert records.read_bytes() == before
    # This read takes no lock, so the lock file is not opened.
    assert _mode(lock) == 0o644


# What is never changed.


def _as_another_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every file this user owns look like another user's."""
    mine = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: mine + 1)


@pytest.mark.parametrize("command", [["usage"], RECORD])
def test_a_directory_and_a_ledger_another_user_owns_are_not_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    _as_another_user(monkeypatch)
    assert main([*_no_config(tmp_path), *command]) == 0
    capsys.readouterr()
    assert _mode(state) == 0o755
    assert _mode(ledger) == 0o644


@pytest.mark.parametrize("command", [["usage"], RECORD])
def test_a_state_directory_that_is_a_link_leaves_its_target_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    target = tmp_path / "elsewhere"
    _old_ledger(target)
    state.parent.mkdir(parents=True)
    state.symlink_to(target, target_is_directory=True)
    assert main([*_no_config(tmp_path), *command]) == 0
    capsys.readouterr()
    assert _mode(target) == 0o755


def test_a_ledger_its_owner_made_read_only_gains_no_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only group and other access is removed, so a budgeted run still refuses this ledger."""
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    ledger.chmod(0o444)
    assert main([*_budget(tmp_path), *ASK]) == 1
    assert "cannot be written" in capsys.readouterr().err
    assert _mode(ledger) == 0o400
    assert _mode(state) == 0o700


# A mode change that fails.


def _failing_fchmod(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make every fchmod fail as it does on a read-only file system. Returns the calls."""
    calls: list[int] = []

    def fchmod(fd: int, mode: int) -> None:
        calls.append(mode)
        raise OSError(errno.EROFS, os.strerror(errno.EROFS))

    monkeypatch.setattr(os, "fchmod", fchmod)
    return calls


def test_record_still_writes_its_row_when_the_mode_cannot_be_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    calls = _failing_fchmod(monkeypatch)
    assert main([*_no_config(tmp_path), *RECORD]) == 0
    assert capsys.readouterr() == ("", "")
    assert sorted(calls) == [0o600, 0o700]
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
    assert _mode(ledger) == 0o644


def test_usage_json_is_unchanged_when_the_mode_cannot_be_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    _old_ledger(state)
    calls = _failing_fchmod(monkeypatch)
    assert main([*_no_config(tmp_path), "usage", "--json"]) == 0
    out, err = capsys.readouterr()
    assert sorted(calls) == [0o600, 0o700]
    assert err == ""
    assert json.loads(out)["total_calls"] == 1


def test_a_budgeted_ask_still_runs_and_records_when_no_mode_can_be_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    _old_open_runs(state, tmp_path)
    calls = _failing_fchmod(monkeypatch)
    assert main([*_budget(tmp_path), *ASK, "--json", "-o", str(tmp_path / "answer.md")]) == 0
    out, err = capsys.readouterr()
    assert calls
    assert err == ""
    assert json.loads(out)["status"] == "ok"
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2


# The two helpers.


def test_the_directory_change_never_goes_by_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enmaas_cli.outputs import make_state_dir_private

    def by_path(*args: object, **kwargs: object) -> None:
        raise AssertionError("the mode was changed by path")

    monkeypatch.setattr(os, "chmod", by_path)
    monkeypatch.setattr(os, "lchmod", by_path, raising=False)
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    make_state_dir_private(state)
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o700


def test_the_directory_helper_ignores_what_is_not_this_users_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enmaas_cli.outputs import make_state_dir_private

    make_state_dir_private(tmp_path / "missing")
    plain = tmp_path / "file"
    plain.write_text("", encoding="utf-8")
    plain.chmod(0o644)
    make_state_dir_private(plain)
    assert _mode(plain) == 0o644
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    make_state_dir_private(link)
    assert _mode(target) == 0o755
    _as_another_user(monkeypatch)
    make_state_dir_private(target)
    assert _mode(target) == 0o755


def test_the_file_helper_ignores_what_is_not_this_users_own_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enmaas_cli.outputs import make_file_private

    folder = tmp_path / "folder"
    folder.mkdir()
    folder.chmod(0o755)
    fd = os.open(folder, os.O_RDONLY)
    try:
        make_file_private(fd)
    finally:
        os.close(fd)
    assert _mode(folder) == 0o755
    plain = tmp_path / "file"
    plain.write_text("", encoding="utf-8")
    plain.chmod(0o644)
    _as_another_user(monkeypatch)
    fd = os.open(plain, os.O_RDONLY)
    try:
        make_file_private(fd)
    finally:
        os.close(fd)
    assert _mode(plain) == 0o644
