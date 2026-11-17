"""Commands behind execution wrappers and inside shell strings are still checked."""

from __future__ import annotations

import inspect
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from enmaas_cli import containment
from enmaas_cli.containment import check_tool
from enmaas_cli.outputs import protect

READ = "full answer reads increase parent context cost"
UNCHECKED = "could not check this Bash input (ValueError)"
ALLOW_READ = "the human must run enmaas allow-read"
STDOUT = "ask --stdout increases parent context cost"
NARROW = "safe scan limit or time budget"
# The guard uses this code point as its own marker while it reads a command.
MARKER = "\ue0fe"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "answer.md").write_text("PRIVATE ANSWER", encoding="utf-8")
    protect(tmp_path / "answer.md")
    (tmp_path / "public.md").write_text("public", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "deep.md").write_text("PRIVATE ANSWER", encoding="utf-8")
    protect(tmp_path / "dir" / "deep.md")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def shell(root: Path, command: str) -> str | None:
    return check_tool({"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}})


def reads(root: Path, command: str) -> bool:
    return READ in (shell(root, command) or "")


def unchecked(root: Path, command: str) -> bool:
    return UNCHECKED in (shell(root, command) or "")


@pytest.mark.parametrize(
    "command",
    [
        "timeout 5 cat answer.md",
        "nice cat answer.md",
        "nohup cat answer.md",
        "stdbuf -o0 cat answer.md",
        "exec cat answer.md",
        "setsid cat answer.md",
        'bash -c "cat answer.md"',
        'sh -c "cat answer.md"',
        "eval cat answer.md",
    ],
)
def test_reported_reads_are_denied(root: Path, command: str) -> None:
    assert reads(root, command)


@pytest.mark.parametrize(
    "command,message",
    [
        ("timeout 30 enmaas allow-read answer.md", ALLOW_READ),
        ("timeout 60 enmaas ask -p x --stdout", STDOUT),
    ],
)
def test_reported_enmaas_commands_are_denied(root: Path, command: str, message: str) -> None:
    assert message in (shell(root, command) or "")


@pytest.mark.parametrize(
    "command",
    [
        "timeout 5 cat public.md",
        "timeout 5 echo answer.md",
        "nice ls",
        'bash -c "echo hi"',
        "eval echo hi",
    ],
)
def test_wrapped_harmless_commands_stay_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


# Every option form the six wrappers accept, each followed by a command.
TIMEOUT = [
    "timeout 5",
    "timeout 5s",
    "timeout 0.5m",
    "timeout .5h",
    "timeout 2d",
    "timeout -s KILL 5",
    "timeout -sKILL 5",
    "timeout -s9 5",
    "timeout --signal=KILL 5",
    "timeout --signal KILL 5",
    "timeout -k 2 5",
    "timeout -k2 5",
    "timeout --kill-after=2 5",
    "timeout --kill-after 2 5",
    "timeout --preserve-status 5",
    "timeout -p 5",
    "timeout --foreground 5",
    "timeout -f 5",
    "timeout -v 5",
    "timeout --verbose 5",
    "timeout -vk 2 5",
    "timeout -fpv -s KILL -k 1s 5",
    "timeout -- 5",
    "/usr/bin/timeout 5",
    "timeout '5'",
    "timeout $T",
    'timeout "$T"',
    "timeout ${TIMEOUT:-60}",
    "timeout $((60*5))",
    "timeout $(echo 5)",
    "timeout 1e3",
    "timeout inf",
    "timeout +5",
]
NICE = [
    "nice",
    "nice -n 5",
    "nice -n5",
    "nice -n -5",
    "nice --adjustment=5",
    "nice --adjustment 5",
    "nice -5",
    "nice --5",
    "nice -+5",
    "nice -5 -n 3",
    "nice --",
    "/usr/bin/nice -n 5",
]
NOHUP = ["nohup", "nohup --", "/usr/bin/nohup"]
STDBUF = [
    "stdbuf -o0",
    "stdbuf -o 0",
    "stdbuf -oL",
    "stdbuf --output=0",
    "stdbuf --output 0",
    "stdbuf -i0",
    "stdbuf -i 0",
    "stdbuf --input=0",
    "stdbuf --input 0",
    "stdbuf -e0",
    "stdbuf -e 0",
    "stdbuf --error=0",
    "stdbuf --error 0",
    "stdbuf -i0 -oL -e 0",
    "stdbuf -o0 --",
]
EXEC = [
    "exec",
    "exec -c",
    "exec -l",
    "exec -cl",
    "exec -a name",
    "exec -aname",
    "exec -cla name",
    "exec -c -l -a name",
    "exec --",
]
SETSID = [
    "setsid",
    "setsid -c",
    "setsid -f",
    "setsid -w",
    "setsid --ctty",
    "setsid --fork",
    "setsid --wait",
    "setsid -fw",
    "setsid -c -f -w",
    "setsid --",
]
WRAPPERS = TIMEOUT + NICE + NOHUP + STDBUF + EXEC + SETSID


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_reader_behind_a_wrapper_is_checked(root: Path, wrapper: str) -> None:
    assert reads(root, f"{wrapper} cat answer.md")
    assert reads(root, f"{wrapper} head -n 3 dir/deep.md")
    assert shell(root, f"{wrapper} cat public.md") is None
    assert shell(root, f"{wrapper} echo answer.md") is None
    assert shell(root, f"{wrapper} ls") is None


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_enmaas_behind_a_wrapper_is_checked(root: Path, wrapper: str) -> None:
    assert ALLOW_READ in (shell(root, f"{wrapper} enmaas allow-read answer.md") or "")
    assert STDOUT in (shell(root, f"{wrapper} enmaas ask -p x --stdout") or "")
    assert shell(root, f"{wrapper} enmaas inspect answer.md") is None
    assert shell(root, f"{wrapper} enmaas ask -p x --out saved.md") is None


@pytest.mark.parametrize(
    "command",
    [
        "timeout --bogus 5 cat public.md",
        "timeout -x 5 cat public.md",
        "timeout --verbose=1 5 cat public.md",
        "timeout -vx 5 cat public.md",
        "nice -z cat public.md",
        "nice --bogus cat public.md",
        "nohup -x cat public.md",
        "nohup --bogus cat public.md",
        "stdbuf -x0 cat public.md",
        "stdbuf --bogus=0 cat public.md",
        "exec -x cat public.md",
        "exec -cx cat public.md",
        "setsid -x cat public.md",
        "setsid --bogus cat public.md",
        "setsid --wait=1 cat public.md",
    ],
)
def test_unknown_wrapper_option_is_denied(root: Path, command: str) -> None:
    assert unchecked(root, command)
    assert unchecked(root, command.replace("public.md", "answer.md"))


def test_timeout_duration_is_not_taken_for_the_command(root: Path) -> None:
    # 5 is the duration and cat is the command.
    assert reads(root, "timeout 5 cat answer.md")
    # Here cat stands where the duration belongs. The real program refuses it
    # and runs nothing, so no file is read.
    assert shell(root, "timeout cat answer.md") is None
    assert shell(root, "timeout cat public.md") is None
    # The word after the duration is the command, whatever the duration holds.
    assert reads(root, "timeout cat cat answer.md")
    assert shell(root, "timeout") is None
    assert shell(root, "timeout 5") is None
    assert shell(root, "timeout -k 5") is None


@pytest.mark.parametrize(
    "command",
    [
        "timeout $T make",
        'timeout "$TIMEOUT" pytest',
        "timeout ${TIMEOUT:-60} npm test",
        "timeout $((60*5)) make",
        "timeout 1e3 make",
        "timeout inf make",
        "timeout +5 make",
    ],
)
def test_timeout_duration_that_is_not_a_plain_number_is_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "timeout $T cat answer.md",
        'timeout "$T" cat answer.md',
        "timeout ${TIMEOUT:-60} cat answer.md",
        "timeout -k 1 $T cat answer.md",
        "timeout $T nice cat answer.md",
    ],
)
def test_reader_behind_a_timeout_with_any_duration_is_denied(root: Path, command: str) -> None:
    assert reads(root, command)
    assert shell(root, command.replace("answer.md", "public.md")) is None


@pytest.mark.parametrize(
    "command",
    [
        "timeout --help",
        "nice --version",
        "nohup --help",
        "stdbuf --help",
        "setsid --version",
        "timeout --help cat answer.md",
        "nice --version cat answer.md",
        "nice",
        "nohup",
        "exec",
        "exec 3> out.txt",
    ],
)
def test_wrapper_that_runs_no_command_is_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "timeout 5 nice cat answer.md",
        "env timeout 5 cat answer.md",
        "sudo timeout 5 cat answer.md",
        "command timeout 5 cat answer.md",
        "timeout 5 command cat answer.md",
        "timeout 5 env X=1 cat answer.md",
        "exec timeout 5 cat answer.md",
        "time nice setsid -w cat answer.md",
        "timeout 5 nice -n 5 nohup cat answer.md",
        "sudo -u root timeout -k 1 5 stdbuf -oL cat answer.md",
        "env -i nohup setsid -f nice -n 19 cat answer.md",
        "X=1 timeout 5 cat answer.md",
        "if true; then timeout 5 cat answer.md; fi",
        "echo hi | timeout 5 cat answer.md",
        "echo $(timeout 5 cat answer.md)",
        "timeout 5 cat < answer.md",
        "timeout 5 nice bash -c 'cat answer.md'",
        "nohup sh -c 'nice cat answer.md'",
        "bash -c 'timeout 5 cat answer.md'",
        "eval timeout 5 nice cat answer.md",
    ],
)
def test_nested_wrappers_are_checked(root: Path, command: str) -> None:
    assert reads(root, command)
    assert shell(root, command.replace("answer.md", "public.md")) is None


def test_wrapper_in_front_of_xargs_keeps_the_xargs_rule(root: Path) -> None:
    assert unchecked(root, "printf answer.md | timeout 5 xargs cat")
    assert unchecked(root, "printf answer.md | xargs timeout 5 cat")
    assert shell(root, "printf answer.md | xargs timeout 5 echo") is None


SHELL_OPTIONS = [
    "bash -c",
    "sh -c",
    "dash -c",
    "zsh -c",
    "ksh -c",
    "/bin/bash -c",
    "/usr/bin/sh -c",
    "bash -lc",
    "bash -ec",
    "sh -ec",
    "bash -cl",
    "bash -l -c",
    "bash -e -u -c",
    "bash -euo pipefail -c",
    "bash -o pipefail -c",
    "bash -eo pipefail -o nounset -c",
    "bash -O extglob -c",
    "bash +e -c",
    "bash +o posix -c",
    "bash --norc --noprofile -c",
    "bash -norc -noprofile -c",
    "bash --login -c",
    "bash --posix -c",
    "bash --rcfile rc.sh -c",
    "bash --init-file rc.sh -c",
    "bash -c -e",
    "bash -c -o pipefail",
    "bash -c --",
    "bash -c -",
    "bash -oc pipefail",
    "zsh -fc",
    "zsh -rcfile",
    "sh -rcfile rc.sh -c",
    "ksh -o errexit -c",
]


@pytest.mark.parametrize("shell_options", SHELL_OPTIONS)
def test_shell_string_is_checked(root: Path, shell_options: str) -> None:
    assert reads(root, f"{shell_options} 'cat answer.md'")
    assert reads(root, f'{shell_options} "cat answer.md"')
    assert reads(root, f"{shell_options} 'cat answer.md' name argument")
    assert ALLOW_READ in (shell(root, f"{shell_options} 'enmaas allow-read answer.md'") or "")
    assert STDOUT in (shell(root, f"{shell_options} 'enmaas ask -p x --stdout'") or "")
    assert shell(root, f"{shell_options} 'enmaas inspect answer.md'") is None
    assert shell(root, f"{shell_options} 'cat public.md'") is None
    assert shell(root, f'{shell_options} "echo hi"') is None


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'echo x | cat answer.md'",
        "bash -c 'cat answer.md | head -1'",
        "bash -c 'echo hi; cat answer.md'",
        "bash -c 'true && cat answer.md'",
        "bash -c 'false || cat answer.md'",
        "bash -c 'echo hi\ncat answer.md'",
        "bash -c 'echo $(cat answer.md)'",
        'bash -c "echo $(cat answer.md)"',
        "bash -c 'echo `cat answer.md`'",
        'bash -c "echo \\$(cat answer.md)"',
        'bash -c "echo \\`cat answer.md\\`"',
        "echo $(bash -c 'cat answer.md')",
        "bash -c 'cat < answer.md'",
        "bash -c 'cat <(cat answer.md)'",
        "bash -c 'cat <<EOF\n$(cat answer.md)\nEOF\n'",
        "bash -c 'if true; then cat answer.md; fi'",
        "bash -c 'cd dir && cat deep.md'",
        "bash -c 'cd dir; cat deep.md'",
        "cd dir && bash -c 'cat deep.md'",
        "bash -c 'cat ~/answer.md'",
        'bash -c "cat ~/answer.md"',
        "bash -c 'cat \"answer.md\"'",
        "bash -c \"cat 'answer.md'\"",
        'bash -c "cat \\"answer.md\\""',
        "bash -c $'cat answer.md'",
        "bash -c $'cat \\x61nswer.md'",
        "bash -c 'cat $'\\''answer.md'\\'''",
        "bash -c 'cat ans''wer.md'",
        "bash -c cat\\ answer.md",
        "bash -c 'cat {answer,other}.md'",
        "bash -c 'cat ans*.md'",
        "bash -c 'grep -r PRIVATE .'",
        "bash -c 'cat answer.md' > out.txt",
        "echo hi | bash -c 'cat answer.md'",
        "true && bash -c 'cat answer.md'",
    ],
)
def test_reader_inside_a_shell_string_is_checked(root: Path, command: str) -> None:
    assert reads(root, command)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c \"sh -c 'cat answer.md'\"",
        "zsh --version -c 'cat answer.md'",
        "bash -c 'sh -c \"cat answer.md\"'",
        'bash -c "sh -c \'bash -c \\"cat answer.md\\"\'"',
        "sh -c 'eval cat answer.md'",
        "eval \"bash -c 'cat answer.md'\"",
        'bash -c \'eval "sh -c \\"cat answer.md\\""\'',
        "timeout 5 bash -c \"nice sh -c 'exec cat answer.md'\"",
    ],
)
def test_shell_strings_nest(root: Path, command: str) -> None:
    assert reads(root, command)
    assert shell(root, command.replace("answer.md", "public.md")) is None


@pytest.mark.parametrize(
    "command",
    [
        'bash -c "echo hi"',
        "bash -c 'echo cat answer.md'",
        "bash -c 'echo \"cat answer.md\"'",
        "bash -c 'ls; echo answer.md'",
        "bash -c 'cd dir && cat ../public.md'",
        "bash -c 'cd dir'; cat public.md",
        "bash -lc 'git status'",
        "bash -c",
        "bash",
        "bash script.sh",
        "bash -x script.sh",
        "bash -eu script.sh answer.md",
        "bash -- -c 'cat answer.md'",
        "bash script.sh -c 'cat answer.md'",
        "bash --version",
        "bash --help",
        "zsh --version",
        "ksh --version",
        "zsh --version -c 'cat public.md'",
        "sh -c 'echo $HOME'",
        "sh -c 'for f in *.py; do echo $f; done'",
    ],
)
def test_harmless_shell_invocations_stay_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "bash --bogus -c 'echo hi'",
        "bash -Z -c 'echo hi'",
        "bash -cZ 'echo hi'",
        "zsh -O -c 'echo hi'",
        "zsh --no-rcs -c 'echo hi'",
        "zsh -restricted 'echo hi'",
        "ksh -T tty -c 'echo hi'",
        "bash --rcfile=rc.sh -c 'echo hi'",
        "bash -c $'echo \\cA'",
        "bash -c \"echo \"$'\\cA'",
        "bash -c 'cat $'\\''\\cA'\\'''",
        "eval $'echo \\cA'",
        "eval echo $'\\cA'",
    ],
)
def test_shell_string_the_guard_cannot_read_is_denied(root: Path, command: str) -> None:
    assert unchecked(root, command)


@pytest.mark.parametrize(
    "command",
    [
        "eval cat answer.md",
        "eval 'cat answer.md'",
        'eval "cat answer.md"',
        'eval cat "answer.md"',
        "eval 'cat' 'answer.md'",
        "eval -- cat answer.md",
        "eval 'echo hi; cat answer.md'",
        "eval echo hi \\; cat answer.md",
        "eval 'echo hi | cat answer.md'",
        "eval 'echo $(cat answer.md)'",
        'eval "echo \\$(cat answer.md)"',
        "eval echo \\$\\(cat answer.md\\)",
        "eval 'cat < answer.md'",
        "eval eval cat answer.md",
        "eval \"eval 'cat answer.md'\"",
        "eval 'cat ~/answer.md'",
        "eval cat ~/answer.md",
        "command eval cat answer.md",
        "builtin eval cat answer.md",
        "eval 'cd dir && cat deep.md'",
        "eval 'cd dir'; cat deep.md",
        "eval cd dir && cat deep.md",
        "eval enmaas allow-read answer.md",
        "eval 'enmaas ask -p x --stdout'",
    ],
)
def test_eval_arguments_are_checked(root: Path, command: str) -> None:
    assert shell(root, command)
    assert not unchecked(root, command)


@pytest.mark.parametrize(
    "command",
    [
        "eval echo hi",
        "eval",
        "eval --",
        "eval ''",
        "eval 'echo answer.md'",
        "eval cat public.md",
        "eval 'echo \"cat answer.md\"'",
        "eval 'cd dir'; cat public.md",
        "eval enmaas inspect answer.md",
        'eval "$(ssh-agent -s)"',
    ],
)
def test_harmless_eval_stays_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


def test_escaped_dollar_in_double_quotes_keeps_its_reading_outside_shell_strings(
    root: Path,
) -> None:
    named = root / "a\\$b.md"
    named.write_text("PRIVATE ANSWER", encoding="utf-8")
    protect(named)
    assert reads(root, "cat 'a\\$b.md'")
    assert reads(root, 'cat "a\\$b.md"')
    assert reads(root, 'grep "\\$" answer.md')
    assert shell(root, 'sed -n "\\$p" public.md') is None
    assert shell(root, 'echo "\\$(cat answer.md)" "\\`cat answer.md\\`"') is None


def test_operands_after_the_shell_source_are_not_source(root: Path) -> None:
    # The shell runs only the first operand. The rest are its positional parameters.
    assert shell(root, "bash -c 'echo' cat answer.md") is None


def test_cd_inside_a_shell_string_does_not_move_later_commands(root: Path) -> None:
    # The cd runs in the child shell, so the later cat still runs in the first directory.
    assert shell(root, "bash -c 'cd dir'; cat deep.md") is None


def test_command_that_holds_the_marker_code_point_is_denied(root: Path) -> None:
    named = root / f"a{MARKER}b.md"
    named.write_text("PRIVATE ANSWER", encoding="utf-8")
    protect(named)
    assert unchecked(root, f"cat a{MARKER}b.md")
    assert unchecked(root, f"cat 'a{MARKER}b.md'")
    assert unchecked(root, f'cat "a{MARKER}b.md"')
    assert unchecked(root, f"timeout 5 bash -c 'cat a{MARKER}b.md'")
    assert unchecked(root, f"echo $(cat a{MARKER}b.md)")
    assert unchecked(root, f"echo {MARKER}")
    # Bash decodes each of these strings to the same file name.
    assert unchecked(root, r"cat $'a\ue0feb.md'")
    assert unchecked(root, r"""bash -c 'cat $'\''a\ue0feb.md'\'''""")
    assert unchecked(root, r"cat $'a\xee\x83\xbeb.md'")
    # The code point is refused even inside a string the guard cannot decode.
    assert unchecked(root, f"echo $'\\cA{MARKER}'")
    # A command without the code point is unaffected.
    assert reads(root, "cat answer.md")
    assert shell(root, "cat public.md") is None
    assert shell(root, r'echo "\$(cat answer.md)"') is None


@pytest.mark.parametrize(
    "tool,inputs",
    [
        ("Read", {"file_path": "{}"}),
        ("Grep", {"path": "{}", "pattern": "x", "output_mode": "content"}),
        ("Grep", {"path": "{}", "pattern": "x"}),
    ],
)
def test_file_tool_path_that_holds_the_marker_code_point_is_denied(
    root: Path, tool: str, inputs: dict[str, str]
) -> None:
    named = root / f"a{MARKER}b.md"
    named.write_text("PRIVATE ANSWER", encoding="utf-8")
    protect(named)

    def check(path: Path) -> str | None:
        tool_input = {key: value.format(path) for key, value in inputs.items()}
        return check_tool({"tool_name": tool, "cwd": str(root), "tool_input": tool_input})

    message = f"could not check this {tool} input (ValueError)"
    assert message in (check(named) or "")
    assert message in (check(root / f"missing{MARKER}.md") or "")
    # A path without the code point is unaffected.
    assert check(root / "public.md") is None
    if tool == "Read" or "output_mode" in inputs:
        assert READ in (check(root / "answer.md") or "")
    else:
        assert check(root / "answer.md") is None


# The wrappers the guard already knew, pinned so the walk through a long chain
# keeps every decision.
@pytest.mark.parametrize(
    "command",
    [
        "command cat answer.md",
        "command -p cat answer.md",
        "builtin cat answer.md",
        "env cat answer.md",
        "env -u X cat answer.md",
        "env --unset X cat answer.md",
        "env -i X=1 cat answer.md",
        "env -- cat answer.md",
        "time cat answer.md",
        "time -p cat answer.md",
        "time -o out.txt cat answer.md",
        "time --output=out.txt cat answer.md",
        "sudo cat answer.md",
        "sudo -u root -g wheel cat answer.md",
        "sudo --user=root cat answer.md",
        "sudo -n -- cat answer.md",
        "X=1 Y=2 cat answer.md",
        "if cat answer.md; then echo; fi",
        "! cat answer.md",
        "{ cat answer.md; }",
        "while ! cat answer.md; do echo; done",
        "command sudo env -i time -p nice cat answer.md",
        "command env sudo builtin cat answer.md",
    ],
)
def test_reader_behind_an_older_wrapper_is_checked(root: Path, command: str) -> None:
    assert reads(root, command)
    assert shell(root, command.replace("answer.md", "public.md")) is None


@pytest.mark.parametrize(
    "command",
    [
        "env -u",
        "env -x cat public.md",
        "sudo --bogus cat public.md",
        "sudo -u",
        "printf answer.md | xargs cat",
        "printf answer.md | xargs -n 1 cat",
        "printf answer.md | xargs -I{} cat {}",
        "printf answer.md | xargs -I {} cat {}",
        "printf x | xargs sudo -u root cat",
        "printf x | xargs env X=1 enmaas inspect",
    ],
)
def test_older_wrapper_the_guard_cannot_read_is_denied(root: Path, command: str) -> None:
    assert unchecked(root, command)


@pytest.mark.parametrize(
    "command",
    [
        "command -v cat answer.md",
        "command -V cat answer.md",
        "command -v",
        "command",
        "env",
        "time",
        "sudo",
        "xargs",
        "X=1",
        "printf x | xargs -n 1 echo",
        "printf x | xargs -0 -r echo answer.md",
        "printf x | xargs -n",
    ],
)
def test_older_wrapper_that_runs_no_reader_is_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


def test_wrapper_chain_checks_the_deadline_each_turn(root: Path) -> None:
    calls = 0

    def remaining() -> float:
        # The budget runs out right after the check starts.
        nonlocal calls
        calls += 1
        return 1.0 if calls == 1 else -1.0

    with patch.object(containment, "_remaining", remaining):
        reason = shell(root, "nice nice nice ls")
    assert reason and NARROW in reason


def test_very_long_wrapper_chain_ends_inside_the_time_budget(root: Path) -> None:
    budget = 1.0
    with patch.object(containment, "_TIME_BUDGET_SECONDS", budget):
        started = time.monotonic()
        harmless = shell(root, "nice " * 20_000 + "cat public.md")
        first = time.monotonic() - started
        started = time.monotonic()
        reader = shell(root, "nice " * 20_000 + "cat answer.md")
        second = time.monotonic() - started
    # A check that finishes in time gives its normal answer. One that meets
    # the deadline is denied. Neither answer comes long after the budget.
    assert harmless is None or NARROW in harmless
    assert reader and (READ in reader or NARROW in reader)
    assert first < budget + 1.5
    assert second < budget + 1.5


def test_long_wrapper_and_eval_chains_are_checked(root: Path) -> None:
    assert shell(root, "eval " * 20 + "cat public.md") is None
    assert reads(root, "eval " * 20 + "cat answer.md")
    assert shell(root, "nice " * 200 + "cat public.md") is None
    assert reads(root, "nice " * 200 + "cat answer.md")
    assert reads(root, "timeout 5 " * 600 + "cat answer.md")


def test_deep_shell_strings_stop_at_the_recursion_limit(root: Path) -> None:
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 150)
    try:
        reason = shell(root, "bash -c '" + "eval " * 200 + "cat public.md'")
    finally:
        sys.setrecursionlimit(limit)
    assert reason and "could not check this Bash input (RecursionError)" in reason


def test_deep_shell_strings_stop_at_the_deadline(root: Path) -> None:
    started = time.monotonic()
    with patch.object(containment, "_TIME_BUDGET_SECONDS", 0.2):
        reason = shell(root, "eval " * 600 + "cat public.md")
    assert reason and NARROW in reason
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("form", ["bash -c '{}'", "eval '{}'", "timeout 5 sh -c '{}'"])
def test_shell_strings_share_the_time_budget(tmp_path: Path, form: str) -> None:
    # Each scan alone fits inside the budget; together they do not.
    for index in range(8):
        (tmp_path / f"dir{index}").mkdir()
    scans = "; ".join(f"grep -r x dir{index}" for index in range(8))
    listing = os.scandir

    def slow(directory: object) -> object:
        time.sleep(0.1)
        return listing(str(directory))

    with (
        patch.object(containment, "_TIME_BUDGET_SECONDS", 0.3),
        patch("enmaas_cli.containment.os.scandir", slow),
    ):
        reason = shell(tmp_path, form.format(scans))
    assert reason and NARROW in reason
