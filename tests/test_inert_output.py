"""Text a run prints cannot drive the terminal.

A reply, a catalog id or a file name can hold an escape sequence, such as
OSC 52, which asks the terminal to set the clipboard. The receipt preview,
the paths in a receipt, the `models` list and `inspect` print such text with
every control character replaced. The answer file keeps the bytes the model
sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.auth import printable_block
from enmaas_cli.cli import main
from enmaas_cli.receipts import preview

MODEL = "rits/zai-org/glm-5-3"
OSC52 = "\x1b]52;c;SGVsbG8=\x07"
HOSTILE = f"first {OSC52} bell\x07 back\rover \x9b31m end"


def _controls(text: str, allowed: str = "") -> list[str]:
    return sorted({ch for ch in text if not ch.isprintable() and ch not in allowed})


def _gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, catalog: list[str] | None = None
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": i} for i in catalog or []]})
        body: dict[str, Any] = {
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        return httpx.Response(200, json=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )


CONFIG = ["--config", "/nonexistent/enmaas-config.toml"]
ASK = ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\nb", "a\nb"),
        ("a\tb", "a\tb"),
        ("a\rb", "a b"),
        ("a\x1bb", "a?b"),
        ("a\x07b", "a?b"),
        ("a\x9bb", "a?b"),
        ("a‮b", "a?b"),
        ("héllo", "héllo"),
    ],
)
def test_printable_block_keeps_newlines_and_tabs_only(raw: str, expected: str) -> None:
    assert printable_block(raw) == expected


def test_printable_block_output_holds_no_other_control_character() -> None:
    text = "".join(chr(n) for n in range(0x00, 0x300))
    assert _controls(printable_block(text), "\n\t") == []


def test_preview_collapses_whitespace_then_replaces_controls_then_cuts() -> None:
    assert preview("a\n\n b\x1bc", key="k", limit=160) == "a b?c"
    head = preview("\x1b" * 500, key="k", limit=10)
    assert head == "?" * 10


def test_an_ask_receipt_holds_no_control_character_and_the_answer_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch, HOSTILE)
    output = tmp_path / "answer.md"
    assert main([*CONFIG, *ASK, "-o", str(output)]) == 0
    out = capsys.readouterr().out
    assert _controls(out, "\n") == []
    (line,) = [line for line in out.splitlines() if line.startswith("  preview")]
    assert line == "  preview   first ?]52;c;SGVsbG8=? bell? back over ?31m end"
    assert output.read_bytes() == HOSTILE.encode("utf-8")


def test_a_path_in_an_ask_receipt_is_printed_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch, "fine")
    output = tmp_path / "a\x1b]0;title\x07.md"
    assert main([*CONFIG, *ASK, "-o", str(output)]) == 0
    out = capsys.readouterr().out
    assert _controls(out, "\n") == []
    assert f"  output    {tmp_path}/a?]0;title?.md  (4 B)" in out.splitlines()
    assert output.read_text(encoding="utf-8") == "fine"


def test_a_path_in_a_schema_failure_receipt_is_printed_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch, "not json")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    output = tmp_path / "a\x1b[2J.md"
    assert main([*CONFIG, *ASK, "-o", str(output), "--schema", str(schema)]) == 1
    err = capsys.readouterr().err
    assert _controls(err, "\n") == []
    assert f"  output    {tmp_path}/a?[2J.md" in err.splitlines()


def test_paths_in_a_map_receipt_are_printed_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch, HOSTILE)
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n', encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    outdir = tmp_path / "out\x1b]0;t\x07"
    argv = [*CONFIG, "map", "-m", MODEL, "--dialect", "openai", "--retries", "0"]
    argv += ["--items", str(items), "--template", str(template), "--outdir", str(outdir)]
    assert main(argv) == 0
    lines = capsys.readouterr().out.splitlines()
    assert _controls("\n".join(lines), "\n") == []
    assert f"  outdir    {tmp_path}/out?]0;t?" in lines
    assert f"  index     {tmp_path}/out?]0;t?/results.jsonl" in lines
    assert (outdir / "a.md").read_bytes() == HOSTILE.encode("utf-8")


def test_a_catalog_id_with_an_escape_prints_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch, "", catalog=["m" + OSC52, "plain"])
    assert main([*CONFIG, "models", "--dialect", "openai"]) == 0
    out = capsys.readouterr().out
    assert _controls(out, "\n") == []
    assert out.splitlines()[0].startswith("m?]52;c;SGVsbG8=? ")


def test_the_models_json_keeps_the_catalog_id_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch, "", catalog=["m" + OSC52])
    assert main([*CONFIG, "models", "--dialect", "openai", "--json"]) == 0
    out = capsys.readouterr().out
    assert _controls(out, "\n") == []
    assert json.loads(out) == [{"id": "m" + OSC52, "dialects": ["openai"]}]


def test_inspect_prints_no_escape_and_keeps_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "answer.md"
    path.write_text(f"line one {OSC52}\n\tline two\r\n", encoding="utf-8")
    assert main(["inspect", str(path)]) == 0
    out = capsys.readouterr().out
    assert _controls(out, "\n\t") == []
    assert out == "line one ?]52;c;SGVsbG8=?\n\tline two \n"
    assert path.read_bytes() == f"line one {OSC52}\n\tline two\r\n".encode()
