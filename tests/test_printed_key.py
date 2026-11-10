"""A key that the gateway sends back is never printed.

A reply or a catalog id can echo the key, as typed or in its JSON escaped
form. The receipt preview and both `models` outputs remove it before the
text is made printable and cut, so no part of the key reaches stdout or
stderr. The answer file keeps the bytes the model sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.auth import REDACTION
from enmaas_cli.cli import main
from enmaas_cli.receipts import PREVIEW_LIMIT, preview

MODEL = "rits/zai-org/glm-5-3"
# A quote and a backslash, so the JSON escaped form differs from the key.
KEY = 'Qz8"Jx\\Wv3Kp9Lm'
ESCAPED = json.dumps(KEY)[1:-1]
CONFIG = ["--config", "/nonexistent/enmaas-config.toml"]
ASK = ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"]


def _parts(text: str) -> list[str]:
    """Every piece of four or more characters of the key, or of its escaped form, in `text`."""
    found = []
    for form in (KEY, ESCAPED):
        for start in range(len(form) - 3):
            piece = form[start : start + 4]
            if piece in text:
                found.append(piece)
    return found


def _gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str = "",
    catalog: list[str] | None = None,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", KEY)
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


# The key at the start, and the key across the preview's cut point.
REPLIES = {
    f"{form_name} {place}": text
    for form_name, form in (("typed", KEY), ("escaped", ESCAPED))
    for place, text in (
        ("at the start", f"{form} is the key"),
        ("across the cut", "x" * (PREVIEW_LIMIT - 5) + form + " end"),
    )
}


@pytest.mark.parametrize("reply", sorted(REPLIES))
def test_preview_removes_the_key_before_the_cut(reply: str) -> None:
    head = preview(REPLIES[reply], key=KEY)
    assert _parts(head) == []
    assert REDACTION in head


@pytest.mark.parametrize("reply", sorted(REPLIES))
@pytest.mark.parametrize("as_json", [False, True])
def test_an_ask_never_prints_a_key_the_reply_echoes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reply: str,
    as_json: bool,
) -> None:
    _gateway(tmp_path, monkeypatch, REPLIES[reply])
    output = tmp_path / "answer.md"
    argv = [*CONFIG, *ASK, "-o", str(output), *(["--json"] if as_json else [])]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert _parts(captured.out) == []
    assert _parts(captured.err) == []
    if not as_json:
        (line,) = [line for line in captured.out.splitlines() if line.startswith("  preview")]
        assert REDACTION in line
    assert output.read_text(encoding="utf-8") == REPLIES[reply]


CATALOG = [f"m-{KEY}", f"n-{ESCAPED}-x"]


def test_the_models_list_never_prints_a_key_in_a_catalog_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch, catalog=CATALOG)
    assert main([*CONFIG, "models", "--dialect", "openai"]) == 0
    captured = capsys.readouterr()
    assert _parts(captured.out) == []
    assert _parts(captured.err) == []
    names = [line.split()[0] for line in captured.out.splitlines()]
    assert names == [f"m-{REDACTION}", f"n-{REDACTION}-x"]


def test_the_models_json_never_holds_a_key_in_a_catalog_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway(tmp_path, monkeypatch, catalog=CATALOG)
    assert main([*CONFIG, "models", "--dialect", "openai", "--json"]) == 0
    captured = capsys.readouterr()
    rows = json.loads(captured.out)
    # The decoded ids, because JSON encoding escapes the quote in the key and
    # would hide it from a check on the raw text.
    assert [row["id"] for row in rows] == [f"m-{REDACTION}", f"n-{REDACTION}-x"]
    assert _parts(captured.out) == []
    assert _parts(captured.err) == []
