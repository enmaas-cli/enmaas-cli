"""Tests for JSON Schema support in map command."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from enmaas_cli.runner import run_map


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_map_schema_invalid_item_recorded_as_failed(tmp_path: Path) -> None:
    """map records a schema-invalid item as failed in results.jsonl."""
    items_file = tmp_path / "items.jsonl"
    items_file.write_text('{"id": "one", "prompt": "hi"}\n')

    template_file = tmp_path / "template.txt"
    template_file.write_text("{prompt}")

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

    outdir = tmp_path / "out"

    call_count = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["count"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"wrong": "field"}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    async with _client(handler) as client:
        outcome = await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
            model="gpt-6-sol",
            items_path=items_file,
            template_path=template_file,
            outdir=outdir,
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=100,
            dialect=None,
            resume=False,
            schema_file=schema_file,
            schema_name="Test",
        )

    assert outcome.failed == 1
    assert outcome.ok == 0
    assert call_count["count"] == 1

    rows = [
        json.loads(line)
        for line in (outdir / "results.jsonl").read_text().splitlines()
        if line.strip()
    ]
    # One charge row follows the item row, and its charge is nonzero: a
    # response the schema rejects still cost the tokens that produced it.
    charges = [r for r in rows if r.get("type") == "request"]
    assert len(charges) == 1
    assert charges[0]["charge"] > 0
    result_line = next(r for r in rows if r.get("type") != "request")
    assert result_line["id"] == "one"
    assert result_line["status"] == "error"
    assert "schema" in result_line["error"].lower() or "validation" in result_line["error"].lower()


async def test_map_schema_invalid_counts_toward_circuit_breaker(tmp_path: Path) -> None:
    """map counts schema failures toward the circuit breaker."""
    items_file = tmp_path / "items.jsonl"
    items_file.write_text("\n".join(f'{{"id": "item{i}"}}' for i in range(10)))

    template_file = tmp_path / "template.txt"
    template_file.write_text("test")

    schema_file = tmp_path / "schema.json"
    schema_file.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"valid": {"type": "boolean"}},
                "required": ["valid"],
            }
        )
    )

    outdir = tmp_path / "out"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"invalid": true}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    async with _client(handler) as client:
        outcome = await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
            model="gpt-6-sol",
            items_path=items_file,
            template_path=template_file,
            outdir=outdir,
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=100,
            dialect=None,
            resume=False,
            schema_file=schema_file,
            schema_name="Test",
        )

    assert outcome.abort_reason == "breaker"
    assert outcome.attempted < 10
