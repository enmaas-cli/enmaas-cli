"""Files a reader opens as input are checked before the filename and count rule.

A pattern, script or ignore file is read by the tool even when its output holds
only names or counts, and ripgrep quotes a pattern file in a regex error. So a
protected file named by such an option denies the command.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from enmaas_cli.containment import check_tool
from enmaas_cli.outputs import protect

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
