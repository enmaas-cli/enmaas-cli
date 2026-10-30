import re
from pathlib import Path

from enmaas_cli.dialect import Completion, Usage
from enmaas_cli.receipts import ask_receipt, create_run_dir, preview, prune_runs


def test_preview_collapses_whitespace_and_truncates() -> None:
    text = "line one\n\n   line  two\n" + ("x" * 500)
    out = preview(text, limit=40)
    assert "\n" not in out
    assert len(out) == 40
    assert out.startswith("line one line two")


def test_preview_of_empty_text_is_empty() -> None:
    assert preview("", limit=40) == ""


def test_create_run_dir_uses_timestamp_and_random_slug(tmp_path: Path) -> None:
    first = create_run_dir(tmp_path)
    second = create_run_dir(tmp_path)
    assert first.exists() and second.exists()
    assert first != second
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f]{4}", first.name)


def test_prune_removes_old_runs_only(tmp_path: Path) -> None:
    import os
    import time

    old = tmp_path / "2020-01-01T00-00-00-aaaa"
    new = tmp_path / "2030-01-01T00-00-00-bbbb"
    for d in (old, new):
        d.mkdir(parents=True)
    ancient = time.time() - (40 * 86400)
    os.utime(old, (ancient, ancient))
    prune_runs(tmp_path, max_age_days=30)
    assert not old.exists()
    assert new.exists()


def test_prune_failure_is_not_fatal(tmp_path: Path) -> None:
    prune_runs(tmp_path / "does-not-exist", max_age_days=30)


def test_ask_receipt_shows_cached_and_reasoning_tokens(tmp_path: Path) -> None:
    out = tmp_path / "response.md"
    out.write_text("hello world", encoding="utf-8")
    completion = Completion(
        text="hello world",
        reasoning="",
        stop="stop",
        usage=Usage(
            input_tokens=10265, cached_tokens=9216, output_tokens=1430, reasoning_tokens=1088
        ),
    )
    text = ask_receipt(
        model="rits/zai-org/glm-5-3",
        dialect="openai",
        completion=completion,
        path=out,
        elapsed=6.2,
    )
    assert "enmaas ask ok" in text
    assert "10,265 (9,216 cached)" in text
    assert "out 1,430" in text
    assert "reasoning 1,088" in text
    assert "6.2s" in text
    assert "hello world" in text


def test_ask_receipt_reports_unknown_not_zero(tmp_path: Path) -> None:
    out = tmp_path / "response.md"
    out.write_text("hi", encoding="utf-8")
    completion = Completion(text="hi", reasoning="", stop="stop", usage=Usage())
    text = ask_receipt(model="m", dialect="openai", completion=completion, path=out, elapsed=1.0)
    assert "unknown" in text
    assert "in 0" not in text


def test_ask_receipt_flags_truncation(tmp_path: Path) -> None:
    out = tmp_path / "response.md"
    out.write_text("partial", encoding="utf-8")
    completion = Completion(
        text="partial",
        reasoning="",
        stop="length",
        usage=Usage(input_tokens=10, output_tokens=20),
    )
    text = ask_receipt(model="m", dialect="openai", completion=completion, path=out, elapsed=1.0)
    assert "truncated" in text
