"""Guard common literal reads against accidental answer context growth.

This module does not execute shell input or provide a filesystem sandbox.

Every check must finish inside a time budget. A host kills a hook that runs past
its own timeout, and some hosts then run the tool call anyway, so a slow check
would let the read through. When the budget runs out, the call is denied. The
environment variable ENMAAS_GUARD_BUDGET_SECONDS can shorten the budget (tests
use it); it cannot make the budget longer than the default.
"""

from __future__ import annotations

import fnmatch
import glob
import math
import os
import re
import shlex
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from enmaas_cli.outputs import MARKER_DIR, is_protected, state_root


def _words(value: str) -> set[str]:
    """Keep the static reader and option tables compact."""
    return set(value.split())


_MAX_SCAN_ENTRIES = 250_000
# Hosts register the hook with a 5 second timeout. Python startup and output
# need part of that, so the checks themselves get 3 seconds.
_TIME_BUDGET_SECONDS = 3.0
_BUDGET_VARIABLE = "ENMAAS_GUARD_BUDGET_SECONDS"
# check_tool sets this from the start of each check. Nested shell substitutions
# share it, so many small scans cannot add up to more than one budget.
_deadline = math.inf
_MAX_SHELL_DIRS = 32
_SHELL_TOOLS = {"Bash", "exec_command", "shell_command", "shell"}
_READERS = _words(
    """
cat head tail sed awk gawk grep egrep fgrep rg bat less more jq diff sort cut uniq paste
join strings xxd od base64 nl fold fmt tac
"""
)
_SEARCHERS = {"grep", "egrep", "fgrep", "rg"}
_SEARCH_VALUES = _words(
    """
-m -A -B -C -e -f --max-count --after-context --before-context --context --regexp --file
"""
)
_RG_VALUES = _words(
    """
-d -E -g -j -M -r -t -T --pre --pre-glob --dfa-size-limit --encoding --engine
--regex-size-limit --threads --glob --iglob --ignore-file --max-depth --max-filesize
--type --type-not --type-add --type-clear --color --colors --context-separator
--field-context-separator --field-match-separator --hostname-bin --hyperlink-format
--max-columns --path-separator --replace --sort --sortr --generate
"""
)
_GREP_VALUES = _words(
    """
-d -D --label --binary-files --directories --devices --include --exclude --exclude-from
--exclude-dir --group-separator --include-from --from
"""
)
_SEARCH_SWITCHES = _words(
    """
--files --files-with-matches --files-without-match --count --count-matches --ignore-case
--case-sensitive --fixed-strings --line-regexp --word-regexp --invert-match --text
--line-number --no-line-number --with-filename --no-filename --only-matching --quiet
--null --help --version
"""
)
_RG_SWITCHES = _words(
    """
--search-zip --crlf --mmap --multiline --multiline-dotall --no-unicode --null-data
--pcre2 --smart-case --stop-on-nonmatch --auto-hybrid-regex --no-pcre2-unicode --binary
--follow --glob-case-insensitive --hidden --ignore-file-case-insensitive --no-ignore
--no-ignore-dot --no-ignore-exclude --no-ignore-files --no-ignore-global
--no-ignore-parent --no-ignore-vcs --no-require-git --one-file-system --unrestricted
--block-buffered --byte-offset --column --no-context-separator --heading --include-zero
--line-buffered --max-columns-preview --passthru --pretty --trim --vimgrep --sort-files
--json --debug --no-ignore-messages --no-messages --stats --trace --no-config
--pcre2-version --type-list
"""
) | {"-" + char for char in "zsFivxUPSawL.bhnN0opqHIcluV"}
_GREP_SWITCHES = _words(
    """
--extended-regexp --basic-regexp --perl-regexp --no-ignore-case --null-data
--no-messages --byte-offset --line-buffered --silent --recursive --dereference-recursive
--initial-tab --no-group-separator --color --colour --binary --ignore-files --config
"""
) | {"-" + char for char in "EFGPiwxzsvVbnHhoqaIrRLTZUlc"}
_VALUE_OPTIONS = {
    "head": {"-n", "-c", "--lines", "--bytes"},
    "tail": {"-n", "-c", "--lines", "--bytes", "--sleep-interval"},
    "sed": {"-e", "-f", "--expression", "--file"},
    "awk": _words("-F -v -f -e --file --source -E --exec -i --include -l --load"),
    "gawk": _words("-F -v -f -e --file --source -E --exec -i --include -l --load"),
    "cut": {"-b", "-c", "-f", "-d", "--bytes", "--characters", "--fields", "--delimiter"},
    "sort": _words("-k -t -S -o --key --field-separator --output --files0-from --random-source"),
    "diff": {"-X", "--exclude-from", "--from-file", "--to-file"},
    "less": {"-k", "--lesskey-file", "--lesskey-src", "-T", "--tag-file"},
    "jq": {
        "-f",
        "--from-file",
        "-L",
        "--library-path",
        "--indent",
        "--arg",
        "--argjson",
        "--rawfile",
        "--slurpfile",
    },
}
# Options whose value names a file the reader opens and reads: a pattern,
# script, ignore, key or name-list file. A tool can quote such a file in an
# error, so it is checked even when the output holds only names or counts.
# The grep set also names ugrep options, because some hosts run ugrep as grep.
_GREP_INPUTS = _words("-f --file --exclude-from --include-from --from --ignore-files --config")
_AWK_INPUTS = _words("-f --file -E --exec -i --include -l --load -D --debug")
_INPUT_FILE_OPTIONS = {
    "grep": _GREP_INPUTS,
    "egrep": _GREP_INPUTS,
    "fgrep": _GREP_INPUTS,
    "rg": {"-f", "--file", "--ignore-file"},
    "sed": {"-f", "--file"},
    "awk": _AWK_INPUTS,
    "gawk": _AWK_INPUTS,
    "jq": {"-f", "--from-file", "--rawfile", "--slurpfile"},
    "diff": {"-X", "--exclude-from", "--from-file", "--to-file"},
    "sort": {"--files0-from", "--random-source"},
    "less": {"-k", "--lesskey-file", "--lesskey-src", "-T", "--tag-file"},
}
# Short options whose value is optional and, when given, attached: gawk -Dfile.
_ATTACHED_VALUE_OPTIONS = {"awk": {"-D"}, "gawk": {"-D"}}
_PREFIX = "enmaas containment: "
# Preserve quoted and escaped punctuation until command boundaries are checked.
_QUOTED_PUNCTUATION = str.maketrans({char: chr(0xE000 + i) for i, char in enumerate(";&|()<>\n~")})
_QUOTED_TILDE = str(_QUOTED_PUNCTUATION[ord("~")])
_LITERAL_PUNCTUATION = str.maketrans(
    {value: key for key, value in _QUOTED_PUNCTUATION.items() if key != ord("~")}
)


# A sed r, R, w or W command takes the rest of its line as a file name. The
# command letter follows the start of a line, a separator, an address, or the
# flags of an s command, so text inside an s pattern is rarely mistaken for it.
# The lookahead tries every position, so an early false match such as the "pr"
# in /print/r FILE cannot hide the real command after it.
_SED_STARTS = rf"\s;{{}}!$0-9/,{re.escape(_QUOTED_TILDE)}"
_SED_FILE_COMMAND = re.compile(
    rf"(?:^|(?<=[{_SED_STARTS}]))(?=[gpiImMe0-9]*[rRwW][ \t]*([^\n]+))", re.MULTILINE
)
# awk opens a quoted literal file with getline < "path" and runs one with
# "path" | getline. Names held in variables are not followed.
_AWK_GETLINE_FILE = re.compile(r'getline\b[^<"\n;]*<\s*"([^"\n]*)"')
_AWK_PIPE_GETLINE = re.compile(r'"([^"\n]*)"\s*\|&?\s*getline\b')
_PROGRAM_READERS = {"sed", "awk", "gawk"}
_HEREDOC_START = re.compile(r"<<(-?)[ \t]*(?:'([^']+)'|\"([^\"]+)\"|([\w]+))")

# Bash decodes these escapes inside $'...'. Any other escape is left to Bash
# rules the guard does not copy, so the string counts as unreadable.
_ANSI_C_ESCAPES = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}
_ANSI_C_OCTAL = re.compile(r"[0-7]{1,3}")
_ANSI_C_HEX = {
    "x": re.compile(r"[0-9A-Fa-f]{1,2}"),
    "u": re.compile(r"[0-9A-Fa-f]{1,4}"),
    "U": re.compile(r"[0-9A-Fa-f]{1,8}"),
}
# Stands in for a $'...' string the guard cannot read. A command that runs a
# known reader with it anywhere in its words is denied, because the guard
# cannot tell which file the reader opens.
_UNRESOLVED = "\ue0ff"
# Runs of text that hold no character the dollar quote pass acts on.
_UNQUOTED_PLAIN = re.compile(r"[^\\'\"$`#<>()\n]+")
_QUOTED_PLAIN = re.compile(r"[^\\\"$`]+")


def _shell_value(value: str) -> str:
    return value.translate(_LITERAL_PUNCTUATION)


def _denial(path: Path) -> str:
    return (
        f"{_PREFIX}full answer reads increase parent context cost. "
        f"Use enmaas inspect {shlex.quote(str(path))} for a bounded preview. "
        "For a full read, ask the human to run enmaas allow-read in their own terminal."
    )


def _time_budget() -> float:
    """Return the budget in seconds. The environment may only shorten it."""
    try:
        value = float(os.environ.get(_BUDGET_VARIABLE, ""))
    except ValueError:
        return _TIME_BUDGET_SECONDS
    if not 0 <= value < _TIME_BUDGET_SECONDS:
        return _TIME_BUDGET_SECONDS
    return value


def _remaining() -> float:
    return _deadline - time.monotonic()


def _narrow() -> str:
    return (
        f"{_PREFIX}directory check exceeded its safe scan limit or time budget. "
        "Use a narrow file path or smaller directory before searching content."
    )


def _path(value: str, cwd: Path) -> Path:
    path = Path(str(Path(value).expanduser()).replace(_QUOTED_TILDE, "~"))
    return path if path.is_absolute() else cwd / path


def _matches(path: Path, root: Path, pattern: str | None) -> bool:
    return (
        pattern is None
        or any(char in pattern for char in "{}!")
        or fnmatch.fnmatch(path.name, pattern)
        or _glob_matches(path.relative_to(root).as_posix(), pattern)
    )


def _check_path(path: Path, pattern: str | None = None) -> str | None:
    """Inspect names and markers only, with a bounded, cycle-safe traversal."""
    if not path.is_dir():
        return _denial(path) if is_protected(path) else None
    pending: list[tuple[Path, frozenset[Path]]] = [(path, frozenset())]
    count = 0
    legacy_root = state_root().resolve()
    while pending:
        if _remaining() <= 0:
            return _narrow()
        directory, ancestors = pending.pop()
        canonical = directory.resolve()
        if canonical in ancestors:
            continue
        next_ancestors = ancestors | {canonical}
        try:
            (directory / MARKER_DIR).stat()
            marked = True
        except FileNotFoundError:
            marked = False
        legacy = canonical.parent == legacy_root
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_SCAN_ENTRIES or _remaining() <= 0:
                    return _narrow()
                child = Path(entry.path)
                if entry.is_dir():
                    pending.append((child, next_ancestors))
                elif (
                    (
                        marked
                        or entry.is_symlink()
                        or (legacy and entry.name in {"response.md", "thinking.md"})
                    )
                    and _matches(child, path, pattern)
                    and is_protected(child)
                ):
                    return _denial(child)
    return None


def _brace_values(value: str) -> list[str]:
    pending = [value]
    result: list[str] = []
    while pending:
        value = pending.pop()
        match = re.search(r"\{([^{}]*,[^{}]*)\}", value)
        if match:
            pending.extend(
                value[: match.start()] + part + value[match.end() :] for part in match[1].split(",")
            )
            if len(pending) + len(result) > 32:
                raise ValueError("too many brace alternatives")
        elif re.search(r"\{[^{}]*\.\.[^{}]*\}", value):
            raise ValueError("use explicit paths instead of brace ranges")
        else:
            result.append(value)
    return result


def _literal_operand(value: str, cwd: Path) -> str | None:
    """Expand literal home paths, brace choices, and filename patterns."""
    literal = _path(value, cwd)
    if literal.is_file() and is_protected(literal):
        return _denial(literal)
    variants = _brace_values(value)
    if variants != [value]:
        for variant in variants:
            reason = _literal_operand(variant, cwd)
            if reason:
                return reason
        return None
    if not any(char in value for char in "*?["):
        return _check_path(_path(value, cwd))
    path = _path(value, cwd)
    # Quoted literal filenames may themselves contain glob characters.
    exact = _check_path(path)
    if exact:
        return exact
    parts = path.parts
    first = next(i for i, part in enumerate(parts) if any(c in part for c in "*?["))
    root = Path(*parts[:first])
    return _check_path(root, str(Path(*parts[first:])))


def _enmaas(args: list[str]) -> str | None:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option, equal, _ = args[index].partition("=")
        matches = [
            flag
            for flag in ("--config", "--key-file")
            if len(option) > 2 and flag.startswith(option)
        ]
        if len(matches) != 1:
            return None
        index += 1 if equal else 2
    if index >= len(args):
        return None
    subcommand = args[index]
    if subcommand == "allow-read":
        return (
            f"{_PREFIX}the human must run enmaas allow-read PATH in their own terminal. "
            "Use enmaas inspect PATH for a bounded preview."
        )
    if subcommand == "ask":
        for option in args[index + 1 :]:
            if option == "--":
                break
            if option.partition("=")[0] in {"--st", "--std", "--stdo", "--stdou", "--stdout"}:
                return (
                    f"{_PREFIX}ask --stdout increases parent context cost. "
                    "Save the answer and use enmaas inspect PATH for a bounded preview."
                )
    return None


def _value_options(name: str) -> set[str]:
    if name in _SEARCHERS:
        return _SEARCH_VALUES | (_RG_VALUES if name == "rg" else _GREP_VALUES)
    return _VALUE_OPTIONS.get(name, set())


def _known_search_option(name: str, option: str) -> bool:
    switches = _RG_SWITCHES if name == "rg" else _GREP_SWITCHES
    return option in _value_options(name) | _SEARCH_SWITCHES | switches


def _option_arity(name: str, option: str) -> int:
    if name == "jq" and option in {"--arg", "--argjson", "--rawfile", "--slurpfile"}:
        return 2
    return int(option in _value_options(name))


def _reader_arguments(name: str, args: list[str]) -> Iterator[tuple[str | None, list[str]]]:
    """Separate known options from operands and stop ambiguous short groups."""
    options = True
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--" and options:
            options = False
        elif options and token.startswith("-") and token != "-":
            if token.startswith("--"):
                option, equal, value = token.partition("=")
                values = [value] if equal else []
                for _ in range(_option_arity(name, option) - len(values)):
                    index += 1
                    if index < len(args):
                        values.append(args[index])
                if name in _SEARCHERS and not _known_search_option(name, option):
                    option = "!unknown"
                yield option, values
            else:
                for position, letter in enumerate(token[1:], start=1):
                    option = "-" + letter
                    if name in _SEARCHERS and not _known_search_option(name, option):
                        yield "!unknown", []
                        break
                    values = []
                    if option in _ATTACHED_VALUE_OPTIONS.get(name, set()):
                        attached = token[position + 1 :]
                        yield option, [attached] if attached else []
                        break
                    if _option_arity(name, option):
                        value = token[position + 1 :]
                        if not value:
                            index += 1
                            value = args[index] if index < len(args) else ""
                        yield option, [value]
                        break
                    yield option, values
        else:
            yield None, [token]
        index += 1


def _program_paths(name: str, programs: list[str]) -> list[str]:
    """Return the literal file names that sed or awk program text opens."""
    paths: list[str] = []
    for program in programs:
        if name == "sed":
            for match in _SED_FILE_COMMAND.finditer(program):
                value = match[1].strip()
                unquoted = value.strip("'\"")
                paths.extend({value, unquoted} - {"", "-", "/dev/stdin"})
        else:
            for pattern in (_AWK_GETLINE_FILE, _AWK_PIPE_GETLINE):
                paths.extend(match[1] for match in pattern.finditer(program) if match[1])
    return paths


def _reader_operands(
    name: str, args: list[str]
) -> tuple[list[str], str | None, bool, list[str], list[str]]:
    """Return file operands, a search glob, metadata-only output, program file names
    and input files.

    Input files are the values of options that name a file the reader opens as
    input, kept apart from the files it searches or prints.
    """
    operands: list[str] = []
    programs: list[str] = []
    inputs: list[str] = []
    input_options = _INPUT_FILE_OPTIONS.get(name, set())
    pattern: str | None = None
    expression_given = False
    metadata = False
    uncertain = False
    data_arguments = False
    expression_reader = name in _SEARCHERS | {"sed", "awk", "gawk", "jq"}
    metadata_options = {
        "--files",
        "--files-with-matches",
        "--files-without-match",
        "--count",
        "--count-matches",
        "-l",
        "-c",
    }
    if name != "rg":
        metadata_options.add("-L")
    for option, values in _reader_arguments(name, args):
        value = values[0] if values else ""
        if option is not None:
            # A ugrep configuration file can change what a search prints.
            uncertain |= (
                option == "!unknown"
                or (name == "rg" and option == "--json")
                or (name in _SEARCHERS and option == "--config")
            )
            if option in input_options:
                # --rawfile and --slurpfile name a variable before the file.
                inputs.extend(values[1:] if option in {"--rawfile", "--slurpfile"} else values)
            if name == "jq":
                if option in {"-f", "--from-file"}:
                    expression_given = True
                elif option in {"--args", "--jsonargs"}:
                    data_arguments = True
            elif expression_reader and (
                option in {"-e", "--regexp", "--expression", "--source", "-f", "--file"}
                or (name in {"awk", "gawk"} and option in {"-E", "--exec"})
            ):
                expression_given = True
                if option in {"-e", "--regexp", "--expression", "--source"} and (
                    name in _PROGRAM_READERS
                ):
                    programs.extend(values)
            if option in {"-g", "--glob", "--include"} and not value.startswith("!"):
                pattern = value if pattern is None else "*"
            if name in _SEARCHERS and option in metadata_options:
                metadata = True
        elif expression_reader and not expression_given:
            expression_given = True
            if name in _PROGRAM_READERS:
                programs.append(value)
        elif not data_arguments:
            operands.append(value)
            if name == "strings" and value.startswith("@"):
                # strings reads more options from the file named after @.
                inputs.append(value[1:])
    return operands, pattern, metadata and not uncertain, _program_paths(name, programs), inputs


def _glob_matches(value: str, pattern: str) -> bool:
    # A globstar can consume zero directories, including at the search root.
    pieces = [fnmatch.translate(part)[4:-3] for part in pattern.split("**/")]
    return re.fullmatch("(?:.*/)?".join(pieces), value) is not None


def _rg_scope(args: list[str], operands: list[str], cwd: Path) -> str | None:
    """Ask ripgrep for filenames only, using the search's traversal options."""
    scope_options = {
        "-g",
        "--glob",
        "--iglob",
        "-t",
        "-T",
        "--type",
        "--type-not",
        "--type-add",
        "--type-clear",
        "--ignore-file",
        "--max-depth",
        "-d",
        "--max-filesize",
        "--hidden",
        "--follow",
        "-L",
        "-u",
        "--unrestricted",
        "--no-ignore",
        "--no-ignore-dot",
        "--no-ignore-exclude",
        "--no-ignore-files",
        "--no-ignore-global",
        "--no-ignore-parent",
        "--no-ignore-vcs",
        "--no-require-git",
        "--one-file-system",
        "--glob-case-insensitive",
        "--ignore-file-case-insensitive",
    }
    command = ["rg", "--no-config", "--files", "--null"]
    for option, values in _reader_arguments("rg", args):
        if option is not None and option in scope_options:
            command.extend([option, *[v.replace(_QUOTED_TILDE, "~") for v in values]])
    expanded: list[str] = []
    for value in operands:
        if value == "-":
            continue
        for candidate in _brace_values(value):
            path = _path(candidate, cwd)
            if path.exists() or not glob.has_magic(str(path)):
                expanded.append(str(path))
                continue
            # A recursive pattern over a large tree can take as long as a scan.
            for match in glob.iglob(str(path), recursive=True):
                if _remaining() <= 0:
                    return _narrow()
                expanded.append(match)
    if not expanded:
        return None
    command.extend(["--", *expanded])
    remaining = _remaining()
    if remaining <= 0:
        return _narrow()
    try:
        # Listing a large tree can take a minute. Stop it at the deadline.
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, check=False, timeout=remaining
        )
    except subprocess.TimeoutExpired:
        return _narrow()
    except FileNotFoundError:
        for operand in operands:
            reason = _literal_operand(operand, cwd)
            if reason:
                return reason
        return None
    if result.returncode not in {0, 1}:
        return f"{_PREFIX}could not list the search scope. Use explicit file paths."
    for index, raw in enumerate(result.stdout.split(b"\0")):
        if index > _MAX_SCAN_ENTRIES or _remaining() <= 0:
            return _narrow()
        if raw:
            path = _path(os.fsdecode(raw), cwd)
            if is_protected(path):
                return _denial(path)
    return None


def _unwrap(tokens: list[str]) -> list[str]:
    """Expose commands behind common shell keywords and execution wrappers."""
    while tokens:
        if re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[0]):
            tokens = tokens[1:]
            continue
        name = Path(_shell_value(tokens[0])).name
        if name == "env":
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                option = tokens.pop(0)
                if option in {"-u", "--unset"} and tokens:
                    tokens.pop(0)
                elif option not in {"-i", "--ignore-environment", "--"}:
                    raise ValueError("unsupported env wrapper option")
            continue
        if name in {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}"}:
            tokens = tokens[1:]
            continue
        if name not in {"command", "builtin", "time", "sudo", "xargs"}:
            return tokens
        tokens = tokens[1:]
        if name == "command" and any(flag in tokens for flag in ("-v", "-V")):
            return []
        value_options = {
            "sudo": {"-u", "-g", "-h", "-p", "-C", "--user", "--group", "--host"},
            "xargs": {"-I", "-n", "-P", "-L", "-d", "-s", "-E"},
            "time": {"-o", "-f", "--output", "--format"},
        }.get(name, set())
        while tokens and tokens[0].startswith("-"):
            option = tokens.pop(0)
            if option == "--":
                break
            if option in value_options and tokens:
                tokens.pop(0)
            elif option.partition("=")[0] in value_options and "=" in option:
                continue
            elif option not in {
                "-p",
                "-v",
                "-V",
                "-l",
                "-n",
                "-b",
                "-E",
                "-H",
                "-i",
                "-s",
                "-k",
                "-K",
                "-S",
                "-0",
                "--null",
                "-r",
                "--no-run-if-empty",
                "-t",
                "--verbose",
                "--preserve-env",
                "--non-interactive",
            }:
                raise ValueError("unsupported execution wrapper option")
        if name == "xargs":
            # Its operands come from stdin, so checking only its command line
            # cannot establish which files a reader will open.
            tokens = _unwrap(tokens)
            if tokens and Path(tokens[0]).name in _READERS | {"enmaas"}:
                raise ValueError("xargs supplies dynamic file operands; use explicit paths")
    return tokens


def _substitutions(command: str, *, literal: bool = False) -> tuple[str, list[str]]:
    """Extract executable substitutions without treating quoted prose as code."""
    result: list[str] = []
    nested: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            result.append(command[index : index + 2])
            index += 2
            continue
        if not literal and char in {"'", '"'} and (quote is None or quote == char):
            quote = None if quote else char
        if (
            not literal
            and quote is None
            and char == "#"
            and (index == 0 or command[index - 1] in " \t\n;&|()")
        ):
            end = command.find("\n", index)
            end = len(command) if end < 0 else end
            result.append(command[index:end])
            index = end
            continue
        if quote != "'" and (
            char == "`"
            or command.startswith("$(", index)
            or (quote is None and command[index : index + 2] in {"<(", ">("})
        ):
            start = index + (1 if char == "`" else 2)
            end = start
            depth = 1
            inner_quote: str | None = None
            while end < len(command):
                current = command[end]
                if current == "\\":
                    end += 2
                    continue
                if char == "`":
                    if current == "`":
                        break
                elif current in {"'", '"'} and (inner_quote is None or inner_quote == current):
                    inner_quote = None if inner_quote else current
                elif inner_quote is None:
                    if current == "(":
                        depth += 1
                    elif current == ")":
                        depth -= 1
                        if depth == 0:
                            break
                end += 1
            if end >= len(command):
                raise ValueError("unfinished command substitution")
            nested.append(command[start:end])
            result.append("enmaas-substitution")
            index = end + 1
            continue
        result.append(char)
        index += 1
    return "".join(result), nested


def _heredocs(command: str) -> tuple[str, list[str]]:
    """Remove heredoc data while retaining executable substitutions."""
    lines = iter(command.splitlines(keepends=True))
    result: list[str] = []
    embedded: list[str] = []
    delimiter = _HEREDOC_START
    quote: str | None = None
    for line in lines:
        pending: list[tuple[str, bool, bool]] = []
        index = 0
        header: list[str] = []
        while index < len(line):
            char = line[index]
            if char == "\\" and quote != "'":
                header.append(line[index : index + 2])
                index += 2
                continue
            if char in {"'", '"'} and (quote is None or quote == char):
                quote = None if quote else char
            if quote is None and char == "#" and (index == 0 or line[index - 1] in " \t;&|()"):
                header.append(line[index:])
                break
            if quote is None and line.startswith("<<<", index):
                header.append("<<<")
                index += 3
                continue
            match = delimiter.match(line, index) if quote is None else None
            if match:
                pending.append(
                    (match[2] or match[3] or match[4], bool(match[1]), bool(match[2] or match[3]))
                )
                header.append(" < /dev/null ")
                index = match.end()
            else:
                header.append(char)
                index += 1
        result.append("".join(header))
        for end, strip_tabs, quoted in pending:
            body: list[str] = []
            for line in lines:
                candidate = line.lstrip("\t") if strip_tabs else line
                if candidate.rstrip("\r\n") == end:
                    break
                body.append(candidate)
            else:
                raise ValueError("unfinished heredoc")
            if not quoted:
                # Quotes in an unquoted heredoc are text. Only substitutions run.
                _, substitutions = _substitutions("".join(body), literal=True)
                embedded.extend(substitutions)
    return "".join(result), embedded


def _segment(tokens: list[str], cwd: Path, stdin: bool = False) -> tuple[str | None, Path]:
    if not tokens:
        return None, cwd
    words = tokens
    cleaned: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"<", ">", ">>", "<<<"}:
            if index + 1 >= len(tokens):
                return f"{_PREFIX}incomplete shell redirection.", cwd
            if token in {"<", "<<<"}:
                stdin = True
            if token == "<":
                reason = _literal_operand(_shell_value(tokens[index + 1]), cwd)
                if reason:
                    return reason, cwd
            index += 2
        else:
            cleaned.append(token)
            index += 1
    tokens = _unwrap(cleaned)
    index = 0
    while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[index]):
        index += 1
    if index < len(tokens) and Path(tokens[index]).name == "env":
        index += 1
        while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[index]):
            index += 1
    if index >= len(tokens):
        return None, cwd
    name = Path(_shell_value(tokens[index])).name
    if _UNRESOLVED in name or (name in _READERS and any(_UNRESOLVED in word for word in words)):
        # The guard cannot tell which program runs, or which file a reader opens.
        raise ValueError("dollar quoting the guard cannot read")
    args = [_shell_value(arg) for arg in tokens[index + 1 :]]
    if name == "cd" and len(args) == 1 and not any(c in args[0] for c in "$`*?["):
        return None, _path(args[0], cwd)
    if name == "enmaas":
        return _enmaas(args), cwd
    if name not in _READERS:
        return None, cwd
    operands, pattern, metadata, program_paths, inputs = _reader_operands(name, args)
    for value in program_paths:
        # sed and awk open these names as files. A directory yields nothing.
        path = _path(value, cwd)
        if not path.is_dir() and is_protected(path):
            return _denial(path), cwd
    for value in inputs:
        if value in {"", "-", "/dev/stdin"}:
            continue
        reason = _literal_operand(value, cwd)
        if reason:
            return reason, cwd
    if metadata:
        return None, cwd
    if name in _SEARCHERS and not operands:
        recursive = name == "rg" or any(
            flag in {"-r", "-R", "--recursive", "--dereference-recursive"}
            for flag, _ in _reader_arguments(name, args)
        )
        if recursive and not stdin:
            operands = ["."]
    if name == "rg" and operands and "RIPGREP_CONFIG_PATH" not in os.environ:
        return _rg_scope(args, operands, cwd), cwd
    for operand in operands:
        if operand == "-":
            continue
        path = _path(operand, cwd)
        reason = _check_path(path, pattern) if path.is_dir() else _literal_operand(operand, cwd)
        if reason:
            return reason, cwd
    return None, cwd


def _ansi_c(body: str) -> str | None:
    """Return the text Bash makes of $'body', or None when the guard cannot know it.

    Bash cuts the string at a NUL, and escapes outside the supported list follow
    rules the guard does not copy, so both give None. A byte that is not UTF-8
    becomes the character Python uses for that byte in a file name.
    """
    data = bytearray()
    index = 0
    try:
        while index < len(body):
            end = body.find("\\", index)
            end = len(body) if end < 0 else end
            data += body[index:end].encode("utf-8", "surrogateescape")
            if end == len(body):
                break
            index = end + 1
            if index >= len(body):
                return None
            escape = body[index]
            if escape in _ANSI_C_ESCAPES:
                data += _ANSI_C_ESCAPES[escape].encode("ascii")
                index += 1
                continue
            octal = _ANSI_C_OCTAL.match(body, index)
            if octal:
                # Bash keeps the low eight bits of an octal value above 0o377.
                data.append(int(octal[0], 8) & 0xFF)
                index = octal.end()
                continue
            pattern = _ANSI_C_HEX.get(escape)
            digits = pattern.match(body, index + 1) if pattern else None
            if digits is None:
                return None
            value = int(digits[0], 16)
            index = digits.end()
            if escape == "x":
                data.append(value)
            elif value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                return None
            else:
                data += chr(value).encode("utf-8")
    except UnicodeEncodeError:
        return None
    if 0 in data:
        return None
    return os.fsdecode(bytes(data))


def _plain_quoted(text: str | None) -> str:
    """Write text in single quotes, with each quote and backslash escaped outside them.

    Every later step reads that form already, so a decoded string needs no new
    rule anywhere else.
    """
    if text is None:
        return f"'{_UNRESOLVED}'"
    if not text:
        return "''"
    return "".join(
        "\\" + part if part in {"'", "\\"} else f"'{part}'"
        for part in re.split(r"(['\\])", text)
        if part
    )


def _dollar_quotes(command: str) -> str:
    """Rewrite each $'...' as the text Bash makes of it, and each $"..." as "..."."""
    return _requote(command, 0, "")[0]


def _requote(command: str, index: int, closer: str) -> tuple[str, int]:
    """Copy command from index up to an unquoted closer, rewriting dollar quotes.

    closer is ")" inside $( ), <( ) and >( ), a backquote inside backquotes, and
    empty for the whole command. Quotes, comments and heredoc bodies are copied
    as they are, because Bash does not decode $'...' inside them. Returns the
    copy and the index of the closer.
    """
    out: list[str] = []
    quote = ""
    depth = 0
    word = False
    heredocs: list[tuple[str, bool]] = []
    while index < len(command):
        if quote == "'":
            end = command.find("'", index)
            end = len(command) if end < 0 else end + 1
            out.append(command[index:end])
            quote = "" if command[end - 1] == "'" else quote
            index = end
            continue
        # Copy text that holds nothing this pass acts on in one step.
        plain = (_QUOTED_PLAIN if quote else _UNQUOTED_PLAIN).match(command, index)
        if plain:
            text = plain[0]
            out.append(text)
            index = plain.end()
            word = bool(quote) or not (text[-1].isspace() or text[-1] in ";&|")
            continue
        char = command[index]
        if char == "\\" or command.startswith("$$", index):
            out.append(command[index : index + 2])
            index += 2
            word = True
            continue
        if closer == "`" and char == "`":
            return "".join(out), index
        opener = ""
        if command.startswith("$(", index):
            opener, end_char = "$(", ")"
        elif char == "`":
            opener, end_char = "`", "`"
        elif not quote and command[index : index + 2] in {"<(", ">("}:
            opener, end_char = command[index : index + 2], ")"
        if opener:
            inner, end = _requote(command, index + len(opener), end_char)
            out.append(opener + inner + command[end : end + 1])
            index = end + 1
            word = True
            continue
        if quote == '"':
            out.append(char)
            quote = "" if char == '"' else quote
            index += 1
            continue
        if closer == ")" and char in "()":
            if char == ")" and depth == 0:
                return "".join(out), index
            depth += 1 if char == "(" else -1
        if char == "#" and not word:
            end = command.find("\n", index)
            end = len(command) if end < 0 else end
            out.append(command[index:end])
            index = end
            continue
        if command.startswith("$'", index):
            end = index + 2
            while end < len(command) and command[end] != "'":
                end += 2 if command[end] == "\\" else 1
            if end >= len(command):
                # Bash refuses an unfinished string. Later steps refuse it too.
                out.append(command[index:])
                index = len(command)
                break
            out.append(_plain_quoted(_ansi_c(command[index + 2 : end])))
            index = end + 1
            word = True
            continue
        if command.startswith('$"', index):
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            out.append(char)
            index += 1
            word = True
            continue
        if command.startswith("<<<", index):
            out.append("<<<")
            index += 3
            word = False
            continue
        heredoc = _HEREDOC_START.match(command, index) if char == "<" else None
        if heredoc:
            heredocs.append((heredoc[2] or heredoc[3] or heredoc[4], bool(heredoc[1])))
            out.append(heredoc[0])
            index = heredoc.end()
            word = True
            continue
        out.append(char)
        index += 1
        word = not (char.isspace() or char in ";&|()<>")
        if char == "\n" and heredocs:
            # Bash reads each body line by line up to its delimiter.
            for delimiter, strip_tabs in heredocs:
                while index < len(command):
                    end = command.find("\n", index)
                    end = len(command) if end < 0 else end + 1
                    line = command[index:end]
                    out.append(line)
                    index = end
                    candidate = line.lstrip("\t") if strip_tabs else line
                    if candidate.rstrip("\r\n") == delimiter:
                        break
            heredocs = []
    return "".join(out), index


def _shell_syntax(command: str) -> str:
    """Preserve quoted punctuation, comments, and literal line continuations."""
    result: list[str] = []
    quote: str | None = None
    word = False
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
                result.append(char)
            else:
                result.append(char.translate(_QUOTED_PUNCTUATION))
        elif char == "\\" and index + 1 < len(command):
            following = command[index + 1]
            if following != "\n":
                result.extend((char, following.translate(_QUOTED_PUNCTUATION)))
                word = True
            index += 1
        elif quote == '"':
            if char == '"':
                quote = None
                result.append(char)
            else:
                result.append(char.translate(_QUOTED_PUNCTUATION))
        elif char in {"'", '"'}:
            quote = char
            word = True
            result.append(char)
        elif char == "#" and not word:
            newline = command.find("\n", index)
            index = len(command) if newline == -1 else newline
            continue
        else:
            result.append(char)
            word = not (char.isspace() or char in ";&|()<>")
        index += 1
    return "".join(result)


def _shell(command: str, cwd: Path) -> str | None:
    command, embedded = _heredocs(_dollar_quotes(command))
    command, substitutions = _substitutions(command)
    lexer = shlex.shlex(_shell_syntax(command), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.whitespace_split = True
    directories = {cwd.resolve()}
    segment: list[str] = []
    stdin = False
    # Retaining prior directories also covers failed cd, pipes, and subshells.
    for token in [*lexer, ";"]:
        if token and all(c in ";&|()\n" for c in token):
            for candidate in list(directories):
                if _remaining() <= 0:
                    return _narrow()
                reason, next_cwd = _segment(segment, candidate, stdin)
                if reason:
                    return reason
                directories.add(next_cwd.resolve())
                if len(directories) > _MAX_SHELL_DIRS:
                    return (
                        f"{_PREFIX}too many possible shell working directories. "
                        "Use a shorter command with a literal file path."
                    )
            segment = []
            stdin = token.endswith("|") and not token.endswith("||")
        else:
            segment.append(token)
    for nested in embedded + substitutions:
        for candidate in directories:
            if _remaining() <= 0:
                return _narrow()
            reason = _shell(nested, candidate)
            if reason:
                return reason
    return None


def check_tool(event: dict[str, object]) -> str | None:
    """Return a denial reason, or leave the host's normal decision unchanged."""
    global _deadline
    _deadline = time.monotonic() + _time_budget()
    try:
        return _check_tool(event)
    finally:
        _deadline = math.inf


def _check_tool(event: dict[str, object]) -> str | None:
    name = event.get("tool_name")
    if not isinstance(name, str) or name not in {"Read", "Grep"} | _SHELL_TOOLS:
        return None
    inputs = event.get("tool_input")
    cwd_value = event.get("cwd", os.getcwd())
    if not isinstance(inputs, dict) or not isinstance(cwd_value, str) or not cwd_value:
        return f"{_PREFIX}malformed input for supported tool {name}."
    cwd = Path(cwd_value)
    try:
        if name in _SHELL_TOOLS:
            command = inputs.get("command", inputs.get("cmd"))
            if not isinstance(command, str) or not command:
                return f"{_PREFIX}shell input requires a command string."
            workdir = inputs.get("workdir", cwd_value)
            if not isinstance(workdir, str) or not workdir:
                return f"{_PREFIX}shell workdir requires a directory string."
            return _shell(command, _path(workdir, cwd))
        value = inputs.get("file_path", inputs.get("path"))
        if name == "Grep":
            if value is None:
                value = "."
            if not isinstance(value, str) or not value:
                return f"{_PREFIX}Grep requires a path string."
            search_pattern = inputs.get("pattern")
            if search_pattern is not None and not isinstance(search_pattern, str):
                return f"{_PREFIX}Grep requires a pattern string."
            mode = inputs.get("output_mode", "files_with_matches")
            pattern = inputs.get("glob")
            if not isinstance(mode, str) or (pattern is not None and not isinstance(pattern, str)):
                return f"{_PREFIX}malformed content search options."
            if mode in {"files_with_matches", "count"}:
                return None
            if mode != "content":
                return f"{_PREFIX}unknown search output mode."
        else:
            pattern = None
        if not isinstance(value, str) or not value:
            return f"{_PREFIX}{name} requires a file path string."
        return _check_path(_path(value, cwd), pattern)
    except (OSError, ValueError, RuntimeError) as exc:
        return (
            f"{_PREFIX}could not check this {name} input ({type(exc).__name__}). "
            "Use a narrow literal path and verify local file access."
        )
