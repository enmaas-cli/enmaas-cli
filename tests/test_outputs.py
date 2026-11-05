"""Exact output markers and bounded local reads."""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from enmaas_cli.outputs import (
    MARKER_DIR,
    allow_read,
    inspect_file,
    is_protected,
    marker_path,
    protect,
    state_root,
)


def test_marker_tracks_only_one_canonical_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = tmp_path / "answer.md"
    alias = tmp_path / "alias.md"
    alias.symlink_to(answer)
    monkeypatch.chdir(tmp_path)
    protect(Path("alias.md"))
    marker = tmp_path / MARKER_DIR / (hashlib.sha256(b"answer.md").hexdigest() + ".json")
    assert marker_path(alias) == marker
    assert json.loads(marker.read_text()) == {
        "version": 1,
        "basename": "answer.md",
        "state": "protected",
    }
    assert is_protected(answer)
    assert is_protected(Path("alias.md"))
    for name in ("source.py", "usage.jsonl", "results.jsonl", "neighbor.md"):
        assert not is_protected(tmp_path / name)


def test_independent_concurrent_records_survive(tmp_path: Path) -> None:
    paths = [tmp_path / f"answer-{i}.md" for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(protect, paths))
    assert all(is_protected(path) for path in paths)
    assert len(list((tmp_path / MARKER_DIR).iterdir())) == 20


def test_directory_moves_keep_markers_and_file_copies_do_not(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    answer = original / "answer.md"
    answer.write_text("answer")
    protect(answer)
    moved = original.rename(tmp_path / "moved")
    assert is_protected(moved / answer.name)
    copied = tmp_path / "copy.md"
    copied.write_text((moved / answer.name).read_text())
    assert not is_protected(copied)


def test_symlink_and_relative_exceptions_use_the_same_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    answer = actual / "answer.md"
    answer.write_text("answer")
    directory_alias = tmp_path / "alias"
    directory_alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    protect(Path("alias/answer.md"))
    allow_read(Path("alias/answer.md"))
    assert not is_protected(answer)
    assert not is_protected(Path("alias/answer.md"))
    protect(answer)
    assert is_protected(Path("alias/answer.md"))


@pytest.mark.parametrize("name", ["response.md", "thinking.md"])
def test_legacy_layout_and_exception_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    answer = state_root() / "old-run" / name
    answer.parent.mkdir(parents=True)
    answer.write_text("old response")
    assert is_protected(answer)
    assert not is_protected(state_root() / name)
    assert not is_protected(answer.parent / "nested" / name)
    assert not is_protected(answer.parent / "usage.jsonl")
    assert not is_protected(tmp_path / "other" / name)
    allow_read(answer)
    allow_read(answer)
    assert not is_protected(answer)
    protect(answer)
    assert is_protected(answer)


@pytest.mark.parametrize(
    "record",
    [
        "{",
        "[]",
        "{}",
        '{"version":2,"basename":"answer.md","state":"allowed"}',
        '{"version":1,"basename":"other.md","state":"allowed"}',
        '{"version":1,"basename":"answer.md","state":"unknown"}',
        '{"version":true,"basename":"answer.md","state":"allowed"}',
    ],
)
def test_invalid_exact_marker_stays_protected(tmp_path: Path, record: str) -> None:
    answer = tmp_path / "answer.md"
    protect(answer)
    marker_path(answer).write_text(record)
    assert is_protected(answer)


def test_unreadable_marker_stays_protected(tmp_path: Path) -> None:
    answer = tmp_path / "answer.md"
    protect(answer)
    marker_path(answer).unlink()
    marker_path(answer).mkdir()
    assert is_protected(answer)


def test_oversized_and_deeply_nested_markers_stay_protected(tmp_path: Path) -> None:
    answer = tmp_path / "answer.md"
    protect(answer)
    marker_path(answer).write_bytes(b"[" * 1200 + b"]" * 1200)
    assert is_protected(answer)
    marker_path(answer).write_bytes(b" " * 5000)
    assert is_protected(answer)


def test_pipe_marker_is_protected_without_waiting(tmp_path: Path) -> None:
    answer = tmp_path / "answer.md"
    protect(answer)
    marker = marker_path(answer)
    marker.unlink()
    os.mkfifo(marker)
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from pathlib import Path; from enmaas_cli.outputs import is_protected; "
            "print(is_protected(Path(__import__('sys').argv[1])))",
            str(answer),
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    assert result.stdout == "True\n"


def test_allow_requires_an_existing_protected_file(tmp_path: Path) -> None:
    answer = tmp_path / "answer.md"
    protect(answer)
    with pytest.raises(OSError):
        allow_read(answer)
    answer.write_text("response")
    other = tmp_path / "source.py"
    other.write_text("source")
    with pytest.raises(ValueError, match="protected"):
        allow_read(other)
    allow_read(answer)
    assert not is_protected(answer)


def test_atomic_replace_failure_retains_prior_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = tmp_path / "answer.md"
    answer.write_text("response")
    protect(answer)

    def fail_replace(source: str, destination: Path) -> None:
        raise PermissionError("read only")

    monkeypatch.setattr("enmaas_cli.outputs.os.replace", fail_replace)
    with pytest.raises(OSError):
        allow_read(answer)
    assert is_protected(answer)
    assert len(list(marker_path(answer).parent.iterdir())) == 1


@pytest.mark.parametrize("limit", [1, 2, 12, 80, 2000])
def test_inspection_caps_multibyte_text(tmp_path: Path, limit: int) -> None:
    answer = tmp_path / "huge.md"
    with answer.open("wb") as handle:
        handle.write(("🙂" * 3000).encode())
        handle.truncate(100_000_000)
    result = inspect_file(answer, max_chars=limit)
    assert len(result) <= limit
    assert result.endswith("\n")
    if limit >= 80:
        assert "🙂" in result
        assert "truncated" in result


def test_inspection_reads_only_a_bounded_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = tmp_path / "answer.md"
    answer.write_text("small response")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("unbounded read"))
    assert inspect_file(answer) == "small response\n"
    answer.write_bytes(b"bad\xff\n")
    assert inspect_file(answer) == "bad\ufffd\n"


def test_inspection_has_a_fixed_byte_read_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = tmp_path / "large.md"
    answer.write_bytes(b"x" * 100_000)
    original_read = os.read
    reads: list[int] = []

    def bounded_read(descriptor: int, size: int) -> bytes:
        reads.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr("enmaas_cli.outputs.os.read", bounded_read)
    assert len(inspect_file(answer, max_chars=80)) == 80
    assert reads == [324]


@pytest.mark.parametrize("limit", [0, -1, 2001])
def test_inspection_rejects_invalid_caps(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError):
        inspect_file(tmp_path / "missing", max_chars=limit)


def test_inspection_rejects_directories_and_pipes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        inspect_file(tmp_path)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(ValueError, match="regular file"):
        inspect_file(pipe)
