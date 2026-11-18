"""Check host fixture construction without running hosts or registered hooks."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from enmaas_cli.auth import resolve_key
from enmaas_cli.cli import build_parser, resolve_base_url
from enmaas_cli.config import default_config_path, load_config
from enmaas_cli.containment import check_tool
from enmaas_cli.outputs import is_protected

ROOT = Path(__file__).resolve().parents[1]
LOCAL_URL = "http://127.0.0.1:12345"
FAKE_KEY = "local-fixture-key"
PATH_VARIABLES = (
    "HOME",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
)
INHERITED_SETTINGS = (
    "ENMAAS_BASE_URL",
    "ENMAAS_KEY",
    "ENMAAS_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "BASH_FUNC_fixture%%",
    "SHELL",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONPYCACHEPREFIX",
    "PYTHONSTARTUP",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
    "NODE_OPTIONS",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "UNRELATED_SETTING",
)
# Only these fixed, harmless path components enter the bounded shell probes.
PUNCTUATION_ROOTS = (
    "case with spaces",
    "case'quote",
    "case$HOME",
    "case`printf INJECTED>HOST_CHECK_INJECTED`",
    "case;printf INJECTED>HOST_CHECK_INJECTED;#",
)
CLI_CASES = (
    "inspect",
    "self-approval",
    "stdout",
    "comment-read",
    "stdout-abbrev",
    "self-approval-abbrev",
    "stdout-short",
)
# case, Claude tool, protected target directory, output mode, expected denial
CASE_CONTRACT = (
    ("read", "Read", ".", "content", True),
    ("shell-read", "Bash", ".", "content", True),
    ("search", "Grep", ".", "content", True),
    ("ordinary-file", "Read", ".", "public", False),
    ("metadata", "Read", ".", "metadata", False),
    ("inspect", "Bash", ".", "preview", False),
    ("self-approval", "Bash", ".", "approval", True),
    ("stdout", "Bash", ".", "stdout", True),
    ("human-override", "Read", ".", "approved-content", False),
    ("workdir-read", None, "nested", "content", True),
    ("redirect-read", "Bash", ".", "content", True),
    ("combined-search", "Bash", ".", "content", True),
    ("json-search", "Bash", ".", "json-content", True),
    ("comment-read", "Bash", ".", "content", True),
    ("continuation-read", "Bash", ".", "content", True),
    ("scoped-cd", "Bash", ".", "content", True),
    ("stdout-abbrev", "Bash", ".", "stdout", True),
    ("self-approval-abbrev", "Bash", ".", "approval", True),
    ("pipeline-grep", "Bash", ".", "public", False),
    ("pipeline-rg", "Bash", ".", "public", False),
    ("heredoc-write", "Bash", ".", "public", False),
    ("grep-default", "Grep", ".", "filenames", False),
    ("globstar-read", "Grep", ".", "content", True),
    ("keyword-read", "Bash", ".", "content", True),
    ("wrapper-read", "Bash", ".", "content", True),
    ("substitution-read", "Bash", ".", "content", True),
    ("brace-read", "Bash", ".", "content", True),
    ("stdout-short", "Bash", ".", "stdout", True),
    ("ignored-search", "Bash", "node_modules", "public", False),
)


@pytest.fixture
def host_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "host_checks", ROOT / "scripts/check-host-hooks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostDouble:
    """Capture Popen and exchange fake host requests with an unbound handler."""

    def __init__(self, runner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        self.runner = runner
        self.command: list[str] = []
        self.options: dict[str, Any] = {}
        self.fixture: dict[str, Any] = {}
        self.reply = runner.DENIAL + " fixture blocked"
        self.write_note = False
        self.closed = False
        self.shutdown = False

        def server(address: tuple[str, int], handler: Any) -> SimpleNamespace:
            assert address == ("127.0.0.1", 0)
            self.handler = handler
            return SimpleNamespace(
                server_port=12345,
                serve_forever=lambda: None,
                shutdown=lambda: setattr(self, "shutdown", True),
                server_close=lambda: setattr(self, "closed", True),
            )

        monkeypatch.setattr(runner, "ThreadingHTTPServer", server)
        monkeypatch.setattr(
            runner,
            "threading",
            SimpleNamespace(Thread=lambda **_: SimpleNamespace(start=lambda: None)),
        )
        monkeypatch.setattr(
            runner,
            "subprocess",
            SimpleNamespace(
                Popen=self.popen,
                PIPE=subprocess.PIPE,
                DEVNULL=subprocess.DEVNULL,
                TimeoutExpired=subprocess.TimeoutExpired,
            ),
        )

    def request(self, path: str, body: dict[str, Any], method: str = "POST") -> tuple[int, bytes]:
        handler = object.__new__(self.handler)
        data = json.dumps(body).encode()
        handler.path = path
        handler.headers = {"Content-Length": str(len(data))}
        handler.rfile = io.BytesIO(data)
        handler.wfile = io.BytesIO()
        status: list[int] = []
        handler.send_response = status.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        getattr(handler, "do_" + method)()
        return status[0], handler.wfile.getvalue()

    def popen(self, command: list[str], **kwargs: Any) -> SimpleNamespace:
        self.command = command
        self.options = kwargs
        host = command[0]
        endpoint = "/v1/messages" if host == "claude" else "/v1/responses"
        status, wire = self.request(endpoint, {})
        assert status == 200
        events = [
            json.loads(line[6:]) for line in wire.decode().splitlines() if line.startswith("data: ")
        ]
        response: dict[str, Any]
        if host == "claude":
            start = next(event for event in events if event["type"] == "content_block_start")
            delta = next(event for event in events if event["type"] == "content_block_delta")
            self.fixture = {
                "tool": start["content_block"]["name"],
                "args": json.loads(delta["delta"]["partial_json"]),
            }
            response = {"messages": [{"content": [{"type": "tool_result", "content": self.reply}]}]}
        else:
            item = next(
                event["item"] for event in events if event["type"] == "response.output_item.done"
            )
            self.fixture = {"tool": item["name"], "args": json.loads(item["arguments"])}
            response = {"input": [{"type": "function_call_output", "output": self.reply}]}
        self.request(endpoint, response)
        if self.write_note:
            (kwargs["cwd"] / "note.md").write_text("We don't need this\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, communicate=lambda **_: ("", ""))

    def run(
        self, host: str, case: str, root: Path, groups: list[Any] | None = None
    ) -> dict[str, Any]:
        result: dict[str, Any] = self.runner.run_case(host, case, root, groups)
        assert self.shutdown and self.closed
        assert self.options["cwd"] == root
        assert self.options["stdin"] == subprocess.DEVNULL
        assert self.options["start_new_session"] is True
        return result

    def shell_command(self) -> str:
        args = self.fixture["args"]
        return str(args["cmd"] if self.fixture["tool"] == "exec_command" else args["command"])


def inherited_sentinels(monkeypatch: pytest.MonkeyPatch, root: Path) -> dict[str, str]:
    inherited = {name: str(root / name.lower()) for name in PATH_VARIABLES}
    inherited.update({name: "synthetic-inherited-" + name for name in INHERITED_SETTINGS})
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
        if name in PATH_VARIABLES:
            Path(value).mkdir(parents=True)
    return inherited


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize("installed", [False, True])
def test_child_environment_and_host_settings_are_case_local(
    host_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str,
    installed: bool,
) -> None:
    inherited = inherited_sentinels(monkeypatch, tmp_path / "inherited")
    binary_dir = tmp_path / "installed-binaries"
    binary_dir.mkdir()
    binary = binary_dir / host
    binary.write_text("not executed\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.defpath)
    groups = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "NEVER_EXECUTE"}]}]
    if installed:
        supplied = tmp_path / "installed-settings"
        if host == "claude":
            supplied.write_text(
                json.dumps({"env": inherited, "hooks": {"PreToolUse": groups, "Stop": groups}})
            )
            groups = host_runner.installed_claude_groups(supplied)
        else:
            supplied.write_text(
                'model_provider="inherited"\n'
                '[model_providers.inherited]\nbase_url="https://synthetic.invalid"\n'
                "[hooks]\nPreToolUse=" + host_runner.toml_value(groups) + "\nStop=[]\n"
            )
            groups = host_runner.installed_codex_groups(supplied)
    captured = HostDouble(host_runner, monkeypatch)
    root = tmp_path / "case"
    captured.run(host, "stdout-short", root, groups if installed else None)
    env = captured.options["env"]
    assert not (set(env.values()) & set(inherited.values()))
    for name in PATH_VARIABLES:
        directory = Path(env[name])
        assert directory.is_dir()
        assert directory.resolve().is_relative_to(root.resolve())
        assert directory.stat().st_mode & 0o077 == 0
    assert shutil.which(host, path=env["PATH"]) == str(binary)
    assert env["LC_ALL"] == env["LANG"] == "C"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    for name in INHERITED_SETTINGS:
        if name not in {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "LANG", "LC_ALL"}:
            assert name not in env
    assert env["ANTHROPIC_BASE_URL"] == LOCAL_URL
    assert env["ANTHROPIC_API_KEY"] == FAKE_KEY
    command = captured.command
    if host == "claude":
        assert command[command.index("--setting-sources") + 1] == ""
        settings = json.loads(Path(command[command.index("--settings") + 1]).read_text())
        assert set(settings) == {"hooks"}
        assert set(settings["hooks"]) == {"PreToolUse"}
        actual_groups = settings["hooks"]["PreToolUse"]
        assert json.loads(command[command.index("--mcp-config") + 1]) == {"mcpServers": {}}
    else:
        assert "--ignore-user-config" in command
        overrides = tomllib.loads(
            "\n".join(command[i + 1] for i, value in enumerate(command) if value == "-c")
        )
        assert overrides["model_provider"] == "fixture"
        assert overrides["model_providers"]["fixture"]["base_url"] == LOCAL_URL + "/v1"
        assert overrides["model_providers"]["fixture"]["requires_openai_auth"] is False
        assert set(overrides["hooks"]) == {"PreToolUse"}
        actual_groups = overrides["hooks"]["PreToolUse"]
    assert actual_groups == (groups if installed else host_runner.repo_hook_groups())


@pytest.mark.parametrize("path", [None, ""])
def test_missing_search_path_has_a_usable_default(
    host_runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str | None
) -> None:
    if path is None:
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", path)
    captured = HostDouble(host_runner, monkeypatch)
    captured.run("codex", "read", tmp_path / "case")
    assert shutil.which("sh", path=captured.options["env"]["PATH"])


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize("case", CLI_CASES)
def test_every_cli_fixture_resolves_its_fake_config_and_key(
    host_runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str, case: str
) -> None:
    inherited = inherited_sentinels(monkeypatch, tmp_path / "inherited")
    home = Path(inherited["HOME"])
    fallback_config = home / ".config/enmaas/config.toml"
    fallback_config.parent.mkdir(parents=True)
    fallback_config.write_text(
        'base_url="https://synthetic.invalid"\ndefault_model="competing-model"\n'
    )
    fallback_key = home / ".secrets/enmaas"
    fallback_key.parent.mkdir()
    fallback_key.write_text("competing-synthetic-key\n")
    fallback_key.chmod(0o600)
    captured = HostDouble(host_runner, monkeypatch)
    root = tmp_path / "case 'quoted' $value `text`; end"
    captured.run(host, case, root)
    command = captured.shell_command().split(" # note\n", 1)[0]
    tokens = shlex.split(command)
    assert tokens[0] == str(root / "enmaas")
    assert "--config" in tokens and "--key-file" in tokens
    parser = build_parser()
    if case in {"stdout-short", "stdout-abbrev", "self-approval-abbrev"}:
        # Keep the real fixture's rejected flags. Resolve the same argv with
        # argparse's prefix support to check safety if the CLI accepts them.
        with pytest.raises(SystemExit):
            parser.parse_args(tokens[1:])
        parser.allow_abbrev = True
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for subparser in action.choices.values():
                    subparser.allow_abbrev = True
    args = parser.parse_args(tokens[1:])
    assert args.config.is_relative_to(root) and args.key_file.is_relative_to(root)
    assert args.config.is_file() and args.key_file.is_file()
    assert args.key_file.stat().st_mode & 0o077 == 0
    env = captured.options["env"]
    # Check that the competing fallback is real before testing explicit inputs.
    assert default_config_path() == fallback_config
    assert load_config(default_config_path()).default_model == "competing-model"
    assert resolve_key(key_file=None, env={}, home=home).value == "competing-synthetic-key"
    config = load_config(args.config)
    key = resolve_key(key_file=args.key_file, env=env, home=home, config_key_file=config.key_file)
    assert key.value == FAKE_KEY
    assert key.source == str(args.key_file)
    assert config.default_model == "rits/local-fixture"
    assert config.base_url == LOCAL_URL
    assert resolve_base_url(config.base_url, env, args.config) == LOCAL_URL
    if case.startswith("stdout"):
        assert args.stdout is True
        assert (args.model or config.default_model) == "rits/local-fixture"
    expected_flags = {
        "stdout-short": {"--st"},
        "stdout-abbrev": {"--std", "--conf", "--k"},
        "self-approval-abbrev": {"--conf"},
    }
    assert expected_flags.get(case, set()).issubset(tokens)


def shell_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "shell-home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "PATH": os.defpath, "LC_ALL": "C", "LANG": "C"}


@pytest.mark.parametrize("component", PUNCTUATION_ROOTS)
@pytest.mark.parametrize("case", ["continuation-read", "stdout-abbrev", "self-approval-abbrev"])
def test_fixed_punctuation_paths_remain_one_shell_operand(
    host_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    component: str,
    case: str,
) -> None:
    root = tmp_path / component
    captured = HostDouble(host_runner, monkeypatch)
    captured.run("codex", case, root)
    command = captured.shell_command()
    if case == "continuation-read":
        assert "\\\n" in command
        probe = command
    else:
        # set records operands. It does not run enmaas or any stored hook.
        probe = "set -- " + command + "\nprintf '%s\\0' \"$@\""
    completed = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", probe],
        cwd=tmp_path,
        env=shell_environment(tmp_path),
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert not (tmp_path / "HOST_CHECK_INJECTED").exists()
    assert completed.returncode == 0, completed.stderr
    if case == "continuation-read":
        assert completed.stdout == (root / "answer.md").read_bytes()
    else:
        tokens = completed.stdout.decode().rstrip("\0").split("\0")
        assert tokens == shlex.split(command)
        for flag in ("--config", "--key-file", "--conf", "--k"):
            if flag in tokens:
                assert Path(tokens[tokens.index(flag) + 1]).is_file()


def test_wrapper_uses_selected_interpreter_and_punctuation_source_tree(
    host_runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_root = tmp_path / "source 'quoted' $value `text`; end"
    package = source_root / "src/enmaas_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import json, sys\ndef entry():\n"
        "    print(json.dumps({'source': __file__, 'interpreter': sys.executable}))\n"
    )
    monkeypatch.setattr(host_runner, "ROOT", source_root)
    captured = HostDouble(host_runner, monkeypatch)
    root = tmp_path / "case 'quoted' $value `text`; end"
    captured.run("codex", "stdout", root, [])
    wrapper = root / "enmaas"
    assert os.access(wrapper, os.X_OK)
    completed = subprocess.run(
        [str(wrapper)],
        cwd=tmp_path,
        env=shell_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    assert json.loads(completed.stdout) == {
        "source": str(package / "cli.py"),
        "interpreter": sys.executable,
    }
    assert not list(source_root.rglob("__pycache__"))


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize(
    ("reply", "passed", "denied", "hook_error"),
    [
        ("error: unrecognized arguments: --st", False, False, False),
        ("enmaas containment: blocked", True, True, False),
        ("enmaas containment: could not check tool input", False, False, True),
        ("enmaas containment: ENMAAS_PROTECTED_TAIL_41857", False, True, False),
        ("enmaas containment: ENMAAS_PREVIEW_HEAD_41857", False, True, False),
    ],
)
def test_cli_rejection_and_content_checks_are_independent_of_denial(
    host_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str,
    reply: str,
    passed: bool,
    denied: bool,
    hook_error: bool,
) -> None:
    captured = HostDouble(host_runner, monkeypatch)
    captured.reply = reply
    result = captured.run(host, "stdout-short", tmp_path / "case")
    assert result["passed"] is passed
    assert result["denial_received"] is denied
    assert result["hook_error"] is hook_error


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unknown_fixture_endpoint_returns_a_local_error(
    host_runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str, method: str
) -> None:
    captured = HostDouble(host_runner, monkeypatch)
    captured.run(host, "stdout", tmp_path / "case")
    status, body = captured.request("/unexpected-fixture-path", {}, method)
    assert status == 404
    assert len(body) < 1024


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_fixture_model_endpoints_keep_query_parameters(
    host_runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str
) -> None:
    captured = HostDouble(host_runner, monkeypatch)
    captured.run(host, "stdout", tmp_path / "case")
    status, body = captured.request("/v1/models?fixture=true", {}, "GET")
    assert status == 200 and json.loads(body) == {"models": []}
    endpoint = "/v1/messages" if host == "claude" else "/v1/responses"
    status, body = captured.request(endpoint + "?fixture=true", {})
    assert status == 200 and b"event: " in body
    if host == "claude":
        status, body = captured.request("/v1/messages/count_tokens?fixture=true", {})
        assert status == 200 and json.loads(body) == {"input_tokens": 1}


def expected_command(case: str, root: Path, runner: ModuleType, actual: str) -> str:
    """Pin the existing case grammar while checking new global inputs separately."""

    def quote(name: str) -> str:
        return shlex.quote(str(root / name))

    if case in CLI_CASES:
        first, separator, rest = actual.partition(" # note\n")
        words = shlex.split(first)
        assert words.pop(0) == str(root / "enmaas")
        for flag in ("--config", "--key-file", "--conf", "--k"):
            while flag in words:
                index = words.index(flag)
                assert Path(words[index + 1]).is_relative_to(root)
                assert Path(words[index + 1]).is_file()
                del words[index : index + 2]
        suffixes = {
            "inspect": ["inspect", str(root / "answer.md")],
            "self-approval": ["allow-read", str(root / "answer.md")],
            "stdout": ["ask", "--stdout", "-p", "fixture", "-m", "rits/local-fixture"],
            "comment-read": ["inspect", str(root / "answer.md")],
            "stdout-abbrev": ["ask", "--std", "-p", "fixture", "-m", "rits/local-fixture"],
            "self-approval-abbrev": ["allow-read", str(root / "answer.md")],
            "stdout-short": ["ask", "--st", "-p", "fixture"],
        }
        assert words == suffixes[case]
        assert separator == (" # note\n" if case == "comment-read" else "")
        assert rest == (f"cat {quote('answer.md')}" if separator else "")
        return actual
    fixed: dict[str, str] = {
        "workdir-read": "cat answer.md",
        "scoped-cd": "(cd nested && pwd); cat answer.md",
        "heredoc-write": "cat > note.md <<'EOF'\nWe don't need this\nEOF\nprintf '%s\\n' "
        + runner.PUBLIC,
        "globstar-read": "rg -g '**/*.md' ENMAAS .",
        "grep-default": "rg -l ENMAAS .",
        "keyword-read": "if true; then cat answer.md; fi",
        "wrapper-read": "command cat answer.md",
        "substitution-read": "echo `cat answer.md`",
        "brace-read": "cat {answer,other}.md",
        "ignored-search": "rg ENMAAS .",
        "pipeline-grep": f"printf '%s\\n' {runner.PUBLIC} | grep ENMAAS",
        "pipeline-rg": f"printf '%s\\n' {runner.PUBLIC} | rg ENMAAS",
        "redirect-read": f"< {quote('answer.md')} cat",
        "search": f"rg ENMAAS {quote('answer.md')}",
        "combined-search": f"rg -iehello {quote('answer.md')}",
        "json-search": f"rg -l --json hello {quote('answer.md')}",
    }
    if case == "continuation-read":
        assert "\\\n" in actual
        assert shlex.split(actual.replace("\\\n", "")) == ["cat", str(root / "answer.md")]
        return actual
    target = (
        "source.py"
        if case == "ordinary-file"
        else "results.jsonl"
        if case == "metadata"
        else "answer.md"
    )
    return fixed.get(case, f"cat {quote(target)}")


def test_all_57_host_cases_keep_their_acceptance_contract(
    host_runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert tuple(row[0] for row in CASE_CONTRACT) == host_runner.CASES
    seen = []
    for host in ("claude", "codex"):
        for case, claude_tool, answer_dir, output_mode, denied in CASE_CONTRACT:
            if host == "claude" and claude_tool is None:
                continue
            captured = HostDouble(host_runner, monkeypatch)
            root = tmp_path / "case 'quoted' $value `text`; end" / host / case
            if not denied:
                captured.reply = {
                    "public": host_runner.PUBLIC,
                    "metadata": '{"status":"ok"}',
                    "preview": host_runner.HEAD + " truncated",
                    "approved-content": host_runner.TAIL,
                    "filenames": "answer.md",
                }[output_mode]
            captured.write_note = case == "heredoc-write"
            result = captured.run(host, case, root)
            assert result["passed"], (host, case, result)
            assert result["denial_received"] is denied
            assert is_protected(root / answer_dir / "answer.md") is (case != "human-override")
            args = captured.fixture["args"]
            assert captured.fixture["tool"] == (claude_tool if host == "claude" else "exec_command")
            if host == "codex":
                assert args["login"] is False
                assert args.get("workdir") == (
                    str(root / "nested") if case == "workdir-read" else None
                )
            if host == "claude" and claude_tool == "Read":
                target = (
                    "source.py"
                    if case == "ordinary-file"
                    else "results.jsonl"
                    if case == "metadata"
                    else "answer.md"
                )
                assert args == {"file_path": str(root / target)}
            elif host == "claude" and claude_tool == "Grep":
                expected: dict[str, str] = {"pattern": "ENMAAS", "path": str(root)}
                if case == "search":
                    expected.update(path=str(root / "answer.md"), output_mode="content")
                elif case == "globstar-read":
                    expected.update(glob="**/*.md", output_mode="content")
                assert args == expected
            else:
                command = captured.shell_command()
                assert command == expected_command(case, root, host_runner, command)
            reason = check_tool(
                {"tool_name": captured.fixture["tool"], "cwd": str(root), "tool_input": args}
            )
            assert bool(reason) is denied, (host, case, reason)
            seen.append((host, case))
    assert len(seen) == len(set(seen)) == 57
