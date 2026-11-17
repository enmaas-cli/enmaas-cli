"""State left by an earlier version is made private by the first command that runs.

Before 0.8.0 the state directory was created with mode 0755 and the usage
ledger with mode 0644 under the usual umask, so other users on the machine
could read them. Creating new files private does not help a user who
upgrades. Every command removes group and other access from an existing
state directory, and from the ledger, the open-run records, the lock file
or a `map` index when it opens one, when this user owns it. The change is
made on an open descriptor, never by path, never through a link, never on a
file with more than one hard link, and a failure is ignored.
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
from enmaas_cli.outputs import protect
from enmaas_cli.runner import _charge_row
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

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "one-model"}]})
        return httpx.Response(200, json=REPLY)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
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


# Every command, also one that opens no file in the state directory.


def _answer(tmp_path: Path) -> Path:
    answer = tmp_path / "an-answer.md"
    answer.write_text("an answer\n", encoding="utf-8")
    return answer


def _command(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], int]:
    """A command that opened no state file before, and the exit code it gives."""
    if name in ("map, ledger off", "record, ledger off"):
        monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    if name == "ask, no key":
        monkeypatch.delenv("ENMAAS_KEY")
        return [*_no_config(tmp_path), *ASK], 1
    if name == "models":
        return [*_no_config(tmp_path), "models", "--dialect", "openai"], 0
    if name == "models --json":
        return [*_no_config(tmp_path), "models", "--dialect", "openai", "--json"], 0
    if name == "inspect":
        return ["inspect", str(_answer(tmp_path))], 0
    if name == "allow-read":
        answer = _answer(tmp_path)
        # The mark is kept beside the answer, not in the state directory.
        protect(answer)
        return ["allow-read", str(answer)], 0
    if name == "map, ledger off":
        # Its one item is done, so the first run and a second one report the same.
        outdir = tmp_path / "out"
        outdir.mkdir()
        (outdir / "results.jsonl").write_text('{"id": "a", "status": "ok"}\n', encoding="utf-8")
        (outdir / "a.md").write_text("done", encoding="utf-8")
        return [*_no_config(tmp_path), *_map(tmp_path, outdir), "--resume"], 0
    assert name == "record, ledger off"
    return [*_no_config(tmp_path), *RECORD], 0


def _without_elapsed(out: str) -> str:
    """The output without the line of a `map` receipt that gives the time the run took."""
    return "".join(
        line for line in out.splitlines(keepends=True) if not line.startswith("  elapsed")
    )


FIRST_COMMANDS = [
    "models",
    "models --json",
    "inspect",
    "allow-read",
    "map, ledger off",
    "record, ledger off",
    "ask, no key",
]


@pytest.mark.parametrize("name", FIRST_COMMANDS)
def test_every_command_makes_an_existing_state_directory_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    _old_ledger(state)
    argv, code = _command(name, tmp_path, monkeypatch)
    assert main(argv) == code
    first = capsys.readouterr()
    assert _mode(state) == 0o700
    # Nothing is printed about the change: a second run, which has nothing
    # to change, gives the same code and prints the same.
    assert main(argv) == code
    second = capsys.readouterr()
    assert _without_elapsed(second.out) == _without_elapsed(first.out)
    assert second.err == first.err
    assert _mode(state) == 0o700


@pytest.mark.parametrize("name", ["models", "models --json", "inspect", "allow-read"])
def test_a_command_that_writes_no_state_does_not_create_the_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    argv, code = _command(name, tmp_path, monkeypatch)
    assert main(argv) == code
    capsys.readouterr()
    assert not state.exists()
    assert not (tmp_path / "xdg").exists()


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["ask", "--help"], ["no-such"], []])
def test_a_command_line_that_stops_at_its_arguments_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    _old_ledger(state)
    with pytest.raises(SystemExit):
        main(argv)
    capsys.readouterr()
    assert _mode(state) == 0o755


def test_a_home_directory_that_cannot_be_found_does_not_stop_a_local_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without XDG_STATE_HOME the state directory is under the home directory."""
    _gateway(tmp_path, monkeypatch)
    monkeypatch.delenv("XDG_STATE_HOME")

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    assert main(["inspect", str(_answer(tmp_path))]) == 0
    assert capsys.readouterr() == ("an answer\n", "")


@pytest.mark.parametrize("command", ["usage", "record", "ask", "map"])
def test_the_parents_of_the_state_directory_keep_their_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    _old_ledger(state)
    _old_open_runs(state, tmp_path)
    state.parent.chmod(0o755)
    state.parent.parent.chmod(0o755)
    argv = {
        "usage": ["usage"],
        "record": RECORD,
        "ask": ASK,
        "map": _map(tmp_path, tmp_path / "out"),
    }[command]
    assert main([*_budget(tmp_path), *argv]) == 0
    capsys.readouterr()
    assert _mode(state) == 0o700
    assert _mode(state.parent) == 0o755
    assert _mode(state.parent.parent) == 0o755


def test_a_read_only_state_directory_gains_no_write_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    state.chmod(0o555)
    try:
        assert main([*_no_config(tmp_path), "usage", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["total_calls"] == 1
        assert _mode(state) == 0o500
        assert _mode(ledger) == 0o600
    finally:
        # So the temporary directory can be removed.
        state.chmod(0o700)


# A file with more than one name.


def test_a_hard_linked_ledger_keeps_the_mode_of_the_shared_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mode belongs to the file, so a change would reach its other name too."""
    state = _gateway(tmp_path, monkeypatch)
    ledger = _old_ledger(state)
    other = tmp_path / "another-name.jsonl"
    os.link(ledger, other)
    other.chmod(0o664)
    assert main([*_no_config(tmp_path), "usage"]) == 0
    assert _mode(other) == 0o664
    assert main([*_no_config(tmp_path), *RECORD]) == 0
    assert capsys.readouterr().err == ""
    assert _mode(other) == 0o664
    assert _mode(ledger) == 0o664
    assert _mode(state) == 0o700
    # The row is still written, and it is one file under both names.
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
    assert other.read_bytes() == ledger.read_bytes()


@pytest.mark.parametrize("resume", [False, True])
def test_a_hard_linked_index_keeps_the_mode_of_the_shared_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    resume: bool,
) -> None:
    """With --resume nothing is sent, so only the check before the run opens the index."""
    _gateway(tmp_path, monkeypatch)
    outdir = tmp_path / "out"
    outdir.mkdir()
    index = outdir / "results.jsonl"
    index.write_text('{"id": "a", "status": "ok"}\n', encoding="utf-8")
    (outdir / "a.md").write_text("done", encoding="utf-8")
    other = tmp_path / "another-name.jsonl"
    os.link(index, other)
    other.chmod(0o664)
    argv = [*_no_config(tmp_path), *_map(tmp_path, outdir), *(["--resume"] if resume else [])]
    assert main(argv) == 0
    capsys.readouterr()
    assert _mode(other) == 0o664
    assert _mode(index) == 0o664
    assert other.read_bytes() == index.read_bytes()
    assert len(index.read_text(encoding="utf-8").splitlines()) == (1 if resume else 3)


def test_the_file_helper_changes_a_file_only_while_it_has_one_name(tmp_path: Path) -> None:
    from enmaas_cli.outputs import make_file_private

    plain = tmp_path / "file"
    plain.write_text("", encoding="utf-8")
    plain.chmod(0o644)
    other = tmp_path / "another-name"
    os.link(plain, other)
    fd = os.open(plain, os.O_RDONLY)
    try:
        make_file_private(fd)
        assert _mode(plain) == 0o644
        other.unlink()
        make_file_private(fd)
        assert _mode(plain) == 0o600
    finally:
        os.close(fd)


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


def test_a_budgeted_ask_leaves_the_index_an_open_run_record_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The budget check reads that index, in an output directory the user chose."""
    state = _gateway(tmp_path, monkeypatch)
    _old_ledger(state)
    records, _lock = _old_open_runs(state, tmp_path)
    run = json.loads(records.read_text(encoding="utf-8"))
    outdir = Path(run["outdir"])
    outdir.mkdir()
    outdir.chmod(0o755)
    index = outdir / "results.jsonl"
    index.write_text(_charge_row(11, items=1, run=run["run_id"]), encoding="utf-8")
    index.chmod(0o644)
    reads: list[openruns.IndexRead] = []
    real_read = openruns.charge_since

    def charge_since(run: openruns.OpenRun, budget: int | None = None) -> openruns.IndexRead:
        reads.append(real_read(run, budget))
        return reads[-1]

    monkeypatch.setattr(openruns, "charge_since", charge_since)
    assert main([*_budget(tmp_path), *ASK, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    # The command read the index and found the charge in it.
    assert [read.charge for read in reads] == [11]
    assert _mode(index) == 0o644
    assert _mode(outdir) == 0o755
    assert _mode(records) == 0o600


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
    # The start of the refusal. The rest names the ledger path, and a long
    # path pushes the words after it past the cut at 200 characters.
    assert capsys.readouterr().err.startswith("error: the usage ledger ")
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
    # The directory is tried twice: when the command starts, and when the
    # ledger is opened.
    assert sorted(calls) == [0o600, 0o700, 0o700]
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
    assert sorted(calls) == [0o600, 0o700, 0o700]
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

    state = tmp_path / "state"
    state.mkdir()
    # Set after the directory is made, so the umask cannot make it private first.
    state.chmod(0o755)
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o755
    monkeypatch.setattr(os, "chmod", by_path)
    monkeypatch.setattr(os, "lchmod", by_path, raising=False)
    make_state_dir_private(state)
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o700


def test_the_directory_helper_only_removes_permissions(tmp_path: Path) -> None:
    from enmaas_cli.outputs import make_state_dir_private

    state = tmp_path / "state"
    state.mkdir()
    for before, after in ((0o555, 0o500), (0o750, 0o700), (0o7777, 0o700)):
        state.chmod(before)
        try:
            make_state_dir_private(state)
            assert _mode(state) == after
        finally:
            state.chmod(0o700)


def test_the_file_helper_only_removes_permissions(tmp_path: Path) -> None:
    from enmaas_cli.outputs import make_file_private

    plain = tmp_path / "file"
    plain.write_text("", encoding="utf-8")
    fd = os.open(plain, os.O_RDONLY)
    try:
        for before, after in ((0o444, 0o400), (0o640, 0o600), (0o6755, 0o600), (0o200, 0o200)):
            plain.chmod(before)
            make_file_private(fd)
            assert _mode(plain) == after
    finally:
        os.close(fd)


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
