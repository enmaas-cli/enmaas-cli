"""The guard reads Bash dollar quoting the way Bash does.

Bash turns $'...' into literal text with backslash escapes decoded, and reads
$"..." as "...". A guard that kept the dollar sign checked a different file
name than the one Bash opened, so the read went through.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from enmaas_cli import containment
from enmaas_cli.containment import check_tool
from enmaas_cli.outputs import protect

CANARY = "PROTECTED_RESPONSE_TEXT_6f2"
UNRESOLVED = "could not check this Bash input"


def bash_tool(cwd: Path, command: str) -> str | None:
    return check_tool({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    answer = tmp_path / "answer.md"
    answer.write_text(CANARY + "\n", encoding="utf-8")
    protect(answer)
    (tmp_path / "source.py").write_text("print('source')\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("command", ["cat $'answer.md'", "cat $'\\141nswer.md'"])
def test_dollar_quoted_protected_read_is_denied(workspace: Path, command: str) -> None:
    reason = bash_tool(workspace, command)
    assert reason and reason.startswith("enmaas containment: full answer")
    bash = shutil.which("bash")
    if bash is not None:
        # The command really does print the answer, so the denial matters.
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", command],
            cwd=workspace,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert CANARY in result.stdout


@pytest.mark.parametrize("command", ["cat $'source.py'", "cat $'\\163ource.py'"])
def test_dollar_quoted_ordinary_read_is_allowed(workspace: Path, command: str) -> None:
    assert bash_tool(workspace, command) is None


@pytest.mark.parametrize(
    "command",
    [
        'cat $"answer.md"',
        "cat ans$'wer'.md",
        "cat $'ans'\"wer\".md",
        "cat $'ans''wer.md'",
        "echo $(cat $'answer.md')",
        "echo \"$(cat $'\\x61nswer.md')\"",
        "echo `cat $'answer.md'`",
        "cat $'source.py' $'answer.md'",
        "cd $'.' && cat answer.md",
        # An escaped quote inside $'...' does not end it, so what follows is
        # still a command.
        "cat $'it\\'s' $(cat answer.md)",
        "echo $'\\'<<X'\ncat answer.md\nX",
        "echo $'\\'#' ; cat answer.md",
    ],
)
def test_dollar_quoting_in_any_position_is_read_as_bash_reads_it(
    workspace: Path, command: str
) -> None:
    assert bash_tool(workspace, command), command


@pytest.mark.parametrize(
    "command",
    [
        # Inside double quotes and after a backslash, $' is plain text.
        "cat \"$'answer.md'\"",
        "cat \\$'answer.md'",
        "cat source.py # $'\\q' it's",
        "cat <<'EOF'\n$'\\q' don't\nEOF",
        "cat <<EOF\n$'\\q' don't\nEOF",
        "echo $'\\q' $'a\\0b'",
        "printf %s $'\\cA' > out.txt",
    ],
)
def test_dollar_quoting_outside_a_command_word_changes_nothing(
    workspace: Path, command: str
) -> None:
    assert bash_tool(workspace, command) is None, command


def test_a_file_named_with_a_literal_dollar_quote_is_still_guarded(tmp_path: Path) -> None:
    literal = tmp_path / "$'x.md'"
    literal.write_text(CANARY, encoding="utf-8")
    protect(literal)
    assert bash_tool(tmp_path, "cat \"$'x.md'\"")
    assert bash_tool(tmp_path, "cat \\$\\'x.md\\'")


@pytest.mark.parametrize(
    "body",
    ["\\q", "a\\0b", "\\x", "\\cA", "\\?", "\\8", "\\uD800", "\\U00110000", "\\x00", "\\400"],
)
def test_dollar_quoting_the_guard_cannot_read_denies_a_reader(workspace: Path, body: str) -> None:
    for command in (f"cat $'{body}'", f"rg -l x $'{body}'", f"cat source.py $'{body}'"):
        reason = bash_tool(workspace, command)
        assert reason and UNRESOLVED in reason, command
    assert bash_tool(workspace, f"echo $'{body}'") is None


def test_unreadable_dollar_quoting_as_the_command_name_is_denied(workspace: Path) -> None:
    assert bash_tool(workspace, "$'\\q'cat answer.md")


# Each entry is the text between $' and '. Bash prints each one in the
# differential test below, and the guard must read the same bytes.
DIFFERENTIAL = [
    "answer.md",
    "\\141nswer.md",
    "\\x61nswer.md",
    "\\u0061nswer.md",
    "\\U00000061nswer.md",
    "a\\\\b",
    "it\\'s",
    'say\\"hi\\"',
    "tab\\there",
    "new\\nline",
    "cr\\rx",
    "bell\\a",
    "bs\\b",
    "esc\\e",
    "ESC\\E",
    "ff\\f",
    "vt\\v",
    "\\x41\\x42C",
    "\\x414",
    "\\1234",
    "\\777",
    "\\101\\60\\7",
    "\\u00e9t\\u00e9",
    "\\xc3\\xa9",
    "\\U0001F600",
    "\\u4e2d\\u6587",
    "\\x7f",
    "semi;colon|pipe&amp",
    "paren(x)<y>",
    "space here",
    "~tilde",
    "star*glob?[x]",
    "$HOME",
    "back`tick`",
    "hash # x",
    "\\u00411",
    "",
]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="compares with Linux bash")
def test_dollar_quote_reading_matches_bash(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    assert len(DIFFERENTIAL) >= 30
    script = "printf '%s\\0'" + "".join(f" $'{body}'" for body in DIFFERENTIAL)
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", script],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        capture_output=True,
        timeout=5,
        check=True,
    )
    printed = result.stdout.split(b"\0")[:-1]
    assert len(printed) == len(DIFFERENTIAL)
    for index, (body, expected) in enumerate(zip(DIFFERENTIAL, printed, strict=True)):
        decoded = containment._ansi_c(body)
        assert decoded is not None, body
        assert os.fsencode(decoded) == expected, body
        # The guard checks the file Bash would open, and only that file.
        if not expected or b"/" in expected or expected in {b".", b".."}:
            continue
        protected = tmp_path / "protected" / str(index)
        ordinary = tmp_path / "ordinary" / str(index)
        for directory in (protected, ordinary):
            directory.mkdir(parents=True)
            (directory / os.fsdecode(expected)).write_text("text", encoding="utf-8")
        protect(protected / os.fsdecode(expected))
        assert bash_tool(protected, f"cat $'{body}'"), body
        assert bash_tool(ordinary, f"cat $'{body}'") is None, body
