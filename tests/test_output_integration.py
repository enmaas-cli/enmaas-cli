"""Protection must precede requests, including resumed and batched work."""

import json
from pathlib import Path

import httpx
import pytest

from enmaas_cli.cli import main, run_ask
from enmaas_cli.outputs import allow_read, is_protected, marker_path, protect
from enmaas_cli.runner import run_map

OK = {
    "choices": [
        {
            "finish_reason": "stop",
            "message": {
                "content": "answer",
                "reasoning_content": "reasoning",
            },
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("schema_invalid", [False, True])
async def test_ask_protects_before_request_and_keeps_failed_output(
    tmp_path: Path,
    explicit: bool,
    schema_invalid: bool,
) -> None:
    root = tmp_path / "runs"
    output = tmp_path / "custom" / "result.md" if explicit else None
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}')

    def handler(request: httpx.Request) -> httpx.Response:
        run_dir = next(root.iterdir())
        answer = output if output is not None else run_dir / "response.md"
        assert is_protected(answer)
        assert is_protected(run_dir / "thinking.md")
        return httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_ask(
            client=client,
            base_url="https://mock.test",
            key="k",
            model="gpt-4o",
            prompt="p",
            system=None,
            dialect="openai",
            thinking=True,
            max_tokens=50,
            timeout=2,
            retries=0,
            output=output,
            run_root=root,
            save_thinking=True,
            schema_file=schema if schema_invalid else None,
        )
    assert result.path.read_text() == "answer"
    assert is_protected(result.path)
    assert result.exit_code == (1 if schema_invalid else 0)
    if not schema_invalid:
        thinking = next(root.iterdir()) / "thinking.md"
        assert thinking.read_text() == "reasoning"
        assert is_protected(thinking)


@pytest.mark.parametrize("batch", [1, 2])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("retry", [False, True])
async def test_map_protects_pending_outputs_and_preserves_skipped_approval(
    tmp_path: Path,
    batch: int,
    resume: bool,
    retry: bool,
) -> None:
    items = tmp_path / "items.jsonl"
    items.write_text('{"id":"a"}\n{"id":"b"}\n')
    template = tmp_path / "template.txt"
    template.write_text("shared\n===PER ITEM===\n{id}")
    outdir = tmp_path / "out"
    outdir.mkdir()
    skipped = resume and not retry
    (outdir / "a.md").write_text("existing")
    (outdir / "results.jsonl").write_text(
        json.dumps({"id": "a", "status": "ok", "replaced": int(retry)}) + "\n"
    )
    protect(outdir / "a.md")
    allow_read(outdir / "a.md")

    def handler(request: httpx.Request) -> httpx.Response:
        assert is_protected(outdir / "b.md")
        assert is_protected(outdir / "a.md") is not skipped
        assert not is_protected(items)
        assert not is_protected(outdir / "results.jsonl")
        if batch > 1:
            ids = ["b"] if skipped else ["a", "b"]
            text = "\n".join(f"=== ANSWER {name} ===\nanswer" for name in ids)
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": text}}],
                },
            )
        return httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await run_map(
            client=client,
            base_url="https://mock.test",
            key="k",
            model="gpt-4o",
            items_path=items,
            template_path=template,
            outdir=outdir,
            concurrency=2,
            retries=0,
            timeout=2,
            thinking=False,
            max_tokens=50,
            dialect="openai",
            resume=resume,
            batch=batch,
        )
    assert outcome.skipped == int(skipped)
    assert outcome.ok == 2 - int(skipped)
    assert is_protected(outdir / "a.md") is not skipped
    if skipped:
        assert (outdir / "a.md").read_text() == "existing"


@pytest.mark.parametrize("command", ["ask", "map"])
@pytest.mark.parametrize("fail_at", [1, 2])
async def test_marker_failure_prevents_all_gateway_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    fail_at: int,
) -> None:
    calls = 0
    replacements = 0
    import os

    original_replace = os.replace

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=OK)

    def fail_replace(source: str, destination: Path) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == fail_at:
            raise PermissionError("marker is read only")
        original_replace(source, destination)

    monkeypatch.setattr("enmaas_cli.outputs.os.replace", fail_replace)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OSError, match="marker"):
            if command == "ask":
                await run_ask(
                    client=client,
                    base_url="https://mock.test",
                    key="k",
                    model="gpt-4o",
                    prompt="p",
                    system=None,
                    dialect="openai",
                    thinking=False,
                    max_tokens=50,
                    timeout=2,
                    retries=0,
                    output=None,
                    run_root=tmp_path / "runs",
                    save_thinking=True,
                )
            else:
                items = tmp_path / "items.jsonl"
                items.write_text('{"id":"a"}\n{"id":"b"}\n')
                template = tmp_path / "template.txt"
                template.write_text("{id}")
                await run_map(
                    client=client,
                    base_url="https://mock.test",
                    key="k",
                    model="gpt-4o",
                    items_path=items,
                    template_path=template,
                    outdir=tmp_path / "out",
                    concurrency=1,
                    retries=0,
                    timeout=2,
                    thinking=False,
                    max_tokens=50,
                    dialect="openai",
                    resume=False,
                )
    assert calls == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["inspect", "a", "b"],
        ["inspect", "a", "--max-chars", "0"],
        ["inspect", "a", "--max-chars", "2001"],
        ["inspect", "a", "--offset", "1"],
        ["inspect", "a", "--full"],
        ["allow-read", "a", "b"],
    ],
)
def test_local_commands_reject_unbounded_or_multiple_reads(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 1


def test_local_commands_bypass_config_keys_and_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answer = tmp_path / "answer.md"
    answer.write_text("answer" * 1000)
    marker = marker_path(answer)
    marker.parent.mkdir()
    marker.write_text(json.dumps({"version": 1, "basename": answer.name, "state": "protected"}))

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("local command loaded config, credentials, or network")

    for target in ("load_config", "resolve_key"):
        monkeypatch.setattr(f"enmaas_cli.cli.{target}", unexpected)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", unexpected)
    assert main(["inspect", str(answer), "--max-chars", "80"]) == 0
    out = capsys.readouterr().out
    assert len(out) <= 80
    assert out.endswith("\n")
    assert "truncated" in out
    assert main(["allow-read", str(answer)]) == 0
    assert not is_protected(answer)
    assert main(["allow-read", str(answer)]) == 0
    assert capsys.readouterr().out == ""
    allow_read(answer)


def test_record_works_without_any_key_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENMAAS_KEY", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert (
        main(
            [
                "--key-file",
                str(tmp_path / "absent-key"),
                "record",
                "--model",
                "m",
                "--command",
                "other",
                "--input",
                "1",
                "--output",
                "2",
            ]
        )
        == 0
    )
    ledger = tmp_path / "enmaas" / "runs" / "usage.jsonl"
    assert json.loads(ledger.read_text())["command"] == "other"
