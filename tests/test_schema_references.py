"""A schema file can never make enmaas fetch anything.

jsonschema retrieves a reference it cannot find inside the schema, over HTTP
or from a file. A schema from another party could then make enmaas contact any
host, outside the gateway rules and timeouts. Only references inside the file
are allowed, and validation runs with nothing it could retrieve.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from enmaas_cli import schema as schema_module
from enmaas_cli.schema import ValidationError, load_schema, validate_against_schema


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
