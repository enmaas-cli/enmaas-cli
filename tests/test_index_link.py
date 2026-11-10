"""The map index is never written through a link or into something not a file."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli import openruns
from enmaas_cli.cli import main
from enmaas_cli.runner import _append_index, run_map

OK = {
    "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "x": 1}\n{"id": "b", "x": 2}\n', encoding="utf-8")
    template = tmp_path / "template.txt"
    template.write_text("do {x}", encoding="utf-8")
    return items, template


async def _run(tmp_path: Path, outdir: Path, calls: list[Any], **kw: Any) -> Any:
    items, template = _inputs(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await run_map(
            client=client,
            base_url="https://g.test",
            key="k",
            model="rits/zai-org/glm-5-3",
            items_path=items,
            template_path=template,
            outdir=outdir,
            concurrency=1,
            retries=0,
            timeout=5,
            thinking=False,
            max_tokens=None,
            dialect="openai",
            **kw,
        )


@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
async def test_an_index_that_is_a_link_is_refused_before_any_request(
    tmp_path: Path, resume: bool
) -> None:
    outdir = tmp_path / "out"
    outdir.mkdir()
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep me\n", encoding="utf-8")
    (outdir / "results.jsonl").symlink_to(target)
    calls: list[Any] = []
    with pytest.raises(OSError) as caught:
        await _run(tmp_path, outdir, calls, resume=resume)
    assert str(caught.value) == (
        f"{outdir / 'results.jsonl'} is not a regular file; "
        "enmaas does not write its index through a link"
    )
    assert calls == []
    assert target.read_text(encoding="utf-8") == "keep me\n"


async def test_an_index_that_is_a_directory_is_refused_before_any_request(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "out"
    (outdir / "results.jsonl").mkdir(parents=True)
    calls: list[Any] = []
    with pytest.raises(OSError, match="is not a regular file"):
        await _run(tmp_path, outdir, calls, resume=False)
    assert calls == []


def test_append_index_does_not_follow_a_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep me\n", encoding="utf-8")
    index = tmp_path / "results.jsonl"
    index.symlink_to(target)
    with pytest.raises(OSError):
        _append_index(index, "row\n")
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_append_index_creates_a_private_file_and_appends(tmp_path: Path) -> None:
    index = tmp_path / "results.jsonl"
    old = os.umask(0o022)
    try:
        _append_index(index, "one\n")
        _append_index(index, "two\n")
    finally:
        os.umask(old)
    assert index.read_text(encoding="utf-8") == "one\ntwo\n"
    assert stat.S_IMODE(index.stat().st_mode) == 0o600


def test_map_through_main_refuses_a_linked_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ENMAAS_KEY", "sk-oai-x")
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)
    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=OK)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    items, template = _inputs(tmp_path)
    outdir = tmp_path / "out"
    outdir.mkdir()
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(json.dumps({"type": "request", "charge": 50}) + "\n", encoding="utf-8")
    (outdir / "results.jsonl").symlink_to(target)
    argv = ["--config", str(tmp_path / "none.toml"), "map", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "--items", str(items), "--template", str(template)]
    argv += ["--outdir", str(outdir), "--retries", "0"]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert [line for line in err.splitlines() if line.startswith("error:")] == [
        f"error: {outdir / 'results.jsonl'} is not a regular file; "
        "enmaas does not write its index through a link"
    ]
    assert calls == []
    state = tmp_path / "xdg" / "enmaas" / "runs"
    assert not (state / "usage.jsonl").exists()
    for run in openruns._load(state):
        assert openruns.charge_since(run).charge == 0
