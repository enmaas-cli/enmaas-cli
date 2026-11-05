#!/usr/bin/env python3
"""Check real host hooks with synthetic files and a local mock model service."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "src"))

from enmaas_cli.outputs import allow_read, is_protected, protect  # noqa: E402

HEAD = "ENMAAS_PREVIEW_HEAD_41857"
TAIL = "ENMAAS_PROTECTED_TAIL_41857"
PUBLIC = "ENMAAS_PUBLIC_SOURCE_41857"
DENIAL = "enmaas containment:"
CASES = (
    "read",
    "shell-read",
    "search",
    "ordinary-file",
    "metadata",
    "inspect",
    "self-approval",
    "stdout",
    "human-override",
    "workdir-read",
    "redirect-read",
    "combined-search",
    "json-search",
    "comment-read",
    "continuation-read",
    "scoped-cd",
    "stdout-abbrev",
    "self-approval-abbrev",
    "pipeline-grep",
    "pipeline-rg",
    "heredoc-write",
    "grep-default",
    "globstar-read",
    "keyword-read",
    "wrapper-read",
    "substitution-read",
    "brace-read",
    "stdout-short",
    "ignored-search",
)


def tool_output(body: dict[str, Any], host: str) -> list[str]:
    if host == "claude":
        return [
            json.dumps(block.get("content", ""), ensure_ascii=False)
            for message in body.get("messages", [])
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        ]
    return [
        str(item.get("output", ""))
        for item in body.get("input", [])
        if item.get("type") in {"function_call_output", "custom_tool_call_output"}
    ]


def anthropic_events(body: dict[str, Any], tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    done = bool(tool_output(body, "claude"))
    block = (
        {"type": "text", "text": "Fixture complete."}
        if done
        else {"type": "tool_use", "id": "toolu_fixture", "name": tool, "input": args}
    )
    message = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": body.get("model"),
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    start = dict(block, **({"text": ""} if done else {"input": {}}))
    delta = (
        {"type": "text_delta", "text": block["text"]}
        if done
        else {"type": "input_json_delta", "partial_json": json.dumps(args)}
    )
    return [
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": start},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": "end_turn" if done else "tool_use",
                "stop_sequence": None,
            },
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


def responses_events(
    body: dict[str, Any], command: str, workdir: Path | None = None
) -> list[dict[str, Any]]:
    done = bool(tool_output(body, "codex"))
    if done:
        item = {
            "type": "message",
            "id": "msg_fixture",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "Fixture complete.",
                    "annotations": [],
                }
            ],
        }
    else:
        item = {
            "type": "function_call",
            "id": "fc_fixture",
            "call_id": "call_fixture",
            "name": "exec_command",
            "status": "completed",
            "arguments": json.dumps(
                {
                    "cmd": command,
                    "login": False,
                    **({"workdir": str(workdir)} if workdir else {}),
                }
            ),
        }
    response = {
        "id": "resp_fixture",
        "object": "response",
        "created_at": int(time.time()),
        "model": body.get("model"),
        "status": "completed",
        "output": [item],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    return [
        {"type": "response.created", "response": dict(response, output=[], status="in_progress")},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


def run_case(host: str, case: str, root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=False)
    nested = root / "nested"
    nested.mkdir()
    answer = (nested if case == "workdir-read" else root) / "answer.md"
    if case == "ignored-search":
        (root / ".git").mkdir()
        (root / ".gitignore").write_text("node_modules/\n")
        (root / "node_modules").mkdir()
        answer = root / "node_modules" / "answer.md"
    answer.write_text(
        "hello " + HEAD + "\n" + "x" * 8000 + "\nhello " + TAIL + "\n", encoding="utf-8"
    )
    protect(answer)
    source = root / "source.py"
    source.write_text(PUBLIC + "\n", encoding="utf-8")
    metadata = root / "results.jsonl"
    metadata.write_text('{"status":"ok"}\n', encoding="utf-8")
    executable = root / "enmaas"
    executable.write_text(
        "#!/bin/sh\n"
        + "PYTHONPATH="
        + shlex.quote(str(PLUGIN / "src"))
        + " exec "
        + shlex.quote(sys.executable)
        + " -c 'from enmaas_cli.cli import main; raise SystemExit(main())' \"$@\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    target = source if case == "ordinary-file" else metadata if case == "metadata" else answer
    command = f"cat {shlex.quote(str(target))}"
    if case == "inspect":
        command = f"{shlex.quote(str(executable))} inspect {shlex.quote(str(answer))}"
    elif case == "self-approval":
        command = f"{shlex.quote(str(executable))} allow-read {shlex.quote(str(answer))}"
    elif case == "stdout":
        command = (
            f"{shlex.quote(str(executable))} --key-file {shlex.quote(str(root / 'missing-key'))}"
            " ask --stdout -p fixture -m rits/local-fixture"
        )
    elif case == "human-override":
        allow_read(answer)
    elif case == "search":
        command = f"rg ENMAAS {shlex.quote(str(answer))}"
    elif case == "workdir-read":
        command = "cat answer.md"
    elif case == "redirect-read":
        command = f"< {shlex.quote(str(answer))} cat"
    elif case == "combined-search":
        command = f"rg -iehello {shlex.quote(str(answer))}"
    elif case == "json-search":
        command = f"rg -l --json hello {shlex.quote(str(answer))}"
    elif case == "comment-read":
        command = (
            f"{shlex.quote(str(executable))} inspect {shlex.quote(str(answer))} # note\n"
            f"cat {shlex.quote(str(answer))}"
        )
    elif case == "continuation-read":
        command = f"cat {root}/ans" + "\\\n" + "wer.md"
    elif case == "scoped-cd":
        command = "(cd nested && pwd); cat answer.md"
    elif case == "stdout-abbrev":
        command = (
            f"{shlex.quote(str(executable))} --conf {root}/missing-config --k {root}/missing-key"
            " ask --std -p fixture -m rits/local-fixture"
        )
    elif case == "self-approval-abbrev":
        command = (
            f"{shlex.quote(str(executable))} --conf {root}/missing-config"
            f" allow-read {shlex.quote(str(answer))}"
        )

    if case in {"pipeline-grep", "pipeline-rg"}:
        reader = "grep" if case == "pipeline-grep" else "rg"
        command = f"printf '%s\\n' {PUBLIC} | {reader} ENMAAS"
    elif case == "heredoc-write":
        command = "cat > note.md <<'EOF'\nWe don't need this\nEOF\nprintf '%s\\n' " + PUBLIC
    elif case == "globstar-read":
        command = "rg -g '**/*.md' ENMAAS ."
    elif case == "grep-default":
        command = "rg -l ENMAAS ."
    elif case == "keyword-read":
        command = "if true; then cat answer.md; fi"
    elif case == "wrapper-read":
        command = "command cat answer.md"
    elif case == "substitution-read":
        command = "echo `cat answer.md`"
    elif case == "brace-read":
        command = "cat {answer,other}.md"
    elif case == "stdout-short":
        command = f"{shlex.quote(str(executable))} ask --st -p fixture"
    elif case == "ignored-search":
        command = "rg ENMAAS ."

    native = host == "claude" and case in {"read", "ordinary-file", "metadata", "human-override"}
    tool = (
        "Read"
        if native
        else "Grep"
        if host == "claude" and case in {"search", "grep-default", "globstar-read"}
        else "Bash"
    )
    args = (
        {"file_path": str(target)}
        if native
        else {"pattern": "ENMAAS", "path": str(answer), "output_mode": "content"}
        if tool == "Grep"
        else {"command": command}
    )
    if tool == "Grep" and case == "grep-default":
        args = {"pattern": "ENMAAS", "path": str(root)}
    elif tool == "Grep" and case == "globstar-read":
        args = {"pattern": "ENMAAS", "path": str(root), "glob": "**/*.md", "output_mode": "content"}
    outputs: list[str] = []
    request_count = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"models":[]}')

        def do_POST(self) -> None:
            nonlocal request_count
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.startswith("/v1/messages/count_tokens"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens":1}')
                return
            request_count += 1
            outputs.extend(tool_output(body, host))
            events = (
                anthropic_events(body, tool, args)
                if host == "claude"
                else responses_events(body, command, nested if case == "workdir-read" else None)
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    env = os.environ.copy()
    for key in (
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "ANTHROPIC_AUTH_TOKEN",
        "ENMAAS_KEY",
    ):
        env.pop(key, None)
    env.update(
        ANTHROPIC_BASE_URL=url,
        ANTHROPIC_API_KEY="local-fixture-key",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
    )
    if host == "claude":
        registration = "plugin-dir"
        cmd = [
            "claude",
            "-p",
            "Run the local file fixture.",
            "--model",
            "claude-sonnet-4-5",
            "--setting-sources",
            "",
            "--plugin-dir",
            str(PLUGIN),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            "Read,Bash,Grep",
            "--allowedTools",
            "Read,Bash,Grep",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    else:
        # Load the bundled hooks as project configuration. This checks the host
        # decision path; marketplace installation and its trust UI are separate.
        registration = "bundled-hooks-config"
        config = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        groups = config["hooks"]["PreToolUse"]
        entries = []
        for group in groups:
            handlers = []
            for handler in group["hooks"]:
                hook_command = handler["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
                handlers.append(
                    '{type="command", command=' + json.dumps(hook_command) + ", timeout=5}"
                )
            entries.append(
                "{matcher="
                + json.dumps(group.get("matcher", "*"))
                + ", hooks=["
                + ",".join(handlers)
                + "]}"
            )
        cmd = [
            "codex",
            "--no-daemon",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "--dangerously-bypass-hook-trust",
            "-C",
            str(root),
            "-s",
            "danger-full-access",
            "-m",
            "gpt-5.1-codex",
            "-c",
            'model_provider="fixture"',
            "-c",
            'model_providers.fixture.name="Fixture"',
            "-c",
            f'model_providers.fixture.base_url="{url}/v1"',
            "-c",
            'model_providers.fixture.wire_api="responses"',
            "-c",
            "model_providers.fixture.requires_openai_auth=false",
            "-c",
            "hooks.PreToolUse=[" + ",".join(entries) + "]",
            "Run the local file fixture.",
        ]
    started = time.monotonic()
    process = subprocess.Popen(
        cmd,
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    finally:
        server.shutdown()
        server.server_close()
    (root / "stdout.log").write_text(stdout, encoding="utf-8")
    (root / "stderr.log").write_text(stderr, encoding="utf-8")
    text = "\n".join(outputs)
    denied = DENIAL in text
    expected_denial = case not in {
        "ordinary-file",
        "metadata",
        "inspect",
        "human-override",
        "pipeline-grep",
        "pipeline-rg",
        "heredoc-write",
        "grep-default",
        "ignored-search",
    }
    checks = [process.returncode == 0, not timed_out, request_count >= 2, denied == expected_denial]
    if case == "inspect":
        checks += [HEAD in text, TAIL not in text, "truncated" in text]
    elif case == "human-override":
        checks += [TAIL in text, not is_protected(answer)]
    elif case == "ordinary-file":
        checks += [PUBLIC in text]
    elif case == "metadata":
        checks += ["status" in text, "ok" in text]
    elif case in {"pipeline-grep", "pipeline-rg", "heredoc-write", "ignored-search"}:
        checks += [PUBLIC in text, HEAD not in text, TAIL not in text]
        if case == "heredoc-write":
            checks += [(root / "note.md").exists() and "don't" in (root / "note.md").read_text()]
    elif case == "grep-default":
        checks += ["answer.md" in text, HEAD not in text, TAIL not in text]
    else:
        checks += [HEAD not in text, TAIL not in text, is_protected(answer)]
    result = {
        "host": host,
        "case": case,
        "registration": registration,
        "passed": all(checks),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "requests": request_count,
        "denial_received": denied,
        "full_answer_received": TAIL in text,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    root = args.outdir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    results = []
    for host in ("claude", "codex") if args.host == "both" else (args.host,):
        for case in args.case or CASES:
            if host == "claude" and case == "workdir-read":
                continue
            result = run_case(host, case, root / host / case)
            results.append(result)
            print(json.dumps(result), flush=True)
    (root / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
