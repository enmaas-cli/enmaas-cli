"""Run directories and the fixed-shape receipts printed to stdout."""

from __future__ import annotations

import contextlib
import re
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .dialect import Completion, Dialect
from .outputs import state_root as state_root

if TYPE_CHECKING:  # pragma: no cover
    # Import-time cycle avoidance: runner imports budget, not receipts,
    # but keeping this behind TYPE_CHECKING costs nothing and documents
    # that receipts is a leaf.
    from .runner import MapOutcome

PREVIEW_LIMIT = 160
_RUN_NAME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f]{4}")


def create_run_dir(root: Path) -> Path:
    """Timestamped directory with a random suffix.

    The suffix is random rather than derived from the prompt: prompt text is
    arbitrary, and building filenames from it is a path traversal risk.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    path = root / f"{stamp}-{secrets.token_hex(2)}"
    while path.exists():
        path = root / f"{stamp}-{secrets.token_hex(2)}"
    path.mkdir(parents=True)
    return path


def prune_runs(root: Path, *, max_age_days: int = 30) -> None:
    """Delete old run directories. Never fatal: housekeeping must not block work.

    Only children named the way create_run_dir names them are removed.
    Without that, a runs root that a link or a mistaken XDG_STATE_HOME
    pointed at some other directory lost every subdirectory older than the
    cutoff. A root reached through a link is skipped entirely for the same
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


def preview(text: str, *, limit: int = PREVIEW_LIMIT) -> str:
    """Deterministic head of the response. Not a generated summary."""
    return " ".join(text.split())[:limit]


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
    model_defaulted: bool = False,
) -> str:
    size = path.stat().st_size if path.exists() else 0
    model_mark = "  (default)" if model_defaulted else ""
    lines = [
        "enmaas ask ok",
        f"  model     {model}{model_mark}  ({dialect} dialect)",
        f"  output    {path}  ({size:,} B)",
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
    head = preview(completion.text)
    if head:
        lines.append(f"  preview   {head}")
    return "\n".join(lines)


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
    if outcome.abort_reason == "budget":
        header = "enmaas map stopped on budget"
    elif outcome.abort_reason == "breaker":
        header = "enmaas map ABORTED (too many failures)"
    else:
        header = "enmaas map ok"

    not_attempted = outcome.total - outcome.attempted - outcome.skipped
    model_mark = "  (default)" if model_defaulted else ""
    if batch_defaulted:
        has = "has" if outcome.batch > 1 else "has no"
        batch_mark = f"  (default: template {has} ===PER ITEM===)"
    else:
        batch_mark = ""
    lines = [
        header,
        f"  model     {model}{model_mark}",
        f"  batch     {outcome.batch}{batch_mark}",
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
    lines.append(f"  outdir    {outcome.outdir}")
    lines.append(f"  index     {outcome.outdir / 'results.jsonl'}")
    lines.append(f"  tokens    in {outcome.input_tokens:,}   out {outcome.output_tokens:,}")
    if token_budget:
        # outcome.spent, not a figure derived from the token counts above:
        # on a resumed run the ceiling is compared against the job total,
        # and those counters only cover this invocation's successes.
        mark = "  <-- stopped here" if outcome.tripped_budget == "per_run" else ""
        lines.append(f"  budget    {outcome.spent:,} of {token_budget:,} tokens, this run{mark}")
    if rolling_ceiling is not None:
        # Shown whenever a standing budget is configured, even if the
        # per-run flag was not used. Otherwise a rolling stop would print
        # no ceiling at all and look unexplained.
        mark = "  <-- stopped here" if outcome.tripped_budget == "rolling" else ""
        # The accumulator's own total when the run tracked one. The
        # fallback covers a refusal before any request, where no
        # accumulator ran.
        total = outcome.rolling_spent
        if total is None:
            total = rolling_spent + outcome.charged
        lines.append(f"  budget    {total:,} of {rolling_ceiling:,} tokens, rolling window{mark}")
    lines.append(f"  elapsed   {outcome.elapsed:.1f}s")
    for item_id, message in outcome.failures[:20]:
        lines.append(f"  failure   {item_id}  {message}")
    if outcome.tripped_budget == "rolling":
        # Raising --token-budget would not help: the standing ceiling in
        # config.toml is what stopped this, and only it can release it.
        lines.append(
            "  resume    the rolling budget in config.toml is spent; "
            "raise it or wait for the window to roll"
        )
    elif outcome.abort_reason == "budget":
        # Not the plain --resume hint below. A budget stop resumed at the
        # same ceiling re-trips at once, so telling the user to "re-run
        # with --resume" would be advice that cannot work.
        lines.append("  resume    raise --token-budget, then re-run with --resume")
    elif outcome.abort_reason is not None:
        lines.append("  resume    re-run with --resume to continue")
    return "\n".join(lines)
