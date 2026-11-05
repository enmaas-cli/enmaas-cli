import re
from pathlib import Path

from enmaas_cli.dialect import Completion, Usage
from enmaas_cli.receipts import ask_receipt, create_run_dir, map_receipt, preview, prune_runs
from enmaas_cli.runner import MapOutcome


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


def _outcome(**kw: object) -> MapOutcome:
    base: dict[str, object] = dict(
        ok=3, failed=0, skipped=0, attempted=3, total=10,
        abort_reason=None, input_tokens=600, output_tokens=600,
        cached_tokens=0, reasoning_tokens=0, elapsed=2.0,
        outdir=Path("out"), failures=[], spent=1200, tripped_budget=None,
        charged=1200, rolling_spent=None,
    )
    base.update(kw)
    return MapOutcome(**base)  # type: ignore[arg-type]


def test_budget_receipt_names_the_ceiling_and_the_escape_hatch() -> None:
    """A receipt saying only ABORTED sends people hunting for failures."""
    text = map_receipt(_outcome(abort_reason="budget"), model="m", token_budget=1000)
    assert "stopped on budget" in text
    assert "1,200 of 1,000 tokens" in text
    assert "raise --token-budget" in text


def test_breaker_abort_still_says_it_was_failures() -> None:
    text = map_receipt(_outcome(abort_reason="breaker", failed=6), model="m")
    assert "too many failures" in text
    assert "budget" not in text.lower()


def test_no_budget_line_when_no_budget_was_set() -> None:
    """Ordinary runs must gain no noise."""
    assert "budget" not in map_receipt(_outcome(), model="m").lower()


def test_the_receipt_reports_the_job_total_not_the_invocation() -> None:
    """A resumed run that attempted nothing must not claim it spent nothing."""
    text = map_receipt(
        _outcome(abort_reason="budget", attempted=0, ok=0, skipped=10, spent=1200),
        model="m", token_budget=1000,
    )
    assert "1,200 of 1,000 tokens" in text


def test_the_extraction_kept_the_model_elapsed_and_failures() -> None:
    """Guards the move against silently dropping diagnostics."""
    text = map_receipt(
        _outcome(failed=1, failures=[("i3", "boom")]),
        model="rits/zai-org/glm-5-3",
    )
    assert "rits/zai-org/glm-5-3" in text
    assert "elapsed" in text
    assert "i3  boom" in text


def test_a_budget_stop_does_not_advise_a_bare_resume() -> None:
    """Plain --resume at the same ceiling re-trips, so advising it is wrong."""
    text = map_receipt(_outcome(abort_reason="budget"), model="m", token_budget=1000)
    assert "raise --token-budget, then re-run with --resume" in text
    assert "re-run with --resume to continue" not in text


def test_a_breaker_stop_still_advises_a_bare_resume() -> None:
    text = map_receipt(_outcome(abort_reason="breaker", failed=6), model="m")
    assert "re-run with --resume to continue" in text


def test_the_breaker_header_says_why() -> None:
    """Deliberate wording change from the old bare 'enmaas map ABORTED'.

    With two ways to abort, the bare word sent people looking for failures
    that had not happened. Pinned so the distinction is not lost again.
    """
    text = map_receipt(_outcome(abort_reason="breaker", failed=6), model="m")
    assert text.startswith("enmaas map ABORTED (too many failures)")


def test_a_rolling_stop_blames_the_config_not_the_flag() -> None:
    """Raising --token-budget cannot release a standing ceiling."""
    text = map_receipt(
        _outcome(abort_reason="budget", tripped_budget="rolling",
                 rolling_spent=50_100),
        model="m", rolling_ceiling=50_000, rolling_spent=48_900,
    )
    assert "rolling window" in text
    assert "50,100 of 50,000" in text
    assert "config.toml" in text
    assert "raise --token-budget" not in text


def test_a_per_run_stop_still_blames_the_flag() -> None:
    text = map_receipt(
        _outcome(abort_reason="budget", tripped_budget="per_run"),
        model="m", token_budget=1000, rolling_ceiling=50_000, rolling_spent=100,
    )
    assert "raise --token-budget" in text
    assert "config.toml" not in text


def test_both_ceilings_are_shown_and_only_one_is_marked() -> None:
    text = map_receipt(
        _outcome(abort_reason="budget", tripped_budget="rolling",
                 rolling_spent=50_100),
        model="m", token_budget=99_999, rolling_ceiling=50_000, rolling_spent=48_900,
    )
    assert text.count("stopped here") == 1
    assert "this run" in text
    assert "rolling window" in text


def test_a_rolling_ceiling_is_shown_even_without_the_flag() -> None:
    """Otherwise a standing-budget stop prints no ceiling and looks unexplained."""
    text = map_receipt(
        _outcome(abort_reason="budget", tripped_budget="rolling",
                 rolling_spent=50_100),
        model="m", rolling_ceiling=50_000, rolling_spent=48_900,
    )
    assert "50,000" in text


def test_a_resumed_run_does_not_double_count_in_the_rolling_line() -> None:
    """outcome.spent already carries prior invocations on a resumed job.

    Adding the window spend to it again reported 900 for a job whose
    ledger total was 500.
    """
    text = map_receipt(
        _outcome(spent=500, charged=100, rolling_spent=500),
        model="m", rolling_ceiling=10_000, rolling_spent=400,
    )
    assert "500 of 10,000 tokens, rolling window" in text
    assert "900" not in text
def test_the_receipt_says_when_output_was_repaired() -> None:
    """Altering a model's output and saying nothing is its own kind of wrong."""
    completion = Completion(
        text="ok � bad", reasoning="", stop="stop", usage=Usage(), replaced=1
    )
    text = ask_receipt(
        model="m", dialect="openai", completion=completion,
        path=Path("out.md"), elapsed=1.0,
    )
    assert "1 character the model emitted could not be encoded" in text
    assert "U+FFFD" in text


def test_a_clean_response_gains_no_warning() -> None:
    completion = Completion(text="fine", reasoning="", stop="stop", usage=Usage())
    text = ask_receipt(
        model="m", dialect="openai", completion=completion,
        path=Path("out.md"), elapsed=1.0,
    )
    assert "U+FFFD" not in text
