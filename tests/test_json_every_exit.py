"""With --json, every exit of ask and map prints exactly one JSON object on stdout.

A --json caller reads stdout only. An error that went to stderr alone, or a
traceback, would leave it with nothing to parse. No traceback reaches
stderr unless ENMAAS_DEBUG=1, and then the key is removed from it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import enmaas_cli
from enmaas_cli import cli as cli_module
from enmaas_cli.auth import REDACTION
from enmaas_cli.cli import main

KEY = "sk-every-exit-key-77"
MODEL = "rits/zai-org/glm-5-3"
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Point the CLI at a mock gateway and return the requests it receives."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", KEY)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    monkeypatch.delenv("ENMAAS_DEBUG", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPLY)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    return seen


def _one_object(out: str) -> dict[str, Any]:
    """The whole of stdout parses as one JSON object."""
    value = json.loads(out)
    assert isinstance(value, dict)
    return value


def _config(tmp_path: Path, text: str = "") -> list[str]:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return ["--config", str(path)]


def _map_args(tmp_path: Path, *, items: str | None = None, template: str = "do {x}") -> list[str]:
    items_path = tmp_path / "items.jsonl"
    if items is not None:
        items_path.write_text(items, encoding="utf-8")
    template_path = tmp_path / "t.txt"
    template_path.write_text(template, encoding="utf-8")
    return [
        "map",
        "--items",
        str(items_path),
        "--template",
        str(template_path),
        "--outdir",
        str(tmp_path / "out"),
        "--retries",
        "0",
        "--json",
    ]


ONE_ITEM = json.dumps({"id": "a", "x": 1}) + "\n"
ASK = ["ask", "-m", MODEL, "--dialect", "openai", "--retries", "0", "--json"]


def _expect_error(
    capsys: pytest.CaptureFixture[str], code: int, *, expected: int = 1
) -> dict[str, Any]:
    out, err = capsys.readouterr()
    assert code == expected, err
    assert "Traceback" not in err
    assert KEY not in out + err
    return _one_object(out)


def test_map_with_a_missing_items_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    code = main([*_config(tmp_path), *_map_args(tmp_path), "-m", MODEL])
    obj = _expect_error(capsys, code)
    assert obj["status"] == "error"
    assert "items.jsonl" in obj["error"]
    assert seen == []


def test_map_with_a_template_naming_a_missing_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    args = _map_args(tmp_path, items=ONE_ITEM, template="do {missing}")
    obj = _expect_error(capsys, main([*_config(tmp_path), *args, "-m", MODEL]))
    assert obj["status"] == "error"
    assert "missing" in obj["error"]
    assert seen == []


def test_map_with_an_unknown_model_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    args = _map_args(tmp_path, items=ONE_ITEM)
    obj = _expect_error(capsys, main([*_config(tmp_path), *args, "-m", "nobody/knows"]))
    assert obj["status"] == "error"
    assert "cannot determine dialect" in obj["error"]
    assert seen == []


@pytest.mark.parametrize("command", ["map", "ask"])
def test_a_malformed_budget_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _env(tmp_path, monkeypatch)
    config = _config(tmp_path, '[budget]\nwindow_days = 7\ntokens = "lots"\n')
    if command == "map":
        args = [*_map_args(tmp_path, items=ONE_ITEM), "-m", MODEL]
    else:
        args = [*ASK, "-p", "hi"]
    obj = _expect_error(capsys, main([*config, *args]))
    assert obj == {"status": "error", "error": "[budget] tokens must be an integer, got 'lots'"}
    assert seen == []


def test_a_malformed_budget_table_without_json_keeps_the_stderr_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(tmp_path, monkeypatch)
    config = _config(tmp_path, '[budget]\nwindow_days = 7\ntokens = "lots"\n')
    code = main([*config, "ask", "-m", MODEL, "-p", "hi"])
    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    assert err == "enmaas: [budget] tokens must be an integer, got 'lots'\n"


def test_map_with_a_linked_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "results.jsonl").symlink_to(tmp_path / "elsewhere.jsonl")
    args = _map_args(tmp_path, items=ONE_ITEM)
    obj = _expect_error(capsys, main([*_config(tmp_path), *args, "-m", MODEL]))
    assert obj["status"] == "error"
    assert "is not a regular file" in obj["error"]
    assert seen == []


def test_map_with_an_index_another_user_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "results.jsonl").write_text("", encoding="utf-8")
    real_uid = os.getuid()
    # Every file this test made now looks like another user's.
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    args = _map_args(tmp_path, items=ONE_ITEM)
    obj = _expect_error(capsys, main([*_config(tmp_path), *args, "-m", MODEL]))
    assert obj["status"] == "error"
    assert "owned by another user" in obj["error"]
    assert seen == []


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_ask_with_no_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Terminal())
    obj = _expect_error(capsys, main([*_config(tmp_path), *ASK]))
    assert obj == {
        "status": "error",
        "error": "no prompt: pass -p, --prompt-file, or pipe text on stdin",
    }
    assert seen == []


def test_ask_with_a_missing_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    missing = tmp_path / "gone.txt"
    obj = _expect_error(capsys, main([*_config(tmp_path), *ASK, "-p", "hi", "-f", str(missing)]))
    assert obj == {"status": "error", "error": f"attachment does not exist: {missing}"}
    assert seen == []


def test_ask_with_a_missing_schema_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    schema = tmp_path / "gone.schema.json"
    args = [*_config(tmp_path), *ASK, "-p", "hi", "--schema", str(schema)]
    obj = _expect_error(capsys, main(args))
    assert obj["status"] == "error"
    assert "gone.schema.json" in obj["error"]
    assert seen == []


def _budget(tmp_path: Path) -> list[str]:
    return _config(tmp_path, "[budget]\nwindow_days = 7\ntokens = 10\n")


@pytest.mark.parametrize("command", ["ask", "map"])
def test_a_rolling_budget_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seen = _env(tmp_path, monkeypatch)
    config = _budget(tmp_path)
    record = ["record", "--model", MODEL, "--command", "x", "--input", "20", "--output", "0"]
    assert main([*config, *record]) == 0
    capsys.readouterr()
    args = [*ASK, "-p", "hi"] if command == "ask" else [*_map_args(tmp_path, items=ONE_ITEM)]
    obj = _expect_error(capsys, main([*config, *args]), expected=3)
    assert obj["status"] == "refused"
    assert obj["abort_reason"] == "budget"
    assert obj["budget"]["rolling"] == {"ceiling": 10, "spent": 20}
    assert seen == []


@pytest.mark.parametrize(
    ("setup", "phrase"),
    [
        ("no-usage-log", "ENMAAS_NO_USAGE_LOG"),
        ("bad-ledger", "could not be read"),
        ("bad-open-runs", "open-run records"),
    ],
)
def test_every_other_budget_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    phrase: str,
) -> None:
    seen = _env(tmp_path, monkeypatch)
    state = tmp_path / "state" / "enmaas" / "runs"
    state.mkdir(parents=True)
    if setup == "no-usage-log":
        monkeypatch.setenv("ENMAAS_NO_USAGE_LOG", "1")
    elif setup == "bad-ledger":
        (state / "usage.jsonl").write_text("not json\n", encoding="utf-8")
    else:
        if os.geteuid() == 0:
            pytest.skip("root reads a file of mode 000")
        # A record file that cannot be read leaves open spend unknown.
        records = state / "open-runs.jsonl"
        records.write_text("", encoding="utf-8")
        records.chmod(0)
    obj = _expect_error(capsys, main([*_budget(tmp_path), *ASK, "-p", "hi"]), expected=3)
    assert obj["status"] == "refused"
    assert phrase in obj["error"]
    assert seen == []


def test_a_spent_per_run_budget_on_resume_is_one_map_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _env(tmp_path, monkeypatch)
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "results.jsonl").write_text(
        json.dumps({"type": "request", "charge": 50, "items": 1}) + "\n", encoding="utf-8"
    )
    args = [*_map_args(tmp_path, items=ONE_ITEM), "-m", MODEL, "--resume", "--token-budget", "10"]
    obj = _expect_error(capsys, main([*_config(tmp_path), *args]), expected=3)
    assert obj["abort_reason"] == "budget"
    assert obj["budget"]["tripped"] == "per_run"
    assert seen == []


def _inject(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the prompt assembly inside _dispatch raise an error no handler expects."""

    def broken(**kwargs: Any) -> str:
        raise RuntimeError(f"unexpected, near {KEY} and the prompt")

    monkeypatch.setattr(cli_module, "assemble_prompt", broken)


def test_an_unexpected_error_inside_dispatch_is_one_object_without_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(tmp_path, monkeypatch)
    _inject(monkeypatch)
    obj = _expect_error(capsys, main([*_config(tmp_path), *ASK, "-p", "hi"]))
    assert obj["status"] == "error"
    assert obj["error"].startswith("internal error (RuntimeError at test_json_every_exit.py:")
    assert "unexpected" not in obj["error"]


def test_an_unexpected_error_without_json_is_one_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(tmp_path, monkeypatch)
    _inject(monkeypatch)
    code = main([*_config(tmp_path), "ask", "-m", MODEL, "-p", "hi"])
    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    (line,) = err.splitlines()
    assert line.startswith("error: internal error (RuntimeError at ")
    assert KEY not in err


def test_debug_shows_the_traceback_without_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(tmp_path, monkeypatch)
    _inject(monkeypatch)
    monkeypatch.setenv("ENMAAS_DEBUG", "1")
    code = main([*_config(tmp_path), *ASK, "-p", "hi"])
    out, err = capsys.readouterr()
    assert code == 1
    assert _one_object(out)["error"].startswith("internal error (RuntimeError at ")
    assert "Traceback (most recent call last)" in err
    assert f"RuntimeError: unexpected, near {REDACTION} and the prompt" in err
    assert KEY not in out + err


def _models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[], httpx.Response],
    *extra: str,
) -> int:
    real_client = httpx.AsyncClient
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(lambda r: respond())),
    )
    return main([*_config(tmp_path), "models", "--dialect", "openai", *extra])


BAD_CATALOGS = [
    pytest.param(lambda: httpx.Response(200, json=[{"id": "m"}]), "not an object", id="list"),
    pytest.param(lambda: httpx.Response(200, json={"data": ["m"]}), "string id", id="string"),
    pytest.param(lambda: httpx.Response(200, json={"data": [{"id": 5}]}), "string id", id="id"),
    pytest.param(lambda: httpx.Response(200, json={"data": "m"}), "no list", id="data"),
    pytest.param(
        lambda: httpx.Response(
            200,
            text="<html><body>sign in, secret page</body></html>",
            headers={"content-type": "text/html"},
        ),
        "not JSON",
        id="html",
    ),
]


@pytest.mark.parametrize(("respond", "phrase"), BAD_CATALOGS)
def test_a_bad_catalog_is_one_error_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    respond: Callable[[], httpx.Response],
    phrase: str,
) -> None:
    code = _models(tmp_path, monkeypatch, respond)
    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    (line,) = err.splitlines()
    assert line.startswith("error: ")
    assert phrase in line
    assert "secret page" not in line
    assert "Traceback" not in err


@pytest.mark.parametrize(("respond", "phrase"), BAD_CATALOGS)
def test_a_bad_catalog_with_json_is_one_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    respond: Callable[[], httpx.Response],
    phrase: str,
) -> None:
    obj = _expect_error(capsys, _models(tmp_path, monkeypatch, respond, "--json"))
    assert obj["status"] == "error"
    assert phrase in obj["error"]
    assert "secret page" not in obj["error"]


def test_a_failed_catalog_request_with_json_is_one_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respond = lambda: httpx.Response(401, text=f"bad key {KEY}")  # noqa: E731
    obj = _expect_error(capsys, _models(tmp_path, monkeypatch, respond, "--json"))
    assert obj["status"] == "error"
    assert "401" in obj["error"]
    assert "\n" not in obj["error"]


# A child that runs the console script with the prompt assembly broken.
BROKEN_PROGRAM = """
import os
from enmaas_cli import cli

def broken(**kwargs):
    raise RuntimeError("unexpected, near " + os.environ["ENMAAS_KEY"])

cli.assemble_prompt = broken
cli.entry()
"""


def _child(
    tmp_path: Path, program: str, *args: str, debug: bool = False
) -> subprocess.CompletedProcess[str]:
    source = str(Path(enmaas_cli.__file__).resolve().parents[1])
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "ENMAAS_BASE_URL": "http://127.0.0.1:9",
        "ENMAAS_KEY": KEY,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": source,
    }
    if debug:
        env["ENMAAS_DEBUG"] = "1"
    return subprocess.run(
        [sys.executable, "-c", program, *args],
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("debug", [False, True], ids=["plain", "debug"])
def test_a_child_process_reports_an_unexpected_error_as_one_object(
    tmp_path: Path, debug: bool
) -> None:
    done = _child(tmp_path, BROKEN_PROGRAM, *ASK, "-p", "hi", debug=debug)
    assert done.returncode == 1, done.stderr
    obj = _one_object(done.stdout)
    assert obj["error"].startswith("internal error (RuntimeError at <string>:")
    assert KEY not in done.stdout + done.stderr
    assert ("Traceback" in done.stderr) is debug
    if debug:
        assert f"RuntimeError: unexpected, near {REDACTION}" in done.stderr


def test_a_child_process_map_with_a_missing_items_file_is_one_object(tmp_path: Path) -> None:
    program = "from enmaas_cli.cli import entry; entry()"
    done = _child(tmp_path, program, *_map_args(tmp_path), "-m", MODEL)
    assert done.returncode == 1, done.stderr
    obj = _one_object(done.stdout)
    assert obj["status"] == "error"
    assert "Traceback" not in done.stderr
