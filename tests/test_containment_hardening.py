"""The guard must answer inside its time budget and see files named in sed and awk programs."""

from __future__ import annotations

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
