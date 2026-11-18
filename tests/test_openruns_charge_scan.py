"""An index scan counts only whole charge rows of its own run, at a bounded cost.

A forged open-run record can point the scan at any index. The scan matches
each line against one pattern built from the row the runner writes for that
run, so a line that is not exactly such a row costs no Python work and is
never parsed as JSON.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from enmaas_cli import openruns
from enmaas_cli.openruns import OpenRun
from enmaas_cli.runner import _charge_row
from enmaas_cli.tokens import MAX_RECORDED

RUN_ID = "0123456789abcdef"
# The largest item count the pattern takes. A request never carries more
# items than a run can hold in memory.
MAX_ITEMS = 10**10 - 1


def _run(outdir: Path, run_id: str = RUN_ID) -> OpenRun:
    return OpenRun(
        run_id=run_id,
        outdir=str(outdir),
        offset=0,
        started="2026-11-10T00:00:00+00:00",
        pid=1,
        model="m",
        dialect="openai",
    )


@pytest.mark.parametrize(
    "run_id", [RUN_ID, "0" * 16, "f" * 64, 'quote" back\\slash', "café 日本", "0"]
)
def test_every_row_the_runner_writes_matches_its_runs_pattern(run_id: str) -> None:
    pattern = openruns.charge_pattern(run_id).regex
    for charge in (-5, 0, 1, MAX_RECORDED, MAX_RECORDED * 10):
        for items in (1, MAX_ITEMS):
            line = _charge_row(charge, items=items, run=run_id).encode("utf-8")
            match = pattern.fullmatch(line)
            assert match is not None, line
            assert int(match.group(1)) == max(0, min(charge, MAX_RECORDED))


def test_a_row_of_another_run_or_of_no_run_does_not_match() -> None:
    pattern = openruns.charge_pattern(RUN_ID).regex
    for row in (
        _charge_row(5, items=1, run="fedcba9876543210"),
        _charge_row(5, items=1, run=RUN_ID + "0"),
        _charge_row(5, items=1),
    ):
        assert pattern.fullmatch(row.encode("utf-8")) is None


def test_only_whole_rows_of_this_run_are_counted(tmp_path: Path) -> None:
    index = tmp_path / "results.jsonl"
    lines = [
        _charge_row(5, items=1, run=RUN_ID),
        _charge_row(100, items=1, run="other"),
        _charge_row(200, items=1),
        json.dumps({"type": "request", "charge": 300, "items": 1, "run": RUN_ID, "pad": "x"}),
        json.dumps({"charge": 400, "type": "request", "items": 1, "run": RUN_ID}),
        '{"type":"request","charge":500,"items":1,"run":"' + RUN_ID + '"}',
        " " + _charge_row(600, items=1, run=RUN_ID),
        _charge_row(MAX_RECORDED, items=2, run=RUN_ID),
        '{"type": "request", "charge": 9999999999999999999, "items": 1, "run": "' + RUN_ID + '"}',
        '{"id": "a", "status": "ok"}',
        _charge_row(7, items=3, run=RUN_ID),
    ]
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    read = openruns.charge_since(_run(tmp_path))
    assert read.charge == min(5 + MAX_RECORDED + 7, MAX_RECORDED)
    assert read.complete


def test_a_last_row_without_a_newline_is_counted(tmp_path: Path) -> None:
    index = tmp_path / "results.jsonl"
    index.write_text(
        _charge_row(5, items=1, run=RUN_ID) + "\n" + _charge_row(6, items=1, run=RUN_ID),
        encoding="utf-8",
    )
    assert openruns.charge_since(_run(tmp_path)).charge == 11


def test_no_line_is_parsed_as_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = tmp_path / "results.jsonl"
    rows = [_charge_row(3, items=1, run=RUN_ID), '{"type": "request"}', '{"id": "a"}']
    index.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def loads(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a line was parsed as JSON")

    monkeypatch.setattr(json, "loads", loads)
    assert openruns.charge_since(_run(tmp_path)).charge == 3


@pytest.mark.parametrize(
    "unit",
    [b'{"type": "request"\n', b'{"type": "request"}\n'],
    ids=["prefix only", "bare request object"],
)
def test_a_16_mib_index_of_request_lines_is_read_in_under_2_seconds(
    tmp_path: Path, unit: bytes
) -> None:
    index = tmp_path / "results.jsonl"
    index.write_bytes(unit * ((16 << 20) // len(unit)))
    started = time.process_time()
    read = openruns.charge_since(_run(tmp_path))
    assert time.process_time() - started < 2
    assert read.charge == 0
    assert read.complete


def test_rows_cut_across_chunks_are_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = tmp_path / "results.jsonl"
    rows = [_charge_row(n, items=1, run=RUN_ID) for n in range(1, 41)]
    index.write_text("\n".join(rows) + "\n", encoding="utf-8")
    for size in (1, 2, 7, 31, 64, 65, 200):
        monkeypatch.setattr(openruns, "READ_CHUNK_BYTES", size)
        assert openruns.charge_since(_run(tmp_path)).charge == sum(range(1, 41)), size


def test_a_huge_line_is_read_in_flat_memory(tmp_path: Path) -> None:
    import tracemalloc

    index = tmp_path / "results.jsonl"
    with index.open("wb") as handle:
        handle.write(_charge_row(4, items=1, run=RUN_ID).encode() + b"\n")
        handle.write(b'{"type": "request", "charge": 1' + b"0" * (8 << 20) + b"\n")
        handle.write(_charge_row(5, items=1, run=RUN_ID).encode() + b"\n")
    tracemalloc.start()
    try:
        read = openruns.charge_since(_run(tmp_path))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert read.charge == 9
    assert peak < 1 << 20
    assert os.path.getsize(index) > 8 << 20
