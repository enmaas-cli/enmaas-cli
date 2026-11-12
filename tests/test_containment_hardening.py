"""The guard under load and the files a reader opens as input."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from unittest.mock import patch

import pytest

from enmaas_cli import containment
from enmaas_cli.containment import check_tool
from enmaas_cli.outputs import protect

# The guard must answer inside its time budget and see files named in sed and awk programs.


NARROW = "safe scan limit or time budget"


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    output = tmp_path / "nested" / "answer with spaces.md"
    output.parent.mkdir()
    output.write_text("PRIVATE RESPONSE BODY", encoding="utf-8")
    protect(output)
    source = tmp_path / "source.py"
    source.write_text("print('source')", encoding="utf-8")
    return output, source


def event(name: str, cwd: Path, **inputs: object) -> dict[str, object]:
    if name == "Grep":
        inputs.setdefault("output_mode", "content")
    return {"tool_name": name, "cwd": str(cwd), "tool_input": inputs}


def bash(cwd: Path, command: str) -> str | None:
    return check_tool(event("Bash", cwd, command=command))


class _Entry:
    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.name = path.name

    def is_dir(self) -> bool:
        return False

    def is_symlink(self) -> bool:
        return False


class _SlowScandir:
    """Stand in for os.scandir on a slow file system: each entry takes a while."""

    def __init__(self, count: int, delay: float) -> None:
        self.count = count
        self.delay = delay

    def __call__(self, directory: object) -> _SlowScandir._Listing:
        return self._Listing(Path(str(directory)), self.count, self.delay)

    class _Listing:
        def __init__(self, directory: Path, count: int, delay: float) -> None:
            self.directory = directory
            self.count = count
            self.delay = delay

        def __enter__(self) -> Iterator[_Entry]:
            return self._entries()

        def __exit__(
            self,
            kind: type[BaseException] | None,
            value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

        def _entries(self) -> Iterator[_Entry]:
            for index in range(self.count):
                time.sleep(self.delay)
                yield _Entry(self.directory / f"file{index}.txt")


def test_slow_directory_scan_is_denied_inside_the_budget(tmp_path: Path) -> None:
    slow = _SlowScandir(count=100, delay=0.05)
    with (
        patch.object(containment, "_TIME_BUDGET_SECONDS", 0.2),
        patch("enmaas_cli.containment.os.scandir", slow),
    ):
        start = time.monotonic()
        reason = check_tool(event("Grep", tmp_path, pattern="."))
        elapsed = time.monotonic() - start
    assert reason and NARROW in reason
    assert reason.startswith("enmaas containment:")
    assert elapsed < 1.5


def test_environment_can_shorten_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENMAAS_GUARD_BUDGET_SECONDS", "0.2")
    slow = _SlowScandir(count=100, delay=0.05)
    with patch("enmaas_cli.containment.os.scandir", slow):
        start = time.monotonic()
        reason = check_tool(event("Bash", tmp_path, command="grep -r x ."))
        elapsed = time.monotonic() - start
    assert reason and NARROW in reason
    assert elapsed < 1.5


@pytest.mark.parametrize("value", ["100", "inf", "nan", "-1", "soon", ""])
def test_environment_cannot_lengthen_the_budget(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ENMAAS_GUARD_BUDGET_SECONDS", value)
    assert containment._time_budget() == containment._TIME_BUDGET_SECONDS


def test_nested_substitutions_share_one_budget(tmp_path: Path) -> None:
    # Each scan alone fits inside the budget; together they do not.
    directories = []
    for index in range(8):
        directory = tmp_path / f"dir{index}"
        directory.mkdir()
        directories.append(directory)
    command = "cat " + " ".join(f"$(grep -r x {d.name})" for d in directories)
    slow = _SlowScandir(count=3, delay=0.05)
    with (
        patch.object(containment, "_TIME_BUDGET_SECONDS", 0.3),
        patch("enmaas_cli.containment.os.scandir", slow),
    ):
        reason = bash(tmp_path, command)
    assert reason and NARROW in reason


def test_ripgrep_listing_timeout_is_denied(files: tuple[Path, Path]) -> None:
    _, source = files
    timeout = subprocess.TimeoutExpired(cmd=["rg"], timeout=1.0)
    with patch("enmaas_cli.containment.subprocess.run", side_effect=timeout) as run:
        reason = bash(source.parent, "rg -n x /usr")
    assert reason and NARROW in reason
    budget = run.call_args.kwargs["timeout"]
    assert 0 < budget <= containment._TIME_BUDGET_SECONDS


def test_ripgrep_listing_after_the_deadline_is_denied(files: tuple[Path, Path]) -> None:
    _, source = files
    with (
        patch.object(containment, "_TIME_BUDGET_SECONDS", 0.0),
        patch("enmaas_cli.containment.subprocess.run") as run,
    ):
        reason = bash(source.parent, "rg -n x .")
    assert reason and NARROW in reason
    run.assert_not_called()


def test_small_directory_checks_still_pass(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Grep", source.parent, pattern=".", glob="*.py")) is None
    assert bash(source.parent, "grep -r print source.py") is None
    assert bash(source.parent, "rg print source.py") is None
    reason = check_tool(event("Grep", source.parent, pattern="."))
    assert reason and str(output) in reason


def test_deadline_does_not_outlive_the_check(files: tuple[Path, Path]) -> None:
    _, source = files
    with patch.object(containment, "_TIME_BUDGET_SECONDS", 0.0):
        assert check_tool(event("Grep", source.parent, pattern="."))
    assert check_tool(event("Grep", source.parent, pattern=".", glob="*.py")) is None


@pytest.mark.parametrize(
    "template",
    [
        "sed -n 'r {out}' {src}",
        "sed -n '1r {out}' {src}",
        "sed '$R {out}' {src}",
        "sed -n '/print/r {out}' {src}",
        "sed -n 'p;r {out}' {src}",
        "sed -ne 'r {out}' {src}",
        "sed -e p -e 'r {out}' {src}",
        "sed --expression='r {out}' {src}",
        "sed 's/a/b/w {out}' {src}",
        "sed 's/a/b/gw {out}' {src}",
        "sed -n 'W {out}' {src}",
        "awk 'BEGIN{{while((getline s < \"{out}\")>0) print s}}' /dev/null",
        "awk 'BEGIN{{getline <\"{out}\"; print}}'",
        "awk 'BEGIN{{getline line[1] < \"{out}\"}}'",
        "gawk 'BEGIN{{\"{out}\" | getline; print}}'",
        "gawk -e 'BEGIN{{getline s < \"{out}\"}}' {src}",
        "gawk --source 'BEGIN{{getline s < \"{out}\"}}' {src}",
        "gawk --source='BEGIN{{getline s < \"{out}\"}}' {src}",
        "awk -F: 'BEGIN{{getline s < \"{out}\"}}' {src}",
    ],
)
def test_sed_and_awk_programs_cannot_read_protected_files(
    files: tuple[Path, Path], template: str
) -> None:
    output, source = files
    command = template.format(out=output, src=source.name)
    reason = bash(source.parent, command)
    assert reason and str(output) in reason, command


def test_sed_program_paths_are_relative_to_the_shell_directory(
    files: tuple[Path, Path],
) -> None:
    output, source = files
    assert bash(source.parent, f"cd nested && sed 'r {output.name}' ../source.py")
    assert bash(source.parent, f"awk 'BEGIN{{getline s < \"nested/{output.name}\"}}'")


@pytest.mark.parametrize(
    "command",
    [
        "sed -n 1p source.py",
        "awk '{print $1}' source.py",
        "sed 's/word/bar/' source.py",
        "sed -n '/x/p;s/r/w/g' source.py",
        "sed 'r /dev/stdin' source.py",
        "sed '1R -' source.py",
        "sed 'r nested' source.py",
        "awk 'BEGIN{getline s < f}' source.py",
        "awk '{ if ($1 < \"m\") print }' source.py",
        "gawk 'BEGIN{cmd | getline}' source.py",
    ],
)
def test_ordinary_sed_and_awk_programs_stay_allowed(files: tuple[Path, Path], command: str) -> None:
    _, source = files
    assert bash(source.parent, command) is None, command


# Files a reader opens as input are checked before the filename and count rule.
#
# A pattern, script or ignore file is read by the tool even when its output holds
# only names or counts, and ripgrep quotes a pattern file in a regex error. So a
# protected file named by such an option denies the command.


CANARY = "PROTECTED_PATTERN_TEXT_41c"


def bash_tool(cwd: Path, command: str) -> str | None:
    return check_tool({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    answer = tmp_path / "answer.md"
    # The open bracket makes the text an invalid regex, so rg quotes it.
    answer.write_text(CANARY + "[\n", encoding="utf-8")
    protect(answer)
    (tmp_path / "other.txt").write_text("ordinary\n", encoding="utf-8")
    (tmp_path / "pats.txt").write_text("ordinary\n", encoding="utf-8")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    return tmp_path


@pytest.mark.parametrize(
    "command", ["rg -l -f answer.md other.txt", "rg -c -f answer.md other.txt"]
)
def test_pattern_file_in_a_metadata_search_is_denied(workspace: Path, command: str) -> None:
    reason = bash_tool(workspace, command)
    assert reason and reason.startswith("enmaas containment: full answer")
    rg = shutil.which("rg")
    if rg is not None:
        # ripgrep really prints the pattern file in its error, so the denial matters.
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", command],
            cwd=workspace,
            env={"PATH": str(Path(rg).parent) + ":/usr/bin:/bin", "LANG": "C.UTF-8"},
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert CANARY in result.stdout + result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "rg -l -f answer.md other.txt",
        "rg -c -f answer.md other.txt",
        "rg -l --file=answer.md other.txt",
        "rg -c --file answer.md other.txt",
        "rg -l -fanswer.md other.txt",
        "rg -l -f ans*.md other.txt",
        "rg -l --ignore-file=answer.md x .",
        "rg --files --ignore-file answer.md",
        "grep -c -f answer.md other.txt",
        "grep -l --file=answer.md other.txt",
        "grep -c --exclude-from=answer.md x other.txt",
        "grep -l --include-from=answer.md -r x .",
        "grep -l --from=answer.md x",
        "grep -l --ignore-files=answer.md -r x .",
        "grep -c --config=answer.md x other.txt",
        "egrep -l -f answer.md other.txt",
        "fgrep -c --file=answer.md other.txt",
        "sed -n -f answer.md other.txt",
        "sed --file=answer.md other.txt",
        "awk -f answer.md other.txt",
        "awk --file=answer.md other.txt",
        "gawk -E answer.md other.txt",
        "gawk --exec=answer.md other.txt",
        "gawk -i answer.md 'BEGIN{}'",
        "gawk --include=answer.md 'BEGIN{}'",
        "gawk -l answer.md 'BEGIN{}'",
        "gawk --load=answer.md 'BEGIN{}'",
        "gawk -Danswer.md 'BEGIN{}'",
        "gawk --debug=answer.md 'BEGIN{}'",
        "jq -n -f answer.md",
        "jq -n --from-file answer.md",
        "jq -n --rawfile a answer.md '$a'",
        "jq -n --slurpfile a answer.md '$a'",
        "jq --rawfile answer answer.md -n '$answer'",
        "sed -n 'r answer.md' other.txt",
        "diff --from-file=answer.md other.txt",
        "diff --to-file=answer.md other.txt",
        "diff -X answer.md a b",
        "diff --exclude-from=answer.md a b",
        "sort --files0-from=answer.md",
        "sort --random-source=answer.md other.txt",
        "less -k answer.md other.txt",
        "less --lesskey-file=answer.md other.txt",
        "less --lesskey-src=answer.md other.txt",
        "less -T answer.md other.txt",
        "less --tag-file=answer.md other.txt",
        "strings @answer.md",
    ],
)
def test_protected_input_file_of_a_reader_is_denied(workspace: Path, command: str) -> None:
    reason = bash_tool(workspace, command)
    assert reason and reason.startswith("enmaas containment: full answer"), (command, reason)


@pytest.mark.parametrize(
    "command",
    [
        "rg -l pattern .",
        "rg -c pattern .",
        "rg -l pattern a",
        "grep -rl pattern .",
        "rg -l -f pats.txt other.txt",
        "rg -c --file=pats.txt other.txt",
        "grep -c -f pats.txt other.txt",
        "grep -l -f - other.txt",
        "sed -n -f pats.txt other.txt",
        "awk -f pats.txt other.txt",
        "jq -n --rawfile a pats.txt '$a'",
        "diff --from-file=pats.txt other.txt",
        "sort --files0-from=pats.txt",
        "strings @pats.txt",
    ],
)
def test_metadata_searches_and_ordinary_input_files_stay_allowed(
    workspace: Path, command: str
) -> None:
    assert bash_tool(workspace, command) is None, command
