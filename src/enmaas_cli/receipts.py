"""Run directories and the fixed-shape receipts printed to stdout."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from .dialect import Completion, Dialect

PREVIEW_LIMIT = 160


def state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "enmaas" / "runs"


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
    """Delete old run directories. Never fatal: housekeeping must not block work."""
    cutoff = time.time() - (max_age_days * 86400)
    with contextlib.suppress(OSError):
        for child in root.iterdir():
            if not child.is_dir():
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
) -> str:
    size = path.stat().st_size if path.exists() else 0
    lines = [
        "enmaas ask ok",
        f"  model     {model}  ({dialect} dialect)",
        f"  output    {path}  ({size:,} B)",
        _token_line(completion),
        f"  elapsed   {elapsed:.1f}s",
    ]
    if completion.stop in ("length", "max_tokens"):
        lines.append(f"  warning   output truncated at the token limit ({completion.stop})")
    head = preview(completion.text)
    if head:
        lines.append(f"  preview   {head}")
    return "\n".join(lines)
