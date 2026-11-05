"""User-reported read denials and output leaks."""

import json
from pathlib import Path

import pytest

from enmaas_cli.cli import build_parser
from enmaas_cli.containment import check_tool
from enmaas_cli.outputs import protect
from enmaas_cli.schema import ValidationError, validate_against_schema


def shell(root: Path, command: str) -> str | None:
    return check_tool({"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}})


@pytest.fixture
def answers(tmp_path: Path) -> Path:
    (tmp_path / "answer.md").write_text("PRIVATE ANSWER")
    protect(tmp_path / "answer.md")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "git log | grep fix",
        "git log | rg fix",
        "grep fix",
        "cat > n.md <<'EOF'\nWe don't need this\nEOF\n",
        "cat > n.md <<EOF\nWe don't need this\nEOF\n",
        "rg fix <<'EOF'\nWe don't need this\nEOF\n",
        "echo 'literal `cat answer.md`'",
    ],
)
def test_safe_input_does_not_scan_directory(answers: Path, command: str) -> None:
    assert shell(answers, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "git log | rg fix answer.md",
        "cat answer.md | grep fix",
        "if true; then cat answer.md; fi",
        "{ cat answer.md; }",
        "! cat answer.md",
        "time cat answer.md",
        "command cat answer.md",
        "sudo -u root cat answer.md",
        "xargs cat answer.md",
        "printf answer.md | xargs cat",
        "echo `cat answer.md`",
        'echo "$(cat answer.md)"',
        "cat {answer,other}.md",
        "cat <<EOF\n$(cat answer.md)\nEOF\n",
        "cat <<'EOF'\nWe don't need this\nEOF\ncat answer.md",
    ],
)
def test_shell_execution_keeps_protection(answers: Path, command: str) -> None:
    assert shell(answers, command)


def test_home_path_expansion(answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(answers))
    assert shell(answers, "cat ~/answer.md")


def test_grep_default_returns_filenames(answers: Path) -> None:
    assert (
        check_tool({"tool_name": "Grep", "cwd": str(answers), "tool_input": {"pattern": "PRIVATE"}})
        is None
    )


def test_recursive_glob_includes_root_file(answers: Path) -> None:
    assert check_tool(
        {
            "tool_name": "Grep",
            "cwd": str(answers),
            "tool_input": {
                "pattern": "PRIVATE",
                "glob": "**/*.md",
                "output_mode": "content",
            },
        }
    )


def test_rg_uses_ignored_search_scope(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("node_modules/\n.venv/\n")
    for directory in (".git", ".venv", "node_modules"):
        folder = tmp_path / directory
        folder.mkdir(exist_ok=True)
        for index in range(1700):
            (folder / str(index)).touch()
        output = folder / "answer.md"
        output.write_text("PRIVATE")
        protect(output)
    (tmp_path / "source.py").write_text("foo")
    assert shell(tmp_path, "rg foo") is None
    assert shell(tmp_path, "rg --hidden --no-ignore foo")
    assert shell(tmp_path, "rg foo node_modules")


@pytest.mark.parametrize("option", ["--st", "--std", "--stdo", "--stdou"])
def test_cli_rejects_stdout_abbreviations(option: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ask", option, "-p", "hello"])


def test_schema_required_names_missing_field_without_values() -> None:
    schema = {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}}
    with pytest.raises(ValidationError) as caught:
        validate_against_schema(json.dumps({"other": "PRIVATE ANSWER"}), schema)
    assert "title" in str(caught.value)
    assert "PRIVATE ANSWER" not in str(caught.value)


@pytest.mark.parametrize(
    "command",
    [
        "echo safe # `cat answer.md`",
        "echo safe # <<EOF",
        "cat <<'EOF'\n$(cat answer.md)\nEOF\n",
        "cat <<EOF\nDon't expand this\nEOF\n",
    ],
)
def test_literal_text_and_comments_are_not_executed(answers: Path, command: str) -> None:
    assert shell(answers, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\n$(echo ')'; cat answer.md)\nEOF\n",
        "env command cat answer.md",
        "printf answer.md | xargs command cat",
        'echo "$(echo "$(cat answer.md)")"',
    ],
)
def test_nested_execution(answers: Path, command: str) -> None:
    assert shell(answers, command)


def test_schema_nested_required_field() -> None:
    schema = {"properties": {"course": {"type": "object", "required": ["title"]}}}
    with pytest.raises(ValidationError) as caught:
        validate_against_schema('{"course": {}}', schema)
    assert "course" in str(caught.value) and "title" in str(caught.value)


def test_substitution_after_directory_change(answers: Path) -> None:
    assert shell(answers.parent, f'cd {answers.name}; echo "$(cat answer.md)"')
    assert shell(answers, "diff source.py <(cat answer.md)")


def test_quoted_home_is_literal(tmp_path: Path) -> None:
    folder = tmp_path / "~"
    folder.mkdir()
    output = folder / "answer.md"
    output.touch()
    protect(output)
    assert shell(tmp_path, "cat '~/answer.md'")


def test_quoted_brace_filename_is_literal(tmp_path: Path) -> None:
    output = tmp_path / "{answer,other}.md"
    output.touch()
    protect(output)
    assert shell(tmp_path, "cat '{answer,other}.md'")


@pytest.mark.parametrize(
    "command",
    [
        "xargs -a answer.md",
        "sudo --chdir . cat answer.md",
        'env -S "cat answer.md"',
    ],
)
def test_unknown_wrapper_scope_is_not_silently_allowed(answers: Path, command: str) -> None:
    assert shell(answers, command)


@pytest.mark.parametrize(
    "command", ["grep answer <<< 'answer.md'", 'cat <<< "We don\'t need this"']
)
def test_here_strings_are_input_data(answers: Path, command: str) -> None:
    assert shell(answers, command) is None


def test_here_string_substitution_is_checked(answers: Path) -> None:
    assert shell(answers, 'cat <<< "$(cat answer.md)"')
