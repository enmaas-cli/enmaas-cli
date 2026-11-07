"""Validation diagnostics must not print a rejected answer into context."""

import json
from pathlib import Path
from typing import Any

import pytest

from enmaas_cli import cli
from enmaas_cli.outputs import is_protected
from enmaas_cli.schema import ValidationError, validate_against_schema


def test_schema_error_omits_instance_and_bounds_field_names() -> None:
    field = "private-field-" + "x" * 5000
    secret = "REJECTED_ANSWER_CONTENT_" + "y" * 20000
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    with pytest.raises(ValidationError) as caught:
        validate_against_schema(json.dumps({field: secret}), schema)
    message = str(caught.value)
    assert "REJECTED_ANSWER_CONTENT_" not in message
    assert len(message) < 300
    assert "type" in message


@pytest.mark.parametrize("as_json", [False, True])
def test_schema_failure_reports_saved_path_without_response_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    secret = "REJECTED_ANSWER_CONTENT_" + "z" * 20000
    body = json.dumps({"answer": secret})
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
            }
        )
    )
    output = tmp_path / "answer.json"
    monkeypatch.setenv("ENMAAS_KEY", "local-fixture-key")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://g.test")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": body}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(cli, "call", fake_call)
    args = [
        "--config",
        str(tmp_path / "no-config.toml"),
        "ask",
        "-p",
        "fixture",
        "-m",
        "rits/local-fixture",
        "--schema",
        str(schema),
        "-o",
        str(output),
    ]
    code = cli.main(args + (["--json"] if as_json else []))
    captured = capsys.readouterr()
    assert code == 1
    assert "REJECTED_ANSWER_CONTENT_" not in captured.out + captured.err
    assert output.read_text() == body
    assert is_protected(output)
    if as_json:
        assert json.loads(captured.out)["path"] == str(output)
    else:
        assert str(output) in captured.err
