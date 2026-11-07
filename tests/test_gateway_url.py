"""Gateway URL validation and how a setup failure reaches the caller."""

import json
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import main, resolve_base_url

CONFIG = Path("/nonexistent/config.toml")

CHAT_OK = {
    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _resolve(url: str) -> str:
    return resolve_base_url(None, {"ENMAAS_BASE_URL": url}, CONFIG)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("gateway.example.test", "scheme must be http or https"),
        ("ftp://gateway.example.test", "scheme must be http or https"),
        ("localhost:8080", "scheme must be http or https"),
        ("https://", "missing host"),
        ("https:///v1", "missing host"),
        ("https:gateway.example.test", "missing host"),
        ("http://[::1", "missing host"),
        ("https://g.test:bad", "invalid port"),
        ("https://g.test:99999", "invalid port"),
        ("https://g.test?region=eu", "query and fragment are not allowed"),
        ("https://g.test/?", "query and fragment are not allowed"),
        ("https://g.test#top", "query and fragment are not allowed"),
        ("https://user:pass@g.test", "credentials in the URL are not allowed"),
        ("https://user@g.test", "credentials in the URL are not allowed"),
        ("https://g.test/v 1", "whitespace inside the URL"),
        ("https://g.\ttest", "whitespace inside the URL"),
    ],
)
def test_bad_gateway_urls_are_rejected_with_a_reason(url: str, reason: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        _resolve(url)
    assert str(exc_info.value) == f"gateway URL is not valid ({reason}): {url}"


def test_a_bad_configured_url_is_rejected_too() -> None:
    with pytest.raises(ValueError, match=r"\(missing host\): https://$"):
        resolve_base_url("https://", {}, CONFIG)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Host.Example/", "https://Host.Example"),
        ("https://host//", "https://host"),
        ("https://host/v1/", "https://host/v1"),
        ("https://host/v1", "https://host/v1"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("  https://host.example  ", "https://host.example"),
    ],
)
def test_good_gateway_urls_are_normalised(url: str, expected: str) -> None:
    assert _resolve(url) == expected


def test_missing_url_keeps_its_message() -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve_base_url(None, {}, CONFIG)
    assert str(exc_info.value) == (
        f"no gateway URL configured: set base_url in {CONFIG}, "
        "set $ENMAAS_BASE_URL, or run ./install.sh --gateway URL"
    )


def _no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No key anywhere, so a check that ran the key first would fail differently."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ENMAAS_KEY", raising=False)


def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*a: object, **k: object) -> None:
        raise AssertionError("a setup failure must not open a client")

    monkeypatch.setattr(httpx, "AsyncClient", explode)


def _map_args(tmp_path: Path) -> list[str]:
    items = tmp_path / "items.jsonl"
    items.write_text(json.dumps({"id": "a", "x": 1}) + "\n", encoding="utf-8")
    template = tmp_path / "t.txt"
    template.write_text("do {x}", encoding="utf-8")
    return [
        "map",
        "-m",
        "rits/zai-org/glm-5-3",
        "--dialect",
        "openai",
        "--items",
        str(items),
        "--template",
        str(template),
        "--outdir",
        str(tmp_path / "out"),
    ]


COMMANDS = ["models", "ask", "map"]


def _command(name: str, tmp_path: Path) -> list[str]:
    if name == "models":
        return ["models", "--dialect", "openai"]
    if name == "ask":
        return ["ask", "-m", "rits/zai-org/glm-5-3", "--dialect", "openai", "-p", "hi"]
    return _map_args(tmp_path)


@pytest.mark.parametrize("command", COMMANDS)
def test_bad_url_without_a_key_reports_the_url_on_stderr(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_key(tmp_path, monkeypatch)
    _no_client(monkeypatch)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://")
    code = main(["--config", str(tmp_path / "none.toml"), *_command(command, tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == "error: gateway URL is not valid (missing host): https://\n"


@pytest.mark.parametrize("command", COMMANDS)
def test_bad_url_without_a_key_emits_one_json_error_object(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_key(tmp_path, monkeypatch)
    _no_client(monkeypatch)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://g.test:bad")
    argv = ["--config", str(tmp_path / "none.toml"), *_command(command, tmp_path), "--json"]
    code = main(argv)
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "status": "error",
        "error": "gateway URL is not valid (invalid port): https://g.test:bad",
    }


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_url_and_missing_key_reports_the_url(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_key(tmp_path, monkeypatch)
    _no_client(monkeypatch)
    config = tmp_path / "none.toml"
    code = main(["--config", str(config), *_command(command, tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error"].startswith(f"no gateway URL configured: set base_url in {config},")


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_key_with_json_emits_one_json_error_object(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_key(tmp_path, monkeypatch)
    _no_client(monkeypatch)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://g.test")
    argv = ["--config", str(tmp_path / "none.toml"), *_command(command, tmp_path), "--json"]
    code = main(argv)
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error"].startswith("no EnMaaS key found: pass --key-file")


@pytest.mark.parametrize("command", COMMANDS)
def test_environment_url_builds_the_request_url_over_config(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(200, json=CHAT_OK)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    config = tmp_path / "config.toml"
    config.write_text('base_url = "https://from-config.test"\n', encoding="utf-8")
    monkeypatch.setenv("ENMAAS_BASE_URL", "HTTPS://From-Env.test/gw//")
    code = main(["--config", str(config), *_command(command, tmp_path)])
    assert code == 0, capsys.readouterr().err
    expected_path = "/v1/models" if command == "models" else "/v1/chat/completions"
    assert seen
    assert {str(r.url) for r in seen} == {f"https://from-env.test/gw{expected_path}"}
