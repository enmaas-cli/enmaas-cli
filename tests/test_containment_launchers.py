"""The two restricted enmaas commands are denied behind any launcher."""

from __future__ import annotations

import inspect
import sys
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


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "answer.md").write_text("PRIVATE ANSWER", encoding="utf-8")
    protect(tmp_path / "answer.md")
    (tmp_path / "public.md").write_text("public", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def shell(root: Path, command: str) -> str | None:
    return check_tool({"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}})


def verdict(root: Path, command: str) -> str | None:
    """Name the denial a command gets, or None when it is allowed."""
    reason = shell(root, command)
    if reason is None:
        return None
    for message in (ALLOW_READ, STDOUT, READ, UNCHECKED, NARROW):
        if message in reason:
            return message
    return reason


@pytest.mark.parametrize(
    "command,message",
    [
        ("uv run enmaas ask -p hi --stdout", STDOUT),
        ("uvx enmaas allow-read answer.md", ALLOW_READ),
        ("pipx run enmaas ask -p hi --stdout", STDOUT),
        ("ionice enmaas allow-read answer.md", ALLOW_READ),
        ("flock x enmaas allow-read answer.md", ALLOW_READ),
        ("watch enmaas ask -p x --stdout", STDOUT),
    ],
)
def test_reported_commands_are_denied(root: Path, command: str, message: str) -> None:
    assert verdict(root, command) == message


@pytest.mark.parametrize(
    "command,message",
    [
        # The program named by a path.
        ("uv run /usr/bin/enmaas allow-read answer.md", ALLOW_READ),
        ("ionice /usr/bin/enmaas ask -p hi --stdout", STDOUT),
        ("ionice ./enmaas allow-read answer.md", ALLOW_READ),
        ("flock x ./enmaas ask -p hi --stdout", STDOUT),
        ("ionice ~/.local/bin/enmaas allow-read answer.md", ALLOW_READ),
        ("ionice .venv/bin/enmaas ask -p hi --stdout", STDOUT),
        ('ionice "$HOME/.local/bin/enmaas" allow-read answer.md', ALLOW_READ),
        # The end of the launcher's own options.
        ("uv run -- enmaas allow-read x", ALLOW_READ),
        ("uv run -- enmaas ask -p hi --stdout", STDOUT),
        ("ionice -- enmaas allow-read x", ALLOW_READ),
        # Launcher options before the command.
        ("uv run --quiet enmaas ask -p hi --stdout", STDOUT),
        ("uvx --from enmaas-cli enmaas allow-read x", ALLOW_READ),
        ("uv run --with rich --python 3.12 enmaas allow-read x", ALLOW_READ),
        ("uv tool run --from enmaas-cli enmaas ask -p hi --stdout", STDOUT),
        ("pipx run --spec enmaas-cli enmaas allow-read x", ALLOW_READ),
        ("ionice -c 3 enmaas allow-read x", ALLOW_READ),
        ("flock -n /tmp/lock enmaas ask -p hi --stdout", STDOUT),
        ("watch -n 5 enmaas allow-read x", ALLOW_READ),
        # Options of enmaas itself before the subcommand.
        ("ionice enmaas --config c.toml allow-read x", ALLOW_READ),
        ("ionice enmaas --config=c.toml allow-read x", ALLOW_READ),
        ("uv run enmaas --key-file k --config c.toml ask -p hi --stdout", STDOUT),
        # Every spelling of the option that the direct form refuses.
        ("uv run enmaas ask -p hi --stdout=1", STDOUT),
        ("uv run enmaas ask -p hi --stdou", STDOUT),
        ("uv run enmaas ask -p hi --stdo", STDOUT),
        ("uv run enmaas ask -p hi --std", STDOUT),
        ("uv run enmaas ask -p hi --st", STDOUT),
        ("uv run enmaas ask --stdout -p hi", STDOUT),
        # Quotes around a single word do not hide it.
        ("ionice 'enmaas' allow-read x", ALLOW_READ),
        ('uv run enmaas "allow-read" x', ALLOW_READ),
        ("uv run enmaas 'ask' -p hi \"--stdout\"", STDOUT),
        # A redirection between the words is not one of the words.
        ("ionice enmaas > log allow-read x", ALLOW_READ),
        ("uv run enmaas ask -p hi > out.txt --stdout", STDOUT),
        # Other launchers, known and unknown.
        ("poetry run enmaas allow-read x", ALLOW_READ),
        ("hatch run enmaas ask -p hi --stdout", STDOUT),
        ("pdm run enmaas allow-read x", ALLOW_READ),
        ("python -m enmaas allow-read x", ALLOW_READ),
        ("strace -f enmaas ask -p hi --stdout", STDOUT),
        ("chrt 0 enmaas allow-read x", ALLOW_READ),
        ("taskset 1 enmaas allow-read x", ALLOW_READ),
        ("systemd-run --user --wait enmaas ask -p hi --stdout", STDOUT),
        ("doas enmaas allow-read x", ALLOW_READ),
        ("runuser -u me -- enmaas allow-read x", ALLOW_READ),
        ("coproc enmaas allow-read x", ALLOW_READ),
        ("parallel enmaas allow-read ::: a b", ALLOW_READ),
        ("find . -name '*.md' -exec enmaas allow-read {} +", ALLOW_READ),
        ("some-unknown-launcher --flag value enmaas allow-read x", ALLOW_READ),
    ],
)
def test_launcher_forms_are_denied(root: Path, command: str, message: str) -> None:
    assert verdict(root, command) == message


@pytest.mark.parametrize(
    "command,message",
    [
        # A wrapper the guard reads through, in front of a launcher.
        ("timeout 5 uv run enmaas allow-read x", ALLOW_READ),
        ("timeout 5 ionice enmaas ask -p hi --stdout", STDOUT),
        ("nice ionice enmaas allow-read x", ALLOW_READ),
        ("nohup uv run enmaas ask -p hi --stdout", STDOUT),
        ("sudo uv run enmaas allow-read x", ALLOW_READ),
        ("sudo -u me uvx enmaas allow-read x", ALLOW_READ),
        ("env X=1 uv run enmaas allow-read x", ALLOW_READ),
        ("X=1 uv run enmaas ask -p hi --stdout", STDOUT),
        ("time uv run enmaas allow-read x", ALLOW_READ),
        ("command uv run enmaas allow-read x", ALLOW_READ),
        ("exec uv run enmaas ask -p hi --stdout", STDOUT),
        ("xargs uv run enmaas allow-read x", ALLOW_READ),
        # The -v belongs to uv here, so command still runs the launcher.
        ("command uv run -v enmaas allow-read x", ALLOW_READ),
        ("command uv run -v enmaas ask -p hi --stdout", STDOUT),
        # A reader as the command word.
        ("cat public.md enmaas allow-read x", ALLOW_READ),
        ("grep -c x public.md enmaas ask -p hi --stdout", STDOUT),
    ],
)
def test_wrappers_and_readers_in_front_are_denied(root: Path, command: str, message: str) -> None:
    assert verdict(root, command) == message


@pytest.mark.parametrize(
    "command,message",
    [
        # Shell text the guard already reads.
        ('bash -c "uv run enmaas allow-read x"', ALLOW_READ),
        ("sh -c 'uvx enmaas ask -p hi --stdout'", STDOUT),
        ('bash -lc "cd /tmp && uv run enmaas ask -p hi --stdout"', STDOUT),
        ('timeout 5 bash -c "ionice enmaas allow-read x"', ALLOW_READ),
        ("bash -c \"sh -c 'pipx run enmaas allow-read x'\"", ALLOW_READ),
        ("eval uv run enmaas allow-read x", ALLOW_READ),
        ('eval "ionice enmaas ask -p hi --stdout"', STDOUT),
        # Command lists, pipelines and groups.
        ("uv run pytest; uv run enmaas allow-read x", ALLOW_READ),
        ("uv run pytest && uvx enmaas allow-read x", ALLOW_READ),
        ("uv run pytest || ionice enmaas ask -p hi --stdout", STDOUT),
        ("echo hi\nuv run enmaas allow-read x", ALLOW_READ),
        ("uv run enmaas ask -p hi --stdout | head -5", STDOUT),
        ("echo hi | uv run enmaas ask --prompt-file /dev/stdin --stdout", STDOUT),
        ("(uv run enmaas allow-read x)", ALLOW_READ),
        ("{ uv run enmaas allow-read x; }", ALLOW_READ),
        ("if uv run enmaas allow-read x; then echo ok; fi", ALLOW_READ),
        ("for f in a b; do uv run enmaas allow-read $f; done", ALLOW_READ),
        ("uv run enmaas allow-read x &", ALLOW_READ),
        # Substitutions.
        ("echo $(uv run enmaas ask -p hi --stdout)", STDOUT),
        ('echo "$(uv run enmaas ask -p hi --stdout)"', STDOUT),
        ("echo `uvx enmaas allow-read x`", ALLOW_READ),
        ("x=$(ionice enmaas ask -p hi --stdout)", STDOUT),
        ("cat <(uv run enmaas ask -p hi --stdout)", STDOUT),
        ("cat <<EOF\n$(uv run enmaas ask -p hi --stdout)\nEOF", STDOUT),
    ],
)
def test_launcher_inside_shell_text_is_denied(root: Path, command: str, message: str) -> None:
    assert verdict(root, command) == message


@pytest.mark.parametrize(
    "command",
    [
        "uv run enmaas ask -p hi -o out.md",
        "uv run enmaas inspect answer.md",
        "uv run enmaas usage",
        "uv run enmaas models",
        "uv run enmaas",
        "uvx enmaas --version",
        "uv run enmaas --help | grep allow-read",
        "which enmaas",
        "type enmaas",
        "hash enmaas",
        "command -v enmaas",
        "pip install enmaas-cli",
        "pip show enmaas",
        "uv run pytest",
        "uv run pytest tests/test_containment.py -k allow-read",
        "grep enmaas README.md",
        "grep enmaas allow-read.md",
        "ls src/enmaas_cli",
        "uv tool install enmaas-cli",
        'echo "enmaas allow-read answer.md"',
        "echo 'uv run enmaas ask -p hi --stdout'",
        'git commit -m "document enmaas allow-read"',
        'git commit -m "enmaas ask --stdout is denied behind uv run"',
        'uv run enmaas ask -p "use --stdout" -o out.md',
        'uv run enmaas ask -p "how does enmaas allow-read work" -o out.md',
        "uv run enmaas ask -p hi -o out.md -- --stdout",
        # A quoted string is one word and is not split again.
        'uv run "enmaas allow-read x"',
        # The restricted words sit in another simple command.
        "uv run enmaas inspect x; echo allow-read",
        "uv run enmaas ask -p hi -o out.md | grep -- --stdout",
        "uv run enmaas usage && ls allow-read",
        "echo enmaas; echo allow-read x",
        # Heredoc text is data.
        "cat <<'EOF'\nuv run enmaas allow-read x\nEOF",
    ],
)
def test_harmless_commands_stay_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


# Words that run the command that follows them. The guard reads through some
# of them and knows nothing about the others.
LAUNCHERS = [
    "uv run",
    "uv run --",
    "uv run --quiet",
    "uv run --with rich",
    "uv tool run",
    "uvx",
    "uvx --from enmaas-cli",
    "pipx run",
    "pipx run --spec enmaas-cli",
    "ionice",
    "ionice -c 3",
    "flock x",
    "flock -n /tmp/lock",
    "watch",
    "watch -n 5",
    "poetry run",
    "hatch run",
    "pdm run",
    "python -m",
    "strace -f",
    "chrt 0",
    "doas",
    "some-unknown-launcher --flag value",
    "timeout 5 uv run",
    "nice -n 5 ionice",
    "nohup uv run",
    "stdbuf -oL uv run",
    "setsid uv run",
    "exec uv run",
    "sudo uv run",
    "sudo -u me uvx",
    "env X=1 uv run",
    "X=1 uv run",
    "time uv run",
    "command uv run",
    "! uv run",
]

# What follows the word enmaas, with the decision the direct form gets.
ENMAAS_ARGUMENTS = [
    ("allow-read answer.md", ALLOW_READ),
    ("allow-read x", ALLOW_READ),
    ("allow-read", ALLOW_READ),
    ("allow-read --help", ALLOW_READ),
    ("--config c.toml allow-read x", ALLOW_READ),
    ("--config=c.toml allow-read x", ALLOW_READ),
    ("--key-file k allow-read x", ALLOW_READ),
    ("--conf c.toml --key k allow-read x", ALLOW_READ),
    ("> log allow-read x", ALLOW_READ),
    ("allow-read x > log", ALLOW_READ),
    ("ask -p hi --stdout", STDOUT),
    ("ask --stdout -p hi", STDOUT),
    ("ask -p hi --stdout=1", STDOUT),
    ("ask -p hi --stdou", STDOUT),
    ("ask -p hi --stdo", STDOUT),
    ("ask -p hi --std", STDOUT),
    ("ask -p hi --st", STDOUT),
    ("ask -m m -f a.py -p hi --json --stdout", STDOUT),
    ("--config c.toml ask -p hi --stdout", STDOUT),
    ("ask -p hi > out.txt --stdout", STDOUT),
    ("ask -p hi --stdout | head -5", STDOUT),
    ("ask -p hi --stdout; echo done", STDOUT),
    ('ask -p "--stdout"', STDOUT),
    ("ask -p hi -o out.md", None),
    ('ask -p "use --stdout" -o out.md', None),
    ("ask -p 'inspect allow-read --stdout' -o out.md", None),
    ("ask -p hi -o out.md -- --stdout", None),
    ("ask -p hi --s", None),
    ("ask -p hi --json -o out.md", None),
    ("inspect answer.md", None),
    ("inspect answer.md --max-chars 200", None),
    ("usage", None),
    ("usage --json", None),
    ("models", None),
    ("map --items i.jsonl --template t.txt --outdir out", None),
    ("record --model m --input 1 --output 2", None),
    ("", None),
    ("--version", None),
    ("--help", None),
    ("--verbose allow-read x", None),
    ("-c c.toml allow-read x", None),
    ("allow-readx x", None),
    ("ask", None),
]

PROGRAMS = [
    "enmaas",
    "/usr/bin/enmaas",
    "./enmaas",
    "~/.local/bin/enmaas",
    ".venv/bin/enmaas",
    "'enmaas'",
    '"$HOME/.local/bin/enmaas"',
]


@pytest.mark.parametrize("arguments,expected", ENMAAS_ARGUMENTS)
@pytest.mark.parametrize("program", PROGRAMS)
def test_direct_form_keeps_its_decision(
    root: Path, program: str, arguments: str, expected: str | None
) -> None:
    assert verdict(root, f"{program} {arguments}") == expected


@pytest.mark.parametrize("arguments,expected", ENMAAS_ARGUMENTS)
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_launcher_form_gets_the_decision_of_the_direct_form(
    root: Path, launcher: str, arguments: str, expected: str | None
) -> None:
    assert verdict(root, f"enmaas {arguments}") == expected
    assert verdict(root, f"{launcher} enmaas {arguments}") == expected


@pytest.mark.parametrize("arguments,expected", ENMAAS_ARGUMENTS)
@pytest.mark.parametrize("program", PROGRAMS[1:])
@pytest.mark.parametrize("launcher", ["uv run", "ionice", "flock x", "timeout 5 uvx"])
def test_launcher_form_reads_the_program_by_its_base_name(
    root: Path, launcher: str, program: str, arguments: str, expected: str | None
) -> None:
    assert verdict(root, f"{launcher} {program} {arguments}") == expected


@pytest.mark.parametrize(
    "command,message",
    [
        # No command word is left after the wrappers.
        ("command -v enmaas allow-read", ALLOW_READ),
        ("timeout enmaas ask -p hi --stdout", STDOUT),
        # cd, with the words in the options of a wrapper in front of it.
        ("timeout -s enmaas allow-read cd x", ALLOW_READ),
        # A shell that is given no source, and one that is.
        ("bash script.sh enmaas allow-read x", ALLOW_READ),
        ('bash -c "echo hi" enmaas allow-read x', ALLOW_READ),
        ("eval true enmaas ask -p hi --stdout", STDOUT),
        # A command the guard does not know.
        ("echo enmaas allow-read x", ALLOW_READ),
        # A reader that prints names or counts only.
        ("grep -l x public.md enmaas allow-read y", ALLOW_READ),
        ("grep -c x public.md enmaas ask -p hi --stdout", STDOUT),
        # A reader whose files are all harmless.
        ("cat public.md enmaas allow-read x", ALLOW_READ),
        ("head -n 2 public.md enmaas ask -p hi --stdout", STDOUT),
    ],
)
def test_every_path_that_allows_a_command_ends_with_the_check(
    root: Path, command: str, message: str
) -> None:
    assert verdict(root, command) == message


def test_ripgrep_search_of_harmless_files_ends_with_the_check(root: Path) -> None:
    (root / "enmaas").write_text("public", encoding="utf-8")
    (root / "allow-read").write_text("public", encoding="utf-8")
    assert shell(root, "rg public public.md enmaas") is None
    assert verdict(root, "rg public public.md enmaas allow-read") == ALLOW_READ
    assert verdict(root, "rg public answer.md enmaas allow-read") == READ


@pytest.mark.parametrize(
    "command,message",
    [
        ("cat answer.md enmaas allow-read x", READ),
        ("uv run enmaas allow-read x < answer.md", READ),
        ('bash -c "cat answer.md" enmaas allow-read x', READ),
        ("grep -r PRIVATE . enmaas allow-read x", READ),
        ("xargs enmaas allow-read x", UNCHECKED),
        ("sudo --bogus uv run enmaas allow-read x", UNCHECKED),
        ("timeout --bogus 5 uv run enmaas allow-read x", UNCHECKED),
    ],
)
def test_existing_denial_keeps_its_own_message(root: Path, command: str, message: str) -> None:
    assert verdict(root, command) == message


@pytest.mark.parametrize(
    "command,message",
    [
        ("echo enmaas allow-read answer.md", ALLOW_READ),
        ("echo run enmaas ask -p hi --stdout to see it", STDOUT),
        ("git commit -m docs enmaas allow-read", ALLOW_READ),
        ("grep -rn enmaas allow-read src", ALLOW_READ),
        ("command -v enmaas allow-read", ALLOW_READ),
        ("which enmaas allow-read", ALLOW_READ),
        ("timeout enmaas allow-read x", ALLOW_READ),
        # The prompt of a launched ask, written without quotes.
        ("uv run enmaas ask -o out.md how to use enmaas allow-read", ALLOW_READ),
    ],
)
def test_unquoted_sentence_with_the_words_in_order_is_denied(
    root: Path, command: str, message: str
) -> None:
    assert verdict(root, command) == message


@pytest.mark.parametrize(
    "command",
    [
        'echo "enmaas allow-read answer.md"',
        'echo "run enmaas ask -p hi --stdout to see it"',
        'git commit -m "docs enmaas allow-read"',
        'grep -rn "enmaas allow-read" src',
        # The direct form reads only the words of the first enmaas.
        "enmaas ask -o out.md how to use enmaas allow-read",
    ],
)
def test_quoted_sentence_stays_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


@pytest.mark.parametrize(
    "command",
    [
        # The program name comes from a variable or a substitution.
        "e=enmaas; $e ask --stdout",
        "e=enmaas; $e allow-read answer.md",
        "e=enmaas; uv run $e allow-read answer.md",
        "uv run $(which enmaas) allow-read answer.md",
        # A launcher the guard does not read is given the command as one string.
        'ionice bash -c "enmaas allow-read x"',
        'flock x sh -c "enmaas ask -p hi --stdout"',
        'watch "enmaas ask -p hi --stdout"',
        # A script file.
        "bash script.sh",
        "uv run script.py",
        "ionice ./run-enmaas.sh",
    ],
)
def test_forms_outside_the_check_stay_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


# The word enmaas as the command word, behind a launcher the guard does not
# read, and behind a wrapper it reads through.
FORMS = ["enmaas", "uv run enmaas", "ionice enmaas", "timeout 5 uvx /usr/bin/enmaas"]

# A double dash ends the options of enmaas. The next word is the subcommand.
DOUBLE_DASH = [
    ("-- allow-read x", ALLOW_READ),
    ("-- allow-read", ALLOW_READ),
    ("-- ask -p hi --stdout", STDOUT),
    ("-- ask --stdout", STDOUT),
    ("--config c -- allow-read x", ALLOW_READ),
    ("--config=c -- allow-read x", ALLOW_READ),
    ("--key-file k --config c -- allow-read x", ALLOW_READ),
    ("--key-file k -- ask -p hi --stdout", STDOUT),
    ("-- inspect answer.md", None),
    ("-- usage", None),
    ("-- ask -p hi -o out.md", None),
    ("--config c -- ask -p hi -o out.md", None),
    ("--", None),
    ("--config c --", None),
    # The word after the double dash is the subcommand, whatever it is.
    ("-- -- allow-read x", None),
    ("-- --config c allow-read x", None),
    ("-- x allow-read", None),
    # A double dash after ask ends the search for --stdout.
    ("ask -o out.md -- --stdout", None),
    ("-- ask -o out.md -- --stdout", None),
]


@pytest.mark.parametrize("arguments,expected", DOUBLE_DASH)
@pytest.mark.parametrize("form", FORMS)
def test_double_dash_before_the_subcommand_does_not_hide_it(
    root: Path, form: str, arguments: str, expected: str | None
) -> None:
    assert verdict(root, f"{form} {arguments}") == expected


# Bash takes a redirection out of the words of a command wherever it stands.
# Each of these is one or more whole redirections.
REDIRECTIONS = [
    "2>/dev/null",
    "2>&1",
    ">&2",
    ">| log",
    "<> f",
    "&>> log",
    "&> log",
    "&>log",
    "2> log",
    "2>>log",
    "10>log",
    ">&-",
    "2>&-",
    "<&0",
    "2<>f",
    ">& log",
    "{fd}>log",
    "{fd}>&1",
    "1>&2 2>&1",
    ">log 2>&1",
    "2>&1 >log",
    "2>/dev/null >| log <> f",
]

# Where the redirection stands, with the decision of the command without it.
REDIRECTION_PLACES = [
    ("{} allow-read x", ALLOW_READ),
    ("{} ask -p hi --stdout", STDOUT),
    ("{} --config c allow-read x", ALLOW_READ),
    ("--config c {} allow-read x", ALLOW_READ),
    ("--config=c {} ask -p hi --stdout", STDOUT),
    ("--key-file k {0} --config c {0} allow-read x", ALLOW_READ),
    ("-- {} allow-read x", ALLOW_READ),
    ("{} -- ask -p hi --stdout", STDOUT),
    ("ask -p hi {} --stdout", STDOUT),
    ("allow-read {} x", ALLOW_READ),
    ("{} inspect answer.md", None),
    ("{} ask -p hi -o out.md", None),
    ("{} usage", None),
    ("{} --version", None),
    ("{}", None),
    ("--config c {}", None),
    ("-- {} inspect answer.md", None),
    ("inspect answer.md {}", None),
    ("ask -p hi -o out.md {}", None),
]


@pytest.mark.parametrize("place,expected", REDIRECTION_PLACES)
@pytest.mark.parametrize("redirection", REDIRECTIONS)
@pytest.mark.parametrize("form", FORMS[:2])
def test_redirection_before_the_subcommand_does_not_hide_it(
    root: Path, form: str, redirection: str, place: str, expected: str | None
) -> None:
    assert verdict(root, f"{form} {place.format(redirection)}") == expected


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ("2>/dev/null allow-read x", ALLOW_READ),
        ("2>&1 allow-read x", ALLOW_READ),
        (">&2 allow-read x", ALLOW_READ),
        (">| log allow-read x", ALLOW_READ),
        ("<> f allow-read x", ALLOW_READ),
        ("&>> log allow-read x", ALLOW_READ),
        ("&> log allow-read x", ALLOW_READ),
        ("2>/dev/null ask -p hi --stdout", STDOUT),
        ("2>&1 ask -p hi --stdout", STDOUT),
        (">&2 ask -p hi --stdout", STDOUT),
        (">| log ask -p hi --stdout", STDOUT),
        ("<> f ask -p hi --stdout", STDOUT),
        ("&>> log ask -p hi --stdout", STDOUT),
        ("&> log ask -p hi --stdout", STDOUT),
    ],
)
@pytest.mark.parametrize("form", FORMS)
def test_reported_redirections_are_denied(
    root: Path, form: str, arguments: str, expected: str
) -> None:
    assert verdict(root, f"{form} {arguments}") == expected


@pytest.mark.parametrize(
    "arguments",
    [
        "inspect answer.md 2>/dev/null",
        "2>/dev/null inspect answer.md",
        "2>/dev/null ask -p hi -o out.md",
        "usage > log",
        "2>&1",
        ">&2 --version",
        "usage 2>&1 | head -3",
        # Bash takes the word after the operator as the file name, so the
        # program gets x as its subcommand.
        ">| allow-read x",
        ">& allow-read x",
        "&> allow-read x",
        "2>&1 >| allow-read",
    ],
)
@pytest.mark.parametrize("form", FORMS)
def test_redirection_with_a_harmless_command_stays_allowed(
    root: Path, form: str, arguments: str
) -> None:
    assert shell(root, f"{form} {arguments}") is None


@pytest.mark.parametrize(
    "command",
    [
        # git reads the double dash, and allow-read is a path here.
        "git grep enmaas -- allow-read",
        # The program gets the word 2 as its subcommand and refuses it.
        "enmaas 2 allow-read x",
        "enmaas {fd} allow-read x",
        "uv run enmaas 10 -- allow-read x",
        # A quoted operator is a plain word for Bash. The guard reads it as a
        # redirection with its target.
        "enmaas '>&' log allow-read x",
        "enmaas \\>\\| log allow-read x",
    ],
)
def test_harmless_command_that_reads_like_a_hidden_subcommand_is_denied(
    root: Path, command: str
) -> None:
    assert verdict(root, command) == ALLOW_READ


@pytest.mark.parametrize(
    "arguments",
    [
        # A redirection between a global option and its value. The guard reads
        # the first word after the option as the value.
        "--config 2>/dev/null c allow-read x",
        "--config >&2 c allow-read x",
        "--key-file >| log k ask -p hi --stdout",
    ],
)
@pytest.mark.parametrize("form", FORMS[:2])
def test_redirection_inside_a_global_option_still_hides_the_subcommand(
    root: Path, form: str, arguments: str
) -> None:
    assert shell(root, f"{form} {arguments}") is None


@pytest.mark.parametrize(
    "command,message",
    [
        # pipx runs the only program of a package when given the package name.
        ("pipx run enmaas-cli allow-read x", ALLOW_READ),
        ("pipx run --spec . enmaas-cli allow-read x", ALLOW_READ),
        ("pipx run enmaas-cli==0.8.0 ask -p hi --stdout", STDOUT),
        ("pipx run enmaas-cli ask -p hi --stdout", STDOUT),
        ("uvx enmaas-cli allow-read x", ALLOW_READ),
        ("uv tool run enmaas-cli ask -p hi --stdout", STDOUT),
        ("timeout 5 pipx run enmaas-cli allow-read x", ALLOW_READ),
        ('bash -c "pipx run enmaas-cli allow-read x"', ALLOW_READ),
        # Each character that can start a version or a source after the name.
        ("pipx run enmaas-cli==0.8.0 allow-read x", ALLOW_READ),
        ("pipx run enmaas-cli~=0.8 allow-read x", ALLOW_READ),
        ("pipx run enmaas-cli!=0.7 allow-read x", ALLOW_READ),
        ("pipx run 'enmaas-cli>=0.8' allow-read x", ALLOW_READ),
        ('pipx run "enmaas-cli<1" allow-read x', ALLOW_READ),
        ("uvx enmaas-cli@0.8.0 allow-read x", ALLOW_READ),
        ("uvx enmaas-cli@git+https://example.com/e.git ask -p hi --stdout", STDOUT),
        ("pipx run 'enmaas-cli[extra]' allow-read x", ALLOW_READ),
        # Bash reads an unquoted > as a redirection, and the name stands alone.
        ("pipx run enmaas-cli>=0.8 allow-read x", ALLOW_READ),
        # The options of enmaas, a double dash and a redirection after the name.
        ("pipx run enmaas-cli --config c allow-read x", ALLOW_READ),
        ("pipx run enmaas-cli -- allow-read x", ALLOW_READ),
        ("pipx run enmaas-cli 2>/dev/null allow-read x", ALLOW_READ),
        # Accepted cost: any command with the package name in front of the words.
        ("ls enmaas-cli allow-read", ALLOW_READ),
        ("pip download enmaas-cli allow-read", ALLOW_READ),
        ("enmaas-cli allow-read x", ALLOW_READ),
    ],
)
def test_package_name_counts_as_an_enmaas_command(root: Path, command: str, message: str) -> None:
    assert verdict(root, command) == message


@pytest.mark.parametrize(
    "command",
    [
        "pip install enmaas-cli",
        "pip show enmaas-cli",
        "pip install 'enmaas-cli>=0.8'",
        "pip install -U enmaas-cli 2>&1 | tail -3",
        "uv tool install enmaas-cli==0.8.0",
        "pipx install enmaas-cli",
        "pipx run enmaas-cli inspect answer.md",
        "pipx run enmaas-cli ask -p hi -o out.md",
        "pipx run enmaas-cli==0.8.0 --version",
        "git clone https://github.com/enmaas-cli/enmaas-cli.git",
        "cd enmaas-cli && git status",
        # The words sit in one quoted string.
        'pipx run "enmaas-cli allow-read x"',
        # Other names that start with the package name, or end with it.
        "pipx run enmaas-cli-extra allow-read x",
        "pipx run enmaas-client allow-read x",
        "pipx run enmaas-cli.whl allow-read x",
        "pipx run enmaas-cli/ allow-read x",
        "pipx run ./enmaas-cli allow-read x",
        "pipx run xenmaas-cli allow-read x",
        # Other spellings of the package name are not read.
        "pipx run enmaas_cli allow-read x",
        "pipx run ENMAAS-CLI allow-read x",
        "pipx run 'enmaas-cli >=0.8' allow-read x",
    ],
)
def test_package_name_with_a_harmless_command_stays_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "xenmaas allow-read x",
        "ENMAAS allow-read x",
        "Enmaas ask -p hi --stdout",
        "uv run xenmaas allow-read x",
        "uv run ENMAAS ask -p hi --stdout",
        "ionice /usr/bin/xenmaas allow-read x",
        "ionice /usr/bin/ENMAAS allow-read x",
        "uv run enmaasx allow-read x",
        "uv run enmaas.py allow-read x",
        "uv run my-enmaas ask -p hi --stdout",
        # The assignment gives a variable the plain name. Bash runs allow-read.
        "X=enmaas allow-read x",
    ],
)
def test_other_program_names_stay_allowed(root: Path, command: str) -> None:
    assert shell(root, command) is None


@pytest.mark.parametrize(
    "command,expected",
    [
        # A word that only holds the name does not end the search for a start.
        ("ionice xenmaas enmaas allow-read x", ALLOW_READ),
        ("uv run enmaas-client enmaas ask -p hi --stdout", STDOUT),
        # The target of a redirection the guard removes is not one of the words.
        ("echo hi > enmaas allow-read x", None),
        ("uv run enmaas ask -p hi -o out.md > --stdout", None),
        # A quoted word of | or & holds no < or >, so it is not read as a redirection.
        ("enmaas '|' x allow-read y", None),
        ("uv run enmaas '&' x allow-read y", None),
    ],
)
def test_words_the_launcher_check_reads(root: Path, command: str, expected: str | None) -> None:
    assert verdict(root, command) == expected


def test_assignment_whose_value_names_the_program_counts_as_a_start(root: Path) -> None:
    # Accepted cost: Bash runs a program called allow-read here, not enmaas.
    assert verdict(root, "X=/usr/bin/enmaas allow-read x") == ALLOW_READ
    assert verdict(root, "X=./enmaas ask -p hi --stdout") == STDOUT
    assert shell(root, "X=/usr/bin/enmaas inspect answer.md") is None
    assert shell(root, "X=/usr/bin/enmaas") is None


@pytest.mark.parametrize(
    "command",
    [
        # No command word is left after the wrapper.
        "timeout -s enmaas allow-read",
        "timeout -s enmaas allow-read cd x",
        # A shell that is given no source, and one whose source is allowed.
        "timeout -s enmaas allow-read bash script.sh",
        'timeout -s enmaas allow-read bash -c "echo hi"',
        "timeout -s enmaas allow-read eval true",
        # A command the guard does not know.
        "timeout -s enmaas allow-read ls",
        # A search that prints names or counts only.
        "timeout -s enmaas allow-read grep -l x public.md",
        # A ripgrep search of harmless files.
        "timeout -s enmaas allow-read rg public public.md",
        # A reader whose files are all harmless.
        "timeout -s enmaas allow-read cat public.md",
    ],
)
def test_every_exit_reads_the_words_a_wrapper_took(
    root: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    # timeout takes enmaas as its signal and allow-read as its duration, so
    # the command it runs holds neither word. Only a check that reads every
    # word of the simple command denies these.
    monkeypatch.delenv("RIPGREP_CONFIG_PATH", raising=False)
    assert verdict(root, command) == ALLOW_READ


def test_launcher_check_stops_when_the_time_budget_is_spent(root: Path) -> None:
    spent = False
    check = containment._enmaas

    def spend(args: list[str]) -> str | None:
        # The first candidate uses up the budget.
        nonlocal spent
        spent = True
        return check(args)

    def remaining() -> float:
        return -1.0 if spent else 1.0

    with (
        patch.object(containment, "_enmaas", spend),
        patch.object(containment, "_remaining", remaining),
    ):
        reason = shell(root, "uv run enmaas inspect a enmaas inspect b")
    assert reason and NARROW in reason


def test_very_long_launcher_command_stops_at_the_deadline(root: Path) -> None:
    # The clock is patched: each look at it costs one tick of a budget of 100
    # ticks. No wall time is measured, so the load of the machine cannot
    # change the result.
    budget = 100
    looks = 0
    candidates = 0
    check = containment._enmaas

    def remaining() -> float:
        nonlocal looks
        looks += 1
        return float(budget - looks)

    def count(args: list[str]) -> str | None:
        nonlocal candidates
        candidates += 1
        # Fail at once, without reading the other candidates.
        assert candidates <= budget, "the check read on after the deadline"
        return check(args)

    with (
        patch.object(containment, "_remaining", remaining),
        patch.object(containment, "_enmaas", count),
    ):
        harmless = shell(root, "uv run " + "enmaas ask " * 10_000 + "-o out.md")
        read = candidates
        looks = candidates = 0
        restricted = shell(root, "uv run " + "enmaas ask " * 10_000 + "--stdout")
    # The harmless command holds 10,000 candidates. The check reads the clock
    # before each one and stops when the budget is spent.
    assert harmless and NARROW in harmless
    assert 0 < read < budget
    # The first candidate of the restricted command is denied.
    assert restricted and STDOUT in restricted
    assert candidates == 1


def test_long_launcher_commands_are_checked(root: Path) -> None:
    padding = "word " * 2_000
    assert shell(root, f"uv run {padding}enmaas inspect answer.md") is None
    assert verdict(root, f"uv run {padding}enmaas allow-read x") == ALLOW_READ
    assert verdict(root, f"uv run enmaas ask {padding}--stdout") == STDOUT
    assert shell(root, "ls " + "enmaas " * 2_000) is None
    assert verdict(root, "ls " + "enmaas " * 2_000 + "allow-read x") == ALLOW_READ


@pytest.mark.parametrize(
    "command,expected",
    [
        ("cat public.md", None),
        ("uv run enmaas inspect answer.md", None),
        ("cat answer.md", READ),
        ("uv run enmaas allow-read x", ALLOW_READ),
    ],
)
def test_launcher_check_keeps_the_depth_of_nested_shell_strings(
    root: Path, command: str, expected: str | None
) -> None:
    # Each nested shell string costs two calls: one of _shell and one of
    # _segment. A third call for each level would pass this limit, and the
    # guard would refuse these commands with a RecursionError.
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 200)
    try:
        reason = verdict(root, "eval " * 80 + command)
    finally:
        sys.setrecursionlimit(limit)
    assert reason == expected
