import importlib.metadata
import json
import os
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import build_parser, main


def test_missing_key_exits_one_with_actionable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ENMAAS_KEY", raising=False)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://g.test")
    code = main(["ask", "-p", "hi"])
    captured = capsys.readouterr()
    assert code == 1
    assert "no EnMaaS key" in captured.err


def test_no_prompt_source_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://g.test")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    code = main(["ask"])
    captured = capsys.readouterr()
    assert code == 1
    assert "no prompt" in captured.err.lower()


def test_unknown_model_dialect_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://g.test")
    code = main(["ask", "-m", "llama-4", "-p", "hi"])
    captured = capsys.readouterr()
    assert code == 1
    assert "cannot determine dialect" in captured.err


def test_map_model_is_optional() -> None:
    args = build_parser().parse_args(
        ["map", "--items", "i.jsonl", "--template", "t.txt", "--outdir", "out"]
    )
    assert args.model is None


def test_batch_default_is_unset_until_the_template_is_read() -> None:
    args = build_parser().parse_args(
        ["map", "--items", "i.jsonl", "--template", "t.txt", "--outdir", "out"]
    )
    assert args.batch is None


def test_version_prints_package_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"enmaas {importlib.metadata.version('enmaas-cli')}\n"


def test_version_falls_back_when_package_metadata_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    # cli.package_version looks up importlib.metadata.version at call time.
    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out == "enmaas 0+unknown\n"


def _record_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Route every client the CLI opens to a mock catalog and keep the requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    return seen


def _gateway_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.delenv("ENMAAS_BASE_URL", raising=False)


def test_missing_gateway_url_exits_one_and_names_the_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    seen = _record_requests(monkeypatch)
    config = tmp_path / "custom.toml"
    code = main(["--config", str(config), "models"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == (
        f"error: no gateway URL configured: set base_url in {config}, "
        "set $ENMAAS_BASE_URL, or run ./install.sh --gateway URL\n"
    )
    assert seen == []


def test_missing_gateway_url_names_the_default_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    code = main(["models"])
    assert code == 1
    expected = tmp_path / ".config" / "enmaas" / "config.toml"
    assert f"set base_url in {expected}," in capsys.readouterr().err


def test_environment_url_overrides_config_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    seen = _record_requests(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text('base_url = "https://from-config.test"\n', encoding="utf-8")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://from-env.test")
    code = main(["--config", str(config), "models", "--dialect", "openai"])
    assert code == 0, capsys.readouterr().err
    assert [str(r.url) for r in seen] == ["https://from-env.test/v1/models"]


def test_config_base_url_works_without_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    seen = _record_requests(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text('base_url = "https://from-config.test"\n', encoding="utf-8")
    code = main(["--config", str(config), "models", "--dialect", "openai"])
    assert code == 0, capsys.readouterr().err
    assert [str(r.url) for r in seen] == ["https://from-config.test/v1/models"]


def test_gateway_url_without_a_scheme_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    seen = _record_requests(monkeypatch)
    monkeypatch.setenv("ENMAAS_BASE_URL", "gateway.example.test")
    code = main(["--config", str(tmp_path / "none.toml"), "models"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == (
        "error: gateway URL is not valid (scheme must be http or https): gateway.example.test\n"
    )
    assert seen == []


def test_a_trailing_slash_is_stripped_from_the_gateway_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    seen = _record_requests(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text('base_url = "https://gw.example.test/"\n', encoding="utf-8")
    code = main(["--config", str(config), "models", "--dialect", "openai"])
    assert code == 0, capsys.readouterr().err
    assert [str(r.url) for r in seen] == ["https://gw.example.test/v1/models"]


def test_configured_key_file_is_used_by_gateway_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ENMAAS_KEY")
    seen = _record_requests(monkeypatch)
    key_file = tmp_path / "keys" / "enmaas"
    key_file.parent.mkdir()
    key_file.write_text("sk-oai-configured\n", encoding="utf-8")
    key_file.chmod(0o600)
    config = tmp_path / "config.toml"
    config.write_text(
        f'base_url = "https://gw.example.test"\nkey_file = "{key_file}"\n', encoding="utf-8"
    )
    code = main(["--config", str(config), "models", "--dialect", "openai"])
    assert code == 0, capsys.readouterr().err
    assert seen[0].headers["Authorization"] == "Bearer sk-oai-configured"


def test_usage_needs_no_gateway_url_and_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ENMAAS_KEY")
    code = main(["usage"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "gateway URL" not in captured.err


def test_version_needs_no_gateway_url_and_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.startswith("enmaas ")


def _key_file_config(tmp_path: Path, key_file: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f'base_url = "https://gw.example.test"\nkey_file = "{key_file}"\n', encoding="utf-8"
    )
    return config


def test_configured_key_file_that_is_a_directory_is_an_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ENMAAS_KEY")
    seen = _record_requests(monkeypatch)
    folder = tmp_path / "keydir"
    folder.mkdir()
    config = _key_file_config(tmp_path, str(folder))
    code = main(["--config", str(config), "models"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == f"error: key file is a directory: {folder}\n"
    assert seen == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read a mode 000 file, so the read never fails",
)
def test_unreadable_key_file_is_an_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ENMAAS_KEY")
    seen = _record_requests(monkeypatch)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    key_file = tmp_path / "locked"
    key_file.write_text("sk-oai-locked\n", encoding="utf-8")
    key_file.chmod(0o000)
    try:
        code = main(["--key-file", str(key_file), "models"])
    finally:
        key_file.chmod(0o600)
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == f"error: key file is not readable (Permission denied): {key_file}\n"
    assert seen == []


def test_binary_key_file_is_an_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ENMAAS_KEY")
    seen = _record_requests(monkeypatch)
    key_file = tmp_path / "binary"
    key_file.write_bytes(b"\xff\xfe\x00key")
    key_file.chmod(0o600)
    config = _key_file_config(tmp_path, str(key_file))
    code = main(["--config", str(config), "models", "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "status": "error",
        "error": f"key file is not UTF-8 text: {key_file}",
    }
    assert seen == []


def test_a_failing_configured_key_file_never_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ENMAAS_KEY")
    seen = _record_requests(monkeypatch)
    default = tmp_path / ".secrets" / "enmaas"
    default.parent.mkdir()
    default.write_text("sk-oai-default\n", encoding="utf-8")
    default.chmod(0o600)
    folder = tmp_path / "keydir"
    folder.mkdir()
    config = _key_file_config(tmp_path, str(folder))
    code = main(["--config", str(config), "models"])
    assert code == 1
    assert "key file is a directory" in capsys.readouterr().err
    assert seen == []


def test_unexpandable_configured_key_file_loses_to_the_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    seen = _record_requests(monkeypatch)
    config = _key_file_config(tmp_path, "~nonexistent_user_enmaas/key")
    code = main(["--config", str(config), "models", "--dialect", "openai"])
    assert code == 0, capsys.readouterr().err
    assert seen[0].headers["Authorization"] == "Bearer sk-oai-x"


def test_unexpandable_configured_key_file_is_an_auth_error_when_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gateway_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ENMAAS_KEY")
    seen = _record_requests(monkeypatch)
    config = _key_file_config(tmp_path, "~nonexistent_user_enmaas/key")
    code = main(["--config", str(config), "models"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == (
        "error: key_file in config cannot be expanded: ~nonexistent_user_enmaas/key\n"
    )
    assert "Traceback" not in captured.err
    assert seen == []


@pytest.mark.parametrize("as_json", [False, True])
def test_an_empty_answer_still_charges_the_tokens_it_burned(
    as_json: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run that spends max_tokens on reasoning is billed like any other.

    Recording it as zero would let the most expensive failure slip past the
    rolling budget.
    """
    _gateway_env(tmp_path, monkeypatch)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 1000},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    argv = ["--config", str(tmp_path / "none.toml"), "ask", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "-p", "hi", "--retries", "0"]
    if as_json:
        argv.append("--json")
    code = main(argv)
    captured = capsys.readouterr()
    assert code == 1
    ledger = tmp_path / "state" / "enmaas" / "runs" / "usage.jsonl"
    events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    assert events[0]["output_tokens"] == 1000
    assert events[0]["input_tokens"] == 40
    assert events[0]["charge"] == 1040
    assert events[0]["error_kind"] == "EmptyResponseError"
    if as_json:
        payload = json.loads(captured.out)
        assert payload["status"] == "error"
        assert payload["tokens"]["output"] == 1000
        assert payload["tokens"]["input"] == 40
