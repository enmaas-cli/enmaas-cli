"""Containment decisions must avoid reading response bodies."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from enmaas_cli.containment import check_tool
from enmaas_cli.outputs import allow_read, protect


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


def test_read_exact_relative_alias_and_no_answer_reads(files: tuple[Path, Path]) -> None:
    output, source = files
    alias = source.parent / "alias.md"
    alias.symlink_to(output)
    with patch.object(Path, "read_text", side_effect=AssertionError("answer read")):
        for name in (output, alias):
            reason = check_tool(event("Read", source.parent, file_path=str(name)))
            assert reason and reason.startswith("enmaas containment:")
            assert "enmaas inspect" in reason
            assert "context" in reason
            assert "PRIVATE RESPONSE BODY" not in reason
        assert check_tool(event("Read", source.parent, path="source.py")) is None
        assert check_tool(event("Read", output.parent, path=output.name))
    allow_read(output)
    assert check_tool(event("Read", source.parent, path=str(alias))) is None


@pytest.mark.parametrize("name", ["usage.jsonl", "results.jsonl", "ordinary.md"])
def test_neighbors_are_readable(files: tuple[Path, Path], name: str) -> None:
    output, _ = files
    (output.parent / name).touch()
    assert check_tool(event("Read", output.parent, file_path=name)) is None


def test_grep_modes_globs_and_hidden_directories(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Grep", source.parent, pattern=".", output_mode="content"))
    assert check_tool(event("Grep", source.parent, pattern=".", glob="*.md"))
    assert check_tool(event("Grep", source.parent, pattern=".", glob="*.py")) is None
    for mode in ("files_with_matches", "count"):
        assert check_tool(event("Grep", source.parent, path=str(output), output_mode=mode)) is None
    hidden = source.parent / ".hidden"
    hidden.mkdir()
    secret = hidden / "secret.txt"
    secret.write_text("secret")
    protect(secret)
    assert check_tool(event("Grep", source.parent, pattern=".", glob="*.txt"))


def test_directory_symlink_cycle_is_bounded(files: tuple[Path, Path]) -> None:
    _, source = files
    (source.parent / "cycle").symlink_to(source.parent, target_is_directory=True)
    assert check_tool(event("Grep", source.parent, glob="*.py")) is None
    assert check_tool(event("Grep", source.parent, glob="*.md"))


def test_scan_limit_is_actionable(files: tuple[Path, Path]) -> None:
    _, source = files
    with patch("enmaas_cli.containment._MAX_SCAN_ENTRIES", 0):
        reason = check_tool(event("Grep", source.parent, pattern="."))
    assert reason and "narrow" in reason.lower()


@pytest.mark.parametrize("tool", ["Bash", "exec_command", "shell_command", "shell"])
def test_host_shell_aliases(files: tuple[Path, Path], tool: str) -> None:
    output, source = files
    assert check_tool(event(tool, source.parent, command=f"cat '{output}'"))


@pytest.mark.parametrize(
    "reader",
    [
        "cat",
        "head -n 3",
        "tail -10",
        "sed -n '1p'",
        "awk '{print}'",
        "grep x",
        "rg x",
        "bat",
        "less",
        "more",
        "jq .",
        "sort",
        "cut -c 1",
        "diff source.py",
    ],
)
def test_common_shell_readers(files: tuple[Path, Path], reader: str) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"{reader} '{output}'"))


def test_shell_segments_cd_globs_and_default_search(files: tuple[Path, Path]) -> None:
    output, source = files
    for command in (
        f"cd nested && cat '{output.name}'",
        "cat nested/*.md",
        "rg secret",
        "grep -R secret nested",
        f"enmaas inspect '{output}'; cat '{output}'",
        f"enmaas inspect '{output}'\ncat '{output}'",
        f"enmaas inspect '{output}' | cat '{output}'",
    ):
        assert check_tool(event("Bash", source.parent, command=command)), command


@pytest.mark.parametrize(
    "command",
    [
        "ls nested",
        "stat nested",
        "wc nested/*.md",
        "cat source.py",
        "rg print source.py",
        "grep -rl x nested",
        "rg -l x nested",
        "rg --count x nested",
        "echo cat nested/*.md",
        "enmaas ask 'inspect allow-read --stdout'",
        "enmaas map jobs.jsonl",
        "enmaas usage",
        "enmaas record --response-file nested/answer.md",
    ],
)
def test_safe_shell_actions(files: tuple[Path, Path], command: str) -> None:
    _, source = files
    assert check_tool(event("Bash", source.parent, command=command)) is None


def test_enmaas_commands(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"enmaas inspect '{output}'")) is None
    for prefix in (
        "enmaas",
        "/usr/bin/enmaas",
        "X=y enmaas",
        "env X=y enmaas",
        "enmaas --config x --key-file y",
    ):
        assert check_tool(event("Bash", source.parent, command=f"{prefix} allow-read '{output}'"))
        for option in ("--stdout", "--std", "--stdo", "--stdou"):
            assert check_tool(event("Bash", source.parent, command=f"{prefix} ask {option} hello"))
        assert (
            check_tool(event("Bash", source.parent, command=f"{prefix} ask --save-thinking hello"))
            is None
        )


@pytest.mark.parametrize("inputs", [{}, {"file_path": 4}, {"file_path": ""}])
def test_malformed_read_is_denied(tmp_path: Path, inputs: dict[str, object]) -> None:
    assert check_tool(event("Read", tmp_path, **inputs))
    assert check_tool(event("unknown", tmp_path, **inputs)) is None


@pytest.mark.parametrize(
    "name,inputs", [("Bash", {}), ("Grep", {"path": 1}), ("Read", {"path": "x"})]
)
def test_malformed_supported_events(tmp_path: Path, name: str, inputs: dict[str, object]) -> None:
    value = event(name, tmp_path, **inputs)
    value["cwd"] = 7
    assert check_tool(value)
    assert check_tool({"tool_name": name, "tool_input": "bad"})


def test_wrapper_uses_only_bundled_standard_library(files: tuple[Path, Path]) -> None:
    output, source = files
    script = Path(__file__).resolve().parents[1] / "hooks" / "pre-tool-use.py"
    for payload, denied in (
        (json.dumps(event("Read", source.parent, file_path=str(output))), True),
        (json.dumps(event("Read", source.parent, file_path=str(source))), False),
        ("{invalid", True),
        ("[]", True),
        ("x" * (1024 * 1024 + 1), True),
    ):
        result = subprocess.run(
            [sys.executable, "-S", str(script)],
            input=payload,
            text=True,
            capture_output=True,
            cwd="/tmp",
            check=True,
        )
        if denied:
            decision = json.loads(result.stdout)["hookSpecificOutput"]
            assert decision["hookEventName"] == "PreToolUse"
            assert decision["permissionDecision"] == "deny"
            assert decision["permissionDecisionReason"].startswith("enmaas containment:")
        else:
            assert result.stdout == ""


def test_search_flags_do_not_hide_file_operands(files: tuple[Path, Path]) -> None:
    output, source = files
    for flags in ("-w x", "-s x", "-n x", "-F x", "-e '-cat'", "-e-l", "-g '*.md' x"):
        assert check_tool(event("Bash", source.parent, command=f"rg {flags} '{output}'")), flags
    assert check_tool(event("Bash", source.parent, command="rg -w print source.py")) is None


def test_outputs_inside_marker_named_directory_are_checked(tmp_path: Path) -> None:
    output = tmp_path / ".enmaas-output" / "answer.txt"
    output.parent.mkdir()
    output.touch()
    protect(output)
    assert check_tool(event("Grep", tmp_path, pattern="x"))


def test_malformed_marker_is_protected(files: tuple[Path, Path]) -> None:
    from enmaas_cli.outputs import marker_path

    output, source = files
    marker_path(output).write_text("broken", encoding="utf-8")
    assert check_tool(event("Read", source.parent, file_path=str(output)))


def test_scan_error_has_diagnostic(files: tuple[Path, Path]) -> None:
    _, source = files
    with patch("enmaas_cli.containment.os.scandir", side_effect=PermissionError):
        reason = check_tool(event("Grep", source.parent, pattern="x"))
    assert reason and "PermissionError" in reason


def test_search_glob_combinations_are_conservative(files: tuple[Path, Path]) -> None:
    _, source = files
    assert check_tool(event("Grep", source.parent, glob="*.{md,py}"))
    assert check_tool(event("Bash", source.parent, command="rg -g '*.md' -g '*.py' x ."))


def test_wrapper_path_with_spaces(tmp_path: Path, files: tuple[Path, Path]) -> None:
    import shutil

    output, source = files
    plugin = Path(__file__).resolve().parents[1]
    copied = tmp_path / "plugin with spaces"
    for relative in (
        "hooks/pre-tool-use.py",
        "src/enmaas_cli/__init__.py",
        "src/enmaas_cli/outputs.py",
        "src/enmaas_cli/containment.py",
    ):
        target = copied / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plugin / relative, target)
    result = subprocess.run(
        [sys.executable, "-S", str(copied / "hooks/pre-tool-use.py")],
        input=json.dumps(event("Read", source.parent, path=str(output))),
        text=True,
        capture_output=True,
        cwd="/tmp",
        check=True,
    )
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_markers_are_the_only_files_opened(files: tuple[Path, Path]) -> None:
    import os

    output, source = files
    original_open = os.open

    def guarded_open(path: str | Path, flags: int) -> int:
        assert Path(path).resolve() != output.resolve()
        return original_open(path, flags)

    with patch("enmaas_cli.outputs.os.open", side_effect=guarded_open):
        assert check_tool(event("Read", source.parent, path=str(output)))
        assert check_tool(event("Grep", source.parent, pattern="x"))


@pytest.mark.parametrize("filename", ["semi;colon.md", "pipe|name.md", "line\nname.md", ">", ";"])
def test_quoted_punctuation_is_part_of_a_path(tmp_path: Path, filename: str) -> None:
    import shlex

    output = tmp_path / filename
    output.touch()
    protect(output)
    assert check_tool(event("Bash", tmp_path, command=f"cat {shlex.quote(filename)}"))


def test_quoted_separator_does_not_hide_later_operand(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"cat ';' '{output}'"))


def test_glob_named_literal_output_is_protected(tmp_path: Path) -> None:
    output = tmp_path / "answer[1].md"
    output.touch()
    protect(output)
    assert check_tool(event("Bash", tmp_path, command="cat 'answer[1].md'"))


def test_directory_alias_is_checked_for_path_globs(files: tuple[Path, Path]) -> None:
    output, source = files
    (source.parent / "alias").symlink_to(output.parent, target_is_directory=True)
    assert check_tool(event("Grep", source.parent, glob="alias/*.md"))
    assert check_tool(event("Grep", source.parent, glob="nested/*.md"))


def test_rg_follow_symlinks_still_returns_content(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"rg -L x '{output}'"))
    assert check_tool(event("Bash", source.parent, command=f"grep -L x '{output}'")) is None


def test_comment_does_not_hide_later_command(files: tuple[Path, Path]) -> None:
    output, source = files
    command = f"enmaas inspect '{output}' # note\ncat '{output}'"
    assert check_tool(event("Bash", source.parent, command=command))


def test_quoted_separator_does_not_hide_stdout_option(tmp_path: Path) -> None:
    assert check_tool(event("Bash", tmp_path, command="enmaas ask -p ';' --stdout"))


def test_hash_in_literal_filename_is_not_a_comment(tmp_path: Path) -> None:
    output = tmp_path / "answer#1.md"
    output.touch()
    protect(output)
    assert check_tool(event("Bash", tmp_path, command="cat answer#1.md"))
    assert check_tool(event("Bash", tmp_path, command="cat 'answer#1.md'"))


def test_comment_after_separator_keeps_following_newline(files: tuple[Path, Path]) -> None:
    output, source = files
    command = f"enmaas inspect '{output}';# unclosed ' quote\ncat '{output}'"
    assert check_tool(event("Bash", source.parent, command=command))


@pytest.mark.parametrize(
    "global_option",
    [flag[:length] for flag in ("--config", "--key-file") for length in range(3, len(flag) + 1)],
)
@pytest.mark.parametrize("equals_form", [False, True])
@pytest.mark.parametrize("subcommand", ["allow-read", "ask"])
def test_global_option_abbreviations_match_cli_parser(
    files: tuple[Path, Path], global_option: str, equals_form: bool, subcommand: str
) -> None:
    import shlex

    from enmaas_cli.cli import build_parser

    output, source = files
    args = [f"{global_option}=cfg"] if equals_form else [global_option, "cfg"]
    args += ["ask", "--stdout", "-p", "hi"] if subcommand == "ask" else ["allow-read", str(output)]
    if global_option not in {"--config", "--key-file"}:
        with pytest.raises(SystemExit):
            build_parser().parse_args(args)
        return
    parsed = build_parser().parse_args(args)
    assert parsed.command == subcommand
    assert parsed.config == Path("cfg") or parsed.key_file == Path("cfg")
    assert check_tool(event("Bash", source.parent, command=shlex.join(["enmaas", *args])))


def test_shell_uses_tool_workdir(files: tuple[Path, Path]) -> None:
    output, source = files
    value = event(
        "exec_command", source.parent, cmd=f"cat '{output.name}'", workdir=str(output.parent)
    )
    assert check_tool(value)


@pytest.mark.parametrize("reader", ["rg", "grep"])
def test_attached_expression_is_not_metadata(files: tuple[Path, Path], reader: str) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"{reader} -iehello '{output}'"))


def test_leading_input_redirection_is_checked(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"< '{output}' cat"))


@pytest.mark.parametrize(
    "prefix", ["(cd nested && pwd);", "cd nested |", "cd missing-directory;", "cd nested ||"]
)
def test_shell_scope_keeps_original_directory(files: tuple[Path, Path], prefix: str) -> None:
    output, source = files
    command = f"{prefix} cat 'nested/{output.name}'"
    assert check_tool(event("Bash", source.parent, command=command))


def test_line_continuation_joins_literal_path(files: tuple[Path, Path]) -> None:
    output, source = files
    command = "cat nested/\\\n'" + output.name + "'"
    assert check_tool(event("Bash", source.parent, command=command))


def test_double_quoted_line_continuation_is_removed(files: tuple[Path, Path]) -> None:
    output, source = files
    command = 'cat "nested/\\\n' + output.name + '"'
    assert check_tool(event("Bash", source.parent, command=command))


def test_single_quoted_line_continuation_is_literal(tmp_path: Path) -> None:
    import shlex

    output = tmp_path / "answer\\\n.md"
    output.touch()
    protect(output)
    assert check_tool(event("Bash", tmp_path, command=f"cat {shlex.quote(output.name)}"))


def test_possible_directory_limit_is_actionable(tmp_path: Path) -> None:
    with patch("enmaas_cli.containment._MAX_SHELL_DIRS", 2):
        reason = check_tool(event("Bash", tmp_path, command="cd a; cd b; cat source.py"))
    assert reason and "shorter command" in reason


def test_invalid_shell_workdir_is_denied(tmp_path: Path) -> None:
    assert check_tool(event("exec_command", tmp_path, cmd="cat source.py", workdir=7))


def test_relative_shell_workdir_is_resolved(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(
        event("exec_command", source.parent, cmd=f"cat '{output.name}'", workdir="nested")
    )


@pytest.mark.parametrize("reader", ["rg", "grep"])
def test_combined_flags_allow_ordinary_source(files: tuple[Path, Path], reader: str) -> None:
    _, source = files
    assert check_tool(event("Bash", source.parent, command=f"{reader} -iehello source.py")) is None


@pytest.mark.parametrize("options", ["-Elatin1", "-rlol", "--replace lol", "-Qlol"])
def test_rg_option_values_cannot_establish_metadata(files: tuple[Path, Path], options: str) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"rg {options} hello '{output}'"))


def test_unknown_grep_group_cannot_establish_metadata(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"grep -Qlol hello '{output}'"))


@pytest.mark.parametrize(
    "options",
    ["-e .", "-f filter.jq", "--from-file filter.jq", "--arg name value .", "--argjson name 1 ."],
)
def test_jq_options_keep_ordinary_files_readable(files: tuple[Path, Path], options: str) -> None:
    _, source = files
    (source.parent / "source.json").write_text("{}", encoding="utf-8")
    (source.parent / "filter.jq").write_text(".", encoding="utf-8")
    assert check_tool(event("Bash", source.parent, command=f"jq {options} source.json")) is None


@pytest.mark.parametrize("reader", ["rg", "grep"])
def test_unknown_options_decline_other_metadata_exemptions(
    files: tuple[Path, Path], reader: str
) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"{reader} -Q -l hello '{output}'"))


@pytest.mark.parametrize("option", ["--rawfile", "--slurpfile"])
def test_jq_file_arguments_are_checked(files: tuple[Path, Path], option: str) -> None:
    output, source = files
    assert check_tool(
        event("Bash", source.parent, command=f"jq {option} name '{output}' . source.json")
    )


def test_jq_filter_file_is_checked(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"jq -f '{output}' source.json"))


def test_jq_positional_data_is_not_read_as_files(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"jq --args . '{output}'")) is None


@pytest.mark.parametrize("metadata", ["-l", "-c", "--files-with-matches", "--count"])
@pytest.mark.parametrize("json_first", [False, True])
def test_rg_json_output_declines_metadata_exemption(
    files: tuple[Path, Path], metadata: str, json_first: bool
) -> None:
    output, source = files
    flags = f"--json {metadata}" if json_first else f"{metadata} --json"
    assert check_tool(event("Bash", source.parent, command=f"rg {flags} hello '{output}'"))


def test_json_pattern_value_does_not_disable_metadata(files: tuple[Path, Path]) -> None:
    output, source = files
    assert check_tool(event("Bash", source.parent, command=f"rg -l -e '--json' '{output}'")) is None
