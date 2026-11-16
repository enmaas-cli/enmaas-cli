"""Run directories and the fixed-shape receipts printed to stdout."""

from __future__ import annotations

import contextlib
import re
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .auth import ERROR_TEXT_CHARS, cut_redacted, error_text, printable, redact
from .dialect import Completion, Dialect
from .errors import ExitCode

if TYPE_CHECKING:  # pragma: no cover
    # Types only. ask imports this module, and receipts stays a leaf.
    from .ask import AskResult
    from .budget import RollingCheck
    from .runner import MapOutcome

PREVIEW_LIMIT = 160
# A run with many failures would otherwise print a receipt or a --json
# object as long as its index. The index keeps every message whole.
FAILURE_LIMIT = 20
FAILURE_MESSAGE_CHARS = ERROR_TEXT_CHARS
_RUN_NAME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f]{4}")


def create_run_dir(root: Path) -> Path:
    """Timestamped directory with a random suffix.

    The suffix is random rather than derived from the prompt: prompt text is
    arbitrary, and building filenames from it is a path traversal risk.

    The root is the state directory. When this creates it, it is mode 0o700,
    because it holds every answer and the usage ledger.
    """
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    path = root / f"{stamp}-{secrets.token_hex(2)}"
    while path.exists():
        path = root / f"{stamp}-{secrets.token_hex(2)}"
    path.mkdir()
    return path


def prune_runs(root: Path, *, max_age_days: int = 30) -> None:
    """Delete old run directories. Never fatal: housekeeping must not block work.

    Only children named the way create_run_dir names them are removed.
    Without that, a runs root that a link or a mistaken XDG_STATE_HOME
    pointed at some other directory would lose every subdirectory older
    than the cutoff. A root reached through a link is skipped entirely for the same
    reason.
    """
    cutoff = time.time() - (max_age_days * 86400)
    with contextlib.suppress(OSError):
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            return
        for child in root.iterdir():
            if not _RUN_NAME.fullmatch(child.name):
                continue
            if child.is_symlink() or not child.is_dir():
                continue
            with contextlib.suppress(OSError):
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child)


def preview(text: str, *, key: str, limit: int = PREVIEW_LIMIT) -> str:
    """Deterministic head of the response. Not a generated summary.

    The reply is not trusted. A reply that echoes the key would print it, so
    the key is removed first. An escape sequence in it, such as one that sets
    the terminal's clipboard, is made inert before the cut, so the cut counts
    what is shown. The cut never splits the placeholder, and a cut made
    before redaction could leave part of the key that no longer matches.
    """
    return cut_redacted(printable(" ".join(redact(text, key).split())), limit)


def failure_line_text(message: str) -> str:
    """A redacted failure message made printable on one line, then cut to length.

    Printable comes before the cut, so the cut counts what is shown. The cut
    never splits the redaction placeholder.
    """
    return error_text(message)


def _count(value: int | None) -> str:
    return "unknown" if value is None else f"{value:,}"


def _token_line(completion: Completion) -> str:
    usage = completion.usage
    inp = _count(usage.input_tokens)
    if usage.cached_tokens:
        inp = f"{inp} ({usage.cached_tokens:,} cached)"
    line = f"  tokens    in {inp}   out {_count(usage.output_tokens)}"
    if usage.reasoning_tokens:
        line += f"   reasoning {usage.reasoning_tokens:,}"
    return line


def ask_receipt(
    *,
    model: str,
    dialect: Dialect,
    completion: Completion,
    path: Path,
    elapsed: float,
    key: str,
    model_defaulted: bool = False,
) -> str:
    size = path.stat().st_size if path.exists() else 0
    model_mark = "  (default)" if model_defaulted else ""
    lines = [
        "enmaas ask ok",
        f"  model     {model}{model_mark}  ({dialect} dialect)",
        # A path is printed inert: a file name can hold an escape sequence.
        f"  output    {printable(str(path))}  ({size:,} B)",
        _token_line(completion),
        f"  elapsed   {elapsed:.1f}s",
    ]
    if completion.stop in ("length", "max_tokens"):
        lines.append(f"  warning   output truncated at the token limit ({completion.stop})")
    if completion.replaced:
        # The response was altered to make it writable. Saying so is the
        # difference between a repaired file and a quietly corrupted one.
        plural = "s" if completion.replaced > 1 else ""
        lines.append(
            f"  warning   {completion.replaced} character{plural} the model emitted "
            f"could not be encoded and became U+FFFD"
        )
    head = preview(completion.text, key=key)
    if head:
        lines.append(f"  preview   {head}")
    return "\n".join(lines)


def rolling_total(outcome: MapOutcome, rolling_spent: int) -> int:
    """The rolling window's spend after the run, as the receipt and the JSON report it.

    The accumulator's own total when the run tracked one. It is not
    rolling_spent + outcome.spent: on a resumed job outcome.spent already
    carries earlier invocations, so that sum counts them twice. The
    fallback covers a run that tracked no accumulator.
    """
    if outcome.rolling_spent is not None:
        return outcome.rolling_spent
    return rolling_spent + outcome.charged


# The first line of the receipt, by the reason the run stopped early.
_MAP_HEADERS = {
    "budget": "enmaas map stopped on budget",
    "breaker": "enmaas map ABORTED (too many failures)",
    "interrupted": "enmaas map interrupted",
    "index": "enmaas map stopped: the index could not be written",
}


def _batch_mark(outcome: MapOutcome, batch_defaulted: bool) -> str:
    if not batch_defaulted:
        return ""
    has = "has" if outcome.batch > 1 else "has no"
    return f"  (default: template {has} ===PER ITEM===)"


def _budget_lines(
    outcome: MapOutcome, token_budget: int | None, rolling_ceiling: int | None, rolling_spent: int
) -> list[str]:
    lines = []
    if token_budget:
        # outcome.spent, not a figure derived from the token counts above:
        # on a resumed run the ceiling is compared against the job total,
        # and those counters only cover this invocation.
        mark = "  <-- stopped here" if outcome.tripped_budget == "per_run" else ""
        lines.append(f"  budget    {outcome.spent:,} of {token_budget:,} tokens, this run{mark}")
    if rolling_ceiling is not None:
        # Shown whenever a standing budget is configured, even if the
        # per-run flag was not used. Otherwise a rolling stop would print
        # no ceiling at all and look unexplained.
        mark = "  <-- stopped here" if outcome.tripped_budget == "rolling" else ""
        total = rolling_total(outcome, rolling_spent)
        lines.append(f"  budget    {total:,} of {rolling_ceiling:,} tokens, rolling window{mark}")
    return lines


def _failure_lines(outcome: MapOutcome) -> list[str]:
    # Gateway text is not trusted. A newline in it would forge a receipt
    # line, and an escape sequence would reach the terminal.
    lines = [
        f"  failure   {printable(item_id)}  {failure_line_text(message)}"
        for item_id, message in outcome.failures[:FAILURE_LIMIT]
    ]
    if len(outcome.failures) > FAILURE_LIMIT:
        lines.append(f"            {len(outcome.failures) - FAILURE_LIMIT} more in the index")
    return lines


def _resume_lines(outcome: MapOutcome) -> list[str]:
    if outcome.tripped_budget == "rolling":
        # Raising --token-budget would not help: the standing ceiling in
        # config.toml is what stopped this, and only it can release it.
        return [
            "  resume    the rolling budget in config.toml is spent; "
            "raise it or wait for the window to roll"
        ]
    if outcome.abort_reason == "budget":
        # Not the plain --resume hint below. A budget stop resumed at the
        # same ceiling re-trips at once, so telling the user to "re-run
        # with --resume" would be advice that cannot work.
        return ["  resume    raise --token-budget, then re-run with --resume"]
    if outcome.abort_reason is not None:
        return ["  resume    re-run with --resume to continue"]
    return []


def map_receipt(
    outcome: MapOutcome,
    *,
    model: str,
    token_budget: int | None = None,
    rolling_ceiling: int | None = None,
    rolling_spent: int = 0,
    model_defaulted: bool = False,
    batch_defaulted: bool = False,
) -> str:
    """The fixed-shape receipt for a bulk run.

    Here rather than inline in the handler so it can be tested without
    running a whole command, the way ask_receipt already is. Takes the
    model separately because MapOutcome does not carry one.
    """
    not_attempted = outcome.total - outcome.attempted - outcome.skipped
    model_mark = "  (default)" if model_defaulted else ""
    lines = [_MAP_HEADERS.get(outcome.abort_reason or "", "enmaas map ok")]
    if outcome.abort_reason == "index":
        lines.append(f"  error     {error_text(outcome.index_error or 'unknown')}")
    lines += [
        f"  model     {model}{model_mark}",
        f"  batch     {outcome.batch}{_batch_mark(outcome, batch_defaulted)}",
        f"  items     {outcome.total} total: {outcome.attempted} attempted, "
        f"{outcome.ok} ok, {outcome.failed} failed, {outcome.skipped} skipped",
    ]
    if outcome.internal_errors > 0:
        lines.append(
            f"  internal  {outcome.internal_errors} failed on an error inside enmaas; "
            f"please report it"
        )
    if not_attempted > 0:
        lines.append(f"            {not_attempted} not attempted")
    lines.append(f"  outdir    {printable(str(outcome.outdir))}")
    lines.append(f"  index     {printable(str(outcome.outdir / 'results.jsonl'))}")
    lines.append(f"  tokens    in {outcome.input_tokens:,}   out {outcome.output_tokens:,}")
    lines += _budget_lines(outcome, token_budget, rolling_ceiling, rolling_spent)
    lines.append(f"  elapsed   {outcome.elapsed:.1f}s")
    lines += _failure_lines(outcome)
    lines += _resume_lines(outcome)
    return "\n".join(lines)


def ask_json(result: AskResult) -> dict[str, Any]:
    """The `ask --json` object, built from the same result as the text receipt."""
    usage = result.usage
    return {
        "status": "ok" if result.exit_code == ExitCode.OK else "error",
        "model": result.model,
        "dialect": result.dialect,
        "path": str(result.path) if result.completion is not None else None,
        "tokens": {
            "input": usage.input_tokens if usage else None,
            "cached": usage.cached_tokens if usage else None,
            "output": usage.output_tokens if usage else None,
            "reasoning": usage.reasoning_tokens if usage else None,
        },
        "elapsed": result.elapsed,
        # --json suppresses the receipt, so without this a repaired response
        # would look untouched to a caller that only reads the
        # machine-readable output.
        "replaced": (result.completion.replaced if result.completion else 0),
        "error": error_text(result.error, terminal=False) if result.error is not None else None,
    }


def _map_status(outcome: MapOutcome) -> Literal["ok", "partial", "aborted"]:
    if outcome.abort_reason is not None:
        return "aborted"
    return "partial" if outcome.failed > 0 else "ok"


def map_json(
    outcome: MapOutcome,
    *,
    model: str,
    token_budget: int | None = None,
    rolling_ceiling: int | None = None,
    rolling_spent: int = 0,
    model_defaulted: bool = False,
    batch_defaulted: bool = False,
) -> dict[str, Any]:
    """The `map --json` object, built from the same data as map_receipt."""
    return {
        "status": _map_status(outcome),
        "model": model,
        "batch": outcome.batch,
        "defaulted": {"model": model_defaulted, "batch": batch_defaulted},
        # Which of the two stop conditions fired. "aborted" alone cannot
        # distinguish failures from overspending.
        "abort_reason": outcome.abort_reason,
        "total": outcome.total,
        "ok": outcome.ok,
        "failed": outcome.failed,
        # Already inside "failed". Items failed by an error that no handler
        # expected, which is a fault to report.
        "internal_errors": outcome.internal_errors,
        "skipped": outcome.skipped,
        "attempted": outcome.attempted,
        "outdir": str(outcome.outdir),
        "index": str(outcome.outdir / "results.jsonl"),
        "tokens": {
            "input": outcome.input_tokens,
            "output": outcome.output_tokens,
        },
        "elapsed": outcome.elapsed,
        # Both ceilings reported separately, and "tripped" names which one
        # stopped the run. A single flattened object could not say whether
        # to raise the flag or the config, which are different remedies.
        "budget": {
            "per_run": (
                None
                if not token_budget
                # Job-wide when resumed, matching what the ceiling is
                # compared against.
                else {"ceiling": token_budget, "spent": outcome.spent}
            ),
            "rolling": (
                None
                if rolling_ceiling is None
                else {"ceiling": rolling_ceiling, "spent": rolling_total(outcome, rolling_spent)}
            ),
            "tripped": outcome.tripped_budget,
        },
        # Bounded like the receipt; the index keeps every message whole.
        # JSON encoding keeps controls safe; the display cap is strict.
        "failures": [
            {"id": item_id, "error": error_text(message, terminal=False)}
            for item_id, message in outcome.failures[:FAILURE_LIMIT]
        ],
        "failures_truncated": len(outcome.failures) > FAILURE_LIMIT,
        # Why the run stopped when the index could not be written.
        "error": error_text(outcome.index_error, terminal=False)
        if outcome.index_error is not None
        else None,
    }


def refusal_receipt(command: str, rolling: RollingCheck, *, key: str = "") -> str:
    """The text printed when the rolling budget refuses a command before any request."""
    return f"enmaas {command} refused\n  budget    {error_text(rolling.message or '', key)}"


def refusal_json(rolling: RollingCheck, *, key: str = "") -> dict[str, Any]:
    """The --json object for the same refusal.

    Exit 3 already means "stopped early", so a bare exit code cannot
    distinguish a budget refusal from a breaker abort. A --json caller gets
    the same abort_reason vocabulary it would get from a run that started
    and then stopped.
    """
    return {
        "status": "refused",
        "abort_reason": "budget",
        "budget": {
            "per_run": None,
            "rolling": {"ceiling": rolling.ceiling, "spent": rolling.spent},
            "tripped": "rolling",
        },
        "error": error_text(rolling.message, key, terminal=False)
        if rolling.message is not None
        else None,
    }
