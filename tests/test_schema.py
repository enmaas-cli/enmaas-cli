"""Tests for JSON Schema structured output support."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import schema as schema_module
from enmaas_cli.ask import run_ask
from enmaas_cli.dialect import build_request
from enmaas_cli.schema import ValidationError, load_schema, validate_against_schema


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestSchemaRequestBuilding:
    """Test that schemas are correctly added to requests."""

    def test_openai_without_schema_unchanged(self) -> None:
        """OpenAI request without schema is unchanged from today."""
        request = build_request(
            model="gpt-6-sol",
            prompt="hi",
            dialect="openai",
            max_tokens=100,
            thinking=False,
            system=None,
            key="k",
        )
        assert "response_format" not in request.body
        assert "tools" not in request.body
        assert "tool_choice" not in request.body

    def test_anthropic_without_schema_unchanged(self) -> None:
        """Anthropic request without schema is unchanged from today."""
        request = build_request(
            model="claude-opus-5-5",
            prompt="hi",
            dialect="anthropic",
            max_tokens=100,
            thinking=False,
            system=None,
            key="k",
        )
        assert "response_format" not in request.body
        assert "tools" not in request.body
        assert "tool_choice" not in request.body

    def test_openai_with_schema_sets_response_format(self) -> None:
        """OpenAI with schema sets response_format with strict: true."""
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        request = build_request(
            model="gpt-6-sol",
            prompt="hi",
            dialect="openai",
            max_tokens=100,
            thinking=False,
            system=None,
            key="k",
            schema=schema,
            schema_name="Test",
        )
        assert request.body["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "Test",
                "strict": True,
                "schema": schema,
            },
        }
        assert "tools" not in request.body
        assert "tool_choice" not in request.body

    def test_anthropic_with_schema_sets_forced_tool(self) -> None:
        """Anthropic with schema sets tools and tool_choice."""
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        request = build_request(
            model="claude-opus-5-5",
            prompt="hi",
            dialect="anthropic",
            max_tokens=100,
            thinking=False,
            system=None,
            key="k",
            schema=schema,
            schema_name="Test",
        )
        assert "response_format" not in request.body
        assert request.body["tools"] == [{"name": "Test", "input_schema": schema}]
        assert request.body["tool_choice"] == {"type": "tool", "name": "Test"}

    def test_anthropic_schema_produces_warning(self) -> None:
        """Using schema with Anthropic dialect produces advisory warning."""
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        request = build_request(
            model="claude-opus-5-5",
            prompt="hi",
            dialect="anthropic",
            max_tokens=100,
            thinking=False,
            system=None,
            key="k",
            schema=schema,
            schema_name="Test",
        )
        assert len(request.warnings) == 1
        assert "advisory" in request.warnings[0].lower()
        assert "anthropic" in request.warnings[0].lower()


class TestAnthropicToolUseResponseParsing:
    """Test that Anthropic tool_use responses are parsed correctly."""

    def test_tool_use_block_serialized_as_json_text(self) -> None:
        """Anthropic tool_use block input is serialized to JSON text."""
        from enmaas_cli.dialect import parse_response

        data = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Test",
                    "input": {"name": "Alice", "age": 30},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        completion = parse_response(data, "anthropic")
        parsed = json.loads(completion.text)
        assert parsed == {"name": "Alice", "age": 30}

    def test_tool_use_with_no_text_blocks_not_empty(self) -> None:
        """Tool use response with no text blocks does not raise EmptyResponseError."""
        from enmaas_cli.dialect import parse_response

        data = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Test",
                    "input": {"result": "ok"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        completion = parse_response(data, "anthropic")
        assert completion.text == '{"result": "ok"}'


class TestSchemaValidation:
    """Test client-side schema validation."""

    async def test_conforming_output_validates_and_succeeds(self, tmp_path: Path) -> None:
        """Valid output passes validation and succeeds."""
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"name": "Alice"}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                },
            )

        async with _client(handler) as client:
            result = await run_ask(
                client=client,
                base_url="https://g.test",
                key="k",
                model="gpt-6-sol",
                prompt="hi",
                system=None,
                dialect=None,
                thinking=False,
                max_tokens=100,
                timeout=5,
                retries=0,
                output=None,
                run_root=tmp_path,
                save_thinking=False,
                schema_file=schema_file,
                schema_name="Test",
            )
        assert result.exit_code == 0
        assert result.path.read_text() == '{"name": "Alice"}'

    async def test_non_conforming_output_fails_even_on_http_200(self, tmp_path: Path) -> None:
        """Schema-invalid output fails even when HTTP is 200 and JSON is valid."""
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"color": {"type": "string", "enum": ["Zarquon"]}},
                    "required": ["color"],
                }
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"color": "Major"}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                },
            )

        async with _client(handler) as client:
            result = await run_ask(
                client=client,
                base_url="https://g.test",
                key="k",
                model="gpt-6-sol",
                prompt="hi",
                system=None,
                dialect=None,
                thinking=False,
                max_tokens=100,
                timeout=5,
                retries=0,
                output=None,
                run_root=tmp_path,
                save_thinking=False,
                schema_file=schema_file,
                schema_name="Test",
            )
        assert result.exit_code == 1
        assert result.error is not None, "a failed validation must report why"
        assert "schema" in result.error.lower() or "validation" in result.error.lower()
        assert result.path.exists()
        assert result.path.read_text() == '{"color": "Major"}'

    async def test_ask_writes_raw_response_on_schema_failure(self, tmp_path: Path) -> None:
        """ask writes the raw response even when validation fails."""
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(
            json.dumps({"type": "object", "properties": {"x": {"type": "number"}}})
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"x": "not a number"}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                },
            )

        async with _client(handler) as client:
            result = await run_ask(
                client=client,
                base_url="https://g.test",
                key="k",
                model="gpt-6-sol",
                prompt="hi",
                system=None,
                dialect=None,
                thinking=False,
                max_tokens=100,
                timeout=5,
                retries=0,
                output=None,
                run_root=tmp_path,
                save_thinking=False,
                schema_file=schema_file,
                schema_name="Test",
            )
        assert result.exit_code == 1
        assert result.path.read_text() == '{"x": "not a number"}'


class TestSchemaFileValidation:
    """Test that malformed schemas fail before any request."""

    async def test_missing_schema_file_fails_before_request(self, tmp_path: Path) -> None:
        """Missing schema file fails before making any request."""

        call_count = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["count"] += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        async with _client(handler) as client:
            with pytest.raises(ValidationError, match="does not exist"):
                await run_ask(
                    client=client,
                    base_url="https://g.test",
                    key="k",
                    model="gpt-6-sol",
                    prompt="hi",
                    system=None,
                    dialect=None,
                    thinking=False,
                    max_tokens=100,
                    timeout=5,
                    retries=0,
                    output=None,
                    run_root=tmp_path,
                    save_thinking=False,
                    schema_file=tmp_path / "does_not_exist.json",
                    schema_name="Test",
                )
        assert call_count["count"] == 0

    async def test_invalid_json_in_schema_fails_before_request(self, tmp_path: Path) -> None:
        """Schema file with invalid JSON fails before making any request."""
        schema_file = tmp_path / "bad.json"
        schema_file.write_text("{not valid json")

        call_count = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["count"] += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        async with _client(handler) as client:
            with pytest.raises(ValidationError, match="invalid JSON"):
                await run_ask(
                    client=client,
                    base_url="https://g.test",
                    key="k",
                    model="gpt-6-sol",
                    prompt="hi",
                    system=None,
                    dialect=None,
                    thinking=False,
                    max_tokens=100,
                    timeout=5,
                    retries=0,
                    output=None,
                    run_root=tmp_path,
                    save_thinking=False,
                    schema_file=schema_file,
                    schema_name="Test",
                )
        assert call_count["count"] == 0

    async def test_invalid_schema_fails_before_request(self, tmp_path: Path) -> None:
        """Schema that jsonschema rejects fails before making any request."""
        schema_file = tmp_path / "bad_schema.json"
        schema_file.write_text(json.dumps({"type": "not_a_valid_type"}))

        call_count = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["count"] += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        async with _client(handler) as client:
            with pytest.raises(ValidationError, match="not a valid JSON Schema"):
                await run_ask(
                    client=client,
                    base_url="https://g.test",
                    key="k",
                    model="gpt-6-sol",
                    prompt="hi",
                    system=None,
                    dialect=None,
                    thinking=False,
                    max_tokens=100,
                    timeout=5,
                    retries=0,
                    output=None,
                    run_root=tmp_path,
                    save_thinking=False,
                    schema_file=schema_file,
                    schema_name="Test",
                )
        assert call_count["count"] == 0


def test_response_nested_too_deeply_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The depth at which the decoder gives up depends on the Python version
    # and the stack size, so the decoder is made to give up here.
    def deep(text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("enmaas_cli.schema.json.loads", deep)
    with pytest.raises(ValidationError, match="^response is nested too deeply$"):
        schema_module.validate_against_schema("[[1]]", {"type": "array"})


def test_a_response_with_200_000_levels_is_refused_or_read() -> None:
    # The decoder gives up on this document, or reads it when the stack has
    # room. Both are correct: a deep array is still an array.
    try:
        validate_against_schema("[" * 200_000 + "]" * 200_000, {"type": "array"})
    except ValidationError as exc:
        assert str(exc) == "response is nested too deeply"


def test_schema_check_that_recurses_too_deeply_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enmaas_cli import schema as schema_module

    def deep(instance: object, schema: object, **kwargs: object) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("enmaas_cli.schema.jsonschema.validate", deep)
    with pytest.raises(ValidationError, match="^response is nested too deeply$"):
        schema_module.validate_against_schema("[1]", {"type": "array"})


# A schema file can never make enmaas fetch anything.
#
# jsonschema retrieves a reference it cannot find inside the schema, over HTTP
# or from a file. A schema from another party could then make enmaas contact any
# host, outside the gateway rules and timeouts. Only references inside the file
# are allowed, and validation runs with nothing it could retrieve.


@pytest.fixture
def server() -> Iterator[tuple[str, list[str]]]:
    """A local HTTP server that records every request and answers with a schema."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.path)
            body = json.dumps({"type": "string"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{service.server_port}", seen
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=2)


def _write(tmp_path: Path, schema: object) -> Path:
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_a_reference_to_a_server_is_refused_at_load(
    tmp_path: Path, server: tuple[str, list[str]], keyword: str
) -> None:
    url, seen = server
    target = url + "/schema.json"
    path = _write(tmp_path, {"type": "object", "properties": {"a": {keyword: target}}})
    with pytest.raises(ValidationError) as caught:
        load_schema(path)
    assert keyword in str(caught.value)
    assert target not in str(caught.value)
    assert seen == []


@pytest.mark.parametrize(
    "reference", ["file:///etc/passwd", "other.json", "./other.json#/x", "", "//host/x"]
)
def test_any_reference_outside_the_file_is_refused_at_load(tmp_path: Path, reference: str) -> None:
    path = _write(tmp_path, {"allOf": [{"$ref": reference}]})
    with pytest.raises(ValidationError, match=r"\$ref"):
        load_schema(path)


def test_the_review_reference_never_reaches_the_server(
    tmp_path: Path, server: tuple[str, list[str]]
) -> None:
    url, seen = server
    path = _write(tmp_path, {"$ref": url + "/schema.json"})
    with pytest.raises(ValidationError):
        schema = load_schema(path)
        validate_against_schema('"ordinary answer"', schema)
    assert seen == []


def test_references_inside_the_file_still_work(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "$defs": {"count": {"type": "integer"}},
            "definitions": {"name": {"type": "string"}},
            "type": "object",
            "properties": {
                "count": {"$ref": "#/$defs/count"},
                "name": {"$ref": "#/definitions/name"},
            },
        },
    )
    schema = load_schema(path)
    validate_against_schema('{"count": 3, "name": "x"}', schema)
    with pytest.raises(ValidationError, match="does not match schema"):
        validate_against_schema('{"count": "three"}', schema)


def test_a_property_named_like_a_keyword_is_not_a_reference(tmp_path: Path) -> None:
    path = _write(tmp_path, {"type": "object", "properties": {"$ref": {"type": "string"}}})
    schema = load_schema(path)
    validate_against_schema('{"$ref": "text"}', schema)


def test_a_schema_nested_past_the_limit_is_refused(tmp_path: Path) -> None:
    nested: dict[str, object] = {"type": "array"}
    for _ in range(schema_module._MAX_SCHEMA_DEPTH + 1):
        nested = {"type": "array", "items": nested}
    with pytest.raises(ValidationError, match="nested more than"):
        load_schema(_write(tmp_path, nested))
    shallow: dict[str, object] = {"type": "array"}
    for _ in range(schema_module._MAX_SCHEMA_DEPTH // 4):
        shallow = {"type": "array", "items": shallow}
    load_schema(_write(tmp_path, shallow))


@pytest.mark.parametrize("form", ["absolute", "relative-to-id", "file"])
def test_validation_retrieves_nothing_even_without_the_load_check(
    monkeypatch: pytest.MonkeyPatch, server: tuple[str, list[str]], form: str
) -> None:
    url, seen = server
    schemas: dict[str, dict[str, Any]] = {
        "absolute": {"$ref": url + "/schema.json"},
        "relative-to-id": {"$id": url + "/root.json", "$ref": "other.json"},
        "file": {"$ref": "file:///etc/hostname"},
    }
    monkeypatch.setattr(schema_module, "_check_references", lambda schema, path: None)
    with pytest.raises(ValidationError, match="cannot be resolved"):
        validate_against_schema('"ordinary answer"', schemas[form])
    assert seen == []


@pytest.mark.parametrize("key", ["short-secret", "x" * 140 + '/"\\end'])
@pytest.mark.parametrize("required", [False, True])
def test_schema_names_redacted_before_limits(key: str, required: bool) -> None:
    from enmaas_cli.auth import REDACTION

    schema = {"type": "object", "properties": {key: {"type": "integer"}}}
    if required:
        schema["required"] = [key]
    payload = {} if required else {key: "bad"}
    with pytest.raises(ValidationError) as caught:
        validate_against_schema(json.dumps(payload), schema, key=key)
    message = str(caught.value)
    assert REDACTION in message
    assert key[:32] not in message
    assert key not in json.dumps(message)
