"""A key that cannot be sent in a header never shows, in whole or in part.

A key pasted with a newline and a second line, or holding a tab, a carriage
return or a non-ASCII letter, is refused before any request. No piece of it
of four characters or more may reach stdout, stderr, the --json object or
any file under the state directory, in plain, JSON-escaped or Python repr
form.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import main

MODEL = "rits/zai-org/glm-5-3"
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}
# Each part is made of characters no message of this tool holds in a row.
KEYS = {
    "newline": "Qz7Kx9Vw\nJh3Lp8Rt",
    "carriage return": "Qz7Kx9Vw\rJh3Lp8Rt",
    "tab": "Qz7Kx9Vw\tJh3Lp8Rt",
    "non-ASCII letter": "Qz7Kx9VwéJh3Lp8Rt",
    "second line": "Qz7Kx9VwJh3Lp8Rt\nWq4Zm6Yc2Nb5",
}
FRAGMENT = 4


def _fragments(key: str) -> set[str]:
    """Every run of FRAGMENT characters of the key, as written, escaped or quoted."""
    forms = {key, json.dumps(key)[1:-1], json.dumps(key, ensure_ascii=False)[1:-1], repr(key)}
    pieces: set[str] = set()
    for form in forms:
        pieces.update(form[start : start + FRAGMENT] for start in range(len(form) - FRAGMENT + 1))
    return pieces


def _leaks(text: str, key: str) -> list[str]:
    return sorted(piece for piece in _fragments(key) if piece in text)


def _command(name: str, tmp_path: Path) -> list[str]:
    if name == "models":
        return ["models"]
    if name == "ask":
        return ["ask", "-m", MODEL, "--dialect", "openai", "-p", "hi", "--retries", "0"]
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n', encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    return ["map", "-m", MODEL, "--dialect", "openai", "--retries", "0"] + [
        "--items",
        str(items),
        "--template",
        str(template),
        "--outdir",
        str(tmp_path / "out"),
    ]


def _state_text(tmp_path: Path) -> str:
    """Everything under the state directory and the map output, as text."""
    parts: list[str] = []
    for root in (tmp_path / "xdg", tmp_path / "out"):
        if root.exists():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    parts.append(path.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(parts)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("command", ["models", "ask", "map"])
@pytest.mark.parametrize("source", ["environment", "file"])
@pytest.mark.parametrize("shape", sorted(KEYS))
def test_no_fragment_of_an_unsendable_key_is_shown_or_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
    source: str,
    command: str,
    as_json: bool,
) -> None:
    key = KEYS[shape]
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    monkeypatch.delenv("ENMAAS_DEBUG", raising=False)
    argv = ["--config", str(tmp_path / "none.toml")]
    if source == "environment":
        monkeypatch.setenv("ENMAAS_KEY", key)
    else:
        monkeypatch.delenv("ENMAAS_KEY", raising=False)
        key_file = tmp_path / "keyfile"
        key_file.write_text(key, encoding="utf-8")
        key_file.chmod(0o600)
        argv += ["--key-file", str(key_file)]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": MODEL}]})
        return httpx.Response(200, json=REPLY)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    argv += _command(command, tmp_path)
    if as_json:
        argv.append("--json")

    code = main(argv)

    captured = capsys.readouterr()
    assert code != 0
    assert seen == []
    assert _leaks(captured.out, key) == []
    assert _leaks(captured.err, key) == []
    assert _leaks(_state_text(tmp_path), key) == []
    if as_json:
        assert json.loads(captured.out)["status"] == "error"
