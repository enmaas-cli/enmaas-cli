"""Tests for JSON Schema structured output support."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import run_ask
from enmaas_cli.dialect import build_request
from enmaas_cli.schema import ValidationError


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
