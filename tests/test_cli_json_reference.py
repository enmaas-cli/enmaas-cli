"""The reference documents every field of every --json object, and no other.

Each table in docs/reference.md is compared with the keys of a real object
printed by `main`. A field added to the code without a row, or a row for a
field the code no longer prints, fails here.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.cli import main

REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "reference.md"
MODEL = "rits/zai-org/glm-5-3"
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _documented(heading: str) -> set[str]:
    """The field names in the first column of the table under `heading`."""
    lines = REFERENCE.read_text(encoding="utf-8").split("\n")
    start = lines.index(heading)
    rows: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        if line.startswith("|"):
            rows.append(line)
    assert len(rows) > 2, heading
    # The header row and the separator row come first.
    return {row.split("|")[1].strip().strip("`") for row in rows[2:]}


def _paths(value: Any, prefix: str = "") -> set[str]:
    """Every key path in a JSON value: `a.b` for a nested key, `a[].b` inside a list."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            path = f"{prefix}.{key}" if prefix else key
            found.add(path)
            found |= _paths(inner, path)
    elif isinstance(value, list):
        for item in value:
            found |= _paths(item, f"{prefix}[]")
    return found


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mock gateway that answers every prompt except one naming item `bad`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-reference-key")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if b"bad" in request.content:
            return httpx.Response(400, text="no")
        return httpx.Response(200, json=REPLY)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )


def _object(capsys: pytest.CaptureFixture[str], argv: list[str]) -> dict[str, Any]:
    main(argv)
    value = json.loads(capsys.readouterr().out)
    assert isinstance(value, dict)
    return value


def _budget_config(tmp_path: Path, tokens: int) -> list[str]:
    config = tmp_path / "config.toml"
    config.write_text(f"[budget]\nwindow_days = 7\ntokens = {tokens}\n", encoding="utf-8")
    return ["--config", str(config)]


ASK = ["ask", "-m", MODEL, "--dialect", "openai", "--retries", "0", "--json"]


@pytest.mark.usefixtures("gateway")
def test_the_ask_table_matches_the_ask_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    obj = _object(capsys, ["--config", str(tmp_path / "none.toml"), *ASK, "-p", "hi"])
    assert obj["status"] == "ok"
    assert _documented("### The `ask` object") == _paths(obj)


@pytest.mark.usefixtures("gateway")
def test_the_map_table_matches_the_map_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = tmp_path / "items.jsonl"
    items.write_text(
        json.dumps({"id": "good", "x": "good"}) + "\n" + json.dumps({"id": "bad", "x": "bad"}),
        encoding="utf-8",
    )
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    argv = [*_budget_config(tmp_path, 1_000_000), "map", "-m", MODEL, "--dialect", "openai"]
    argv += ["--items", str(items), "--template", str(template), "--outdir", str(tmp_path / "o")]
    argv += ["--retries", "0", "--token-budget", "1000", "--json"]
    obj = _object(capsys, argv)
    # Both budgets and one failure, so every nested field is present.
    assert obj["status"] == "partial"
    assert obj["budget"]["per_run"] is not None
    assert obj["budget"]["rolling"] is not None
    assert len(obj["failures"]) == 1
    assert _documented("### The `map` object") == _paths(obj)


@pytest.mark.usefixtures("gateway")
def test_the_refusal_table_matches_the_refusal_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _budget_config(tmp_path, 10)
    record = ["record", "--model", MODEL, "--command", "x", "--input", "20", "--output", "0"]
    assert main([*config, *record]) == 0
    obj = _object(capsys, [*config, *ASK, "-p", "hi"])
    assert obj["status"] == "refused"
    assert _documented("### The refusal object") == _paths(obj)


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.usefixtures("gateway")
def test_the_error_table_matches_the_error_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", _Terminal())
    obj = _object(capsys, ["--config", str(tmp_path / "none.toml"), *ASK])
    assert obj["status"] == "error"
    assert _documented("### The error object") == _paths(obj)


def test_the_models_table_matches_the_models_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-reference-key")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    real_client = httpx.AsyncClient
    catalog = {"data": [{"id": MODEL}]}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=catalog))
        ),
    )
    assert main(["--config", str(tmp_path / "none.toml"), "models", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"id": MODEL, "dialects": ["anthropic", "openai"]}]
    assert _documented("### The `models` list") == _paths(rows)
