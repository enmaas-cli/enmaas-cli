"""One bad reply or one internal error fails its own items, never the whole run.

Each run has three items. The second request gets the bad reply, so the other
items must still complete. Every case runs once with one item per request and
once with a batched template.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import batching, outputs, runner
from enmaas_cli.cli import main
from enmaas_cli.receipts import map_receipt
from enmaas_cli.runner import MapOutcome, run_map

IDS = ["a", "b", "c"]
SINGLE = "single"
BATCHED = "batched"
# Which items the second request covers. Batched groups are [a, b] and [c].
SECOND = {SINGLE: {"b"}, BATCHED: {"c"}}
GOOD_USAGE = {"prompt_tokens": 5, "completion_tokens": 2}
BAD_USAGE = {"prompt_tokens": 30, "completion_tokens": 4}

# A reply builder gets the ids the request covers and whether the run is batched.
ReplyFn = Callable[[list[str], bool], httpx.Response]


def _ids_in(request: httpx.Request) -> list[str]:
    prompt = request.content.decode("utf-8")
    batched = re.findall(r"=== ITEM (\w+) ===", prompt)
    return batched or re.findall(r"do (\w+)", prompt)[:1]


def _answer(ids: list[str], batched: bool) -> str:
    if not batched:
        return f"done {ids[0]}"
    return "\n".join(f"=== ANSWER {i} ===\ndone {i}" for i in ids)


def _good(ids: list[str], batched: bool) -> httpx.Response:
    content = _answer(ids, batched)
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": GOOD_USAGE,
        },
    )


def _handler(bad: ReplyFn | None, batched: bool) -> httpx.MockTransport:
    seen = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        ids = _ids_in(request)
        if seen == 2 and bad is not None:
            return bad(ids, batched)
        return _good(ids, batched)

    return httpx.MockTransport(handle)


def _files(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    items = tmp_path / "items.jsonl"
    items.write_text("\n".join(json.dumps({"id": i, "x": i}) for i in IDS) + "\n", encoding="utf-8")
    template = tmp_path / "t.txt"
    if mode == BATCHED:
        template.write_text("shared\n===PER ITEM===\ndo {x}", encoding="utf-8")
    else:
        template.write_text("do {x}", encoding="utf-8")
    return items, template


async def _run(
    tmp_path: Path, mode: str, bad: ReplyFn | None, schema_file: Path | None = None
) -> MapOutcome:
    items, template = _files(tmp_path, mode)
    async with httpx.AsyncClient(transport=_handler(bad, mode == BATCHED)) as client:
        return await run_map(
            client=client,
            base_url="https://g.test",
            key="sk-oai-x",
            model="rits/zai-org/glm-5-3",
            items_path=items,
            template_path=template,
            outdir=tmp_path / "out",
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect="openai",
            resume=False,
            schema_file=schema_file,
            batch=2 if mode == BATCHED else 1,
        )


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _item_rows(tmp_path: Path) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in _rows(tmp_path) if "id" in r}


def _charges(tmp_path: Path) -> list[int]:
    return [r["charge"] for r in _rows(tmp_path) if r.get("type") == "request"]


def _schema(tmp_path: Path) -> Path:
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    return path


def _html(ids: list[str], batched: bool) -> httpx.Response:
    return httpx.Response(200, content=b"<html>proxy</html>", headers={"content-type": "text/html"})


def _json_list(ids: list[str], batched: bool) -> httpx.Response:
    return httpx.Response(200, json=[1, 2])


def _parts_without_text(ids: list[str], batched: bool) -> httpx.Response:
    message = {"content": [{"type": "image_url", "image_url": {"url": "x"}}]}
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "stop", "message": message}], "usage": BAD_USAGE},
    )


def _choices_not_objects(ids: list[str], batched: bool) -> httpx.Response:
    return httpx.Response(200, json={"choices": ["x", 1], "usage": BAD_USAGE})


def _parts_with_text(ids: list[str], batched: bool) -> httpx.Response:
    message = {"content": [{"type": "text", "text": _answer(ids, batched)}]}
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "stop", "message": message}], "usage": BAD_USAGE},
    )


MODES = [SINGLE, BATCHED]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("bad", "charge"),
    [(_html, 0), (_json_list, 0), (_parts_without_text, 34), (_choices_not_objects, 34)],
    ids=["html", "json-list", "parts-without-text", "choices-not-objects"],
)
async def test_bad_reply_fails_only_its_own_items(
    tmp_path: Path, mode: str, bad: ReplyFn, charge: int
) -> None:
    outcome = await _run(tmp_path, mode, bad)
    rows = _item_rows(tmp_path)
    for item_id in IDS:
        expected = "error" if item_id in SECOND[mode] else "ok"
        assert rows[item_id]["status"] == expected, item_id
    assert outcome.ok == 2
    assert outcome.failed == 1
    assert outcome.internal_errors == 0
    assert _charges(tmp_path)[1] == charge
    good_requests = 2 if mode == SINGLE else 1
    assert outcome.charged == 7 * good_requests + charge


@pytest.mark.parametrize("mode", MODES)
async def test_text_parts_reply_completes(tmp_path: Path, mode: str) -> None:
    outcome = await _run(tmp_path, mode, _parts_with_text)
    assert outcome.ok == 3
    assert outcome.failed == 0
    (only,) = SECOND[mode]
    assert (tmp_path / "out" / f"{only}.md").read_text(encoding="utf-8") == f"done {only}"


def _validator_that_breaks_on_b(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate(text: str, schema: dict[str, Any]) -> None:
        if text == "done b":
            raise TypeError("unreviewed text from deep inside a library")

    monkeypatch.setattr(runner, "validate_against_schema", validate)


INTERNAL = re.compile(r"^internal error \(TypeError at test_map_bad_replies\.py:\d+\)$")


@pytest.mark.parametrize("mode", MODES)
async def test_internal_error_in_validation_fails_only_that_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _validator_that_breaks_on_b(monkeypatch)
    outcome = await _run(tmp_path, mode, None, schema_file=_schema(tmp_path))
    rows = _item_rows(tmp_path)
    assert rows["b"]["status"] == "error"
    assert INTERNAL.match(rows["b"]["error"])
    assert rows["a"]["status"] == rows["c"]["status"] == "ok"
    assert outcome.internal_errors == 1
    assert outcome.failed == 1
    assert outcome.ok == 2
    assert dict(outcome.failures)["b"] == rows["b"]["error"]


async def test_internal_error_in_a_request_fails_the_item_and_keeps_its_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = outputs.write_output

    def write(path: Path, text: str) -> None:
        if path.name == "b.md":
            raise TypeError("unreviewed")
        real(path, text)

    monkeypatch.setattr("enmaas_cli.runner.write_output", write)
    outcome = await _run(tmp_path, SINGLE, None)
    rows = _item_rows(tmp_path)
    assert INTERNAL.match(rows["b"]["error"])
    assert outcome.internal_errors == 1
    assert (outcome.ok, outcome.failed) == (2, 1)
    # The reply was parsed and charged before the write failed.
    assert _charges(tmp_path) == [7, 7, 7]


async def test_internal_error_in_a_batched_request_fails_every_item_of_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = batching.split_batch_response

    def split(response: str, ids: list[str]) -> dict[str, str]:
        if ids == ["c"]:
            raise TypeError("unreviewed")
        return real(response, ids)

    monkeypatch.setattr("enmaas_cli.runner.split_batch_response", split)
    outcome = await _run(tmp_path, BATCHED, None)
    rows = _item_rows(tmp_path)
    assert INTERNAL.match(rows["c"]["error"])
    assert rows["a"]["status"] == rows["b"]["status"] == "ok"
    assert outcome.internal_errors == 1
    assert (outcome.ok, outcome.failed) == (2, 1)
    # Charged before the split raised, so the charge row keeps it.
    assert _charges(tmp_path) == [7, 7]


@pytest.mark.parametrize("mode", MODES)
async def test_an_index_that_cannot_be_written_still_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    index = tmp_path / "out" / "results.jsonl"
    real_open = Path.open
    writes = 0

    def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal writes
        if self == index and args and args[0] == "a":
            writes += 1
            if writes == 2:
                raise OSError("index is read-only")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(OSError, match="index is read-only"):
        await _run(tmp_path, mode, None)


def test_receipt_shows_the_internal_count(tmp_path: Path) -> None:
    def outcome(internal: int) -> MapOutcome:
        return MapOutcome(
            ok=2,
            failed=1,
            skipped=0,
            attempted=3,
            total=3,
            abort_reason=None,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            reasoning_tokens=0,
            elapsed=0.0,
            outdir=tmp_path,
            failures=[],
            internal_errors=internal,
        )

    lines = map_receipt(outcome(1), model="m").splitlines()
    items_at = next(n for n, line in enumerate(lines) if line.startswith("  items "))
    assert lines[items_at + 1] == (
        "  internal  1 failed on an error inside enmaas; please report it"
    )
    assert "internal" not in map_receipt(outcome(0), model="m")


@pytest.mark.parametrize("as_json", [True, False])
def test_main_reports_the_internal_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    _validator_that_breaks_on_b(monkeypatch)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: real_client(transport=_handler(None, False))
    )
    items, template = _files(tmp_path, SINGLE)
    argv = ["--config", str(tmp_path / "none.toml"), "map", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "--items", str(items), "--template", str(template)]
    argv += ["--outdir", str(tmp_path / "out"), "--retries", "0", "--concurrency", "1"]
    argv += ["--schema", str(_schema(tmp_path))]
    if as_json:
        argv.append("--json")
    code = main(argv)
    captured = capsys.readouterr()
    assert code == 2
    if as_json:
        payload = json.loads(captured.out)
        assert payload["internal_errors"] == 1
        assert payload["failed"] == 1
    else:
        assert "  internal  1 failed on an error inside enmaas; please report it" in captured.out
