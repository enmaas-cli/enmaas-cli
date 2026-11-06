"""Batch several map items into one request.

One call per item re-sends the whole template every time. Measured on a real
job, a 435 token template against 141 token items meant 75% of every prompt
was boilerplate. Batching ten items cuts input by roughly two thirds, and
lifts the prompt over the ~1,024 token floor below which this gateway does
not cache a prefix at all, so the saving compounds.

The template declares where it splits. Guessing the shared part by finding a
common prefix would work until a template changed shape, and then it would
quietly send the wrong thing.
"""

from __future__ import annotations

import re
from typing import Any

from enmaas_cli.schema import ValidationError

PER_ITEM_MARKER = "===PER ITEM==="

_ITEM_HEADER = "=== ITEM {id} ==="
_ANSWER_HEADER = "=== ANSWER {id} ==="
_ANSWER_RE = re.compile(r"^===\s*ANSWER\s+(\S+?)\s*===\s*$", re.MULTILINE)


def split_template(template: str) -> tuple[str, str]:
    """Split a template into its shared preamble and its per-item block."""
    if PER_ITEM_MARKER not in template:
        raise ValidationError(
            f"batching needs the template to mark where the per-item part "
            f"starts. Put a line reading {PER_ITEM_MARKER} between the shared "
            f"instructions and the per-item fields."
        )
    preamble, _, per_item = template.partition(PER_ITEM_MARKER)
    return preamble.rstrip() + "\n", per_item.lstrip("\n")


def build_batch_prompt(preamble: str, per_item: str, items: list[dict[str, Any]]) -> str:
    """One prompt covering every item, with the preamble sent once."""
    parts = [preamble, ""]
    parts.append(
        f"Below are {len(items)} items. Answer every one of them.\n"
        f"Before each answer emit a line reading exactly "
        f"{_ANSWER_HEADER.format(id='<the item id>')}, then that item's "
        f"answer and nothing else. Do not merge, reorder or skip items.\n"
    )
    for item in items:
        parts.append(_ITEM_HEADER.format(id=item["id"]))
        parts.append(per_item.format(**item).rstrip())
        parts.append("")
    return "\n".join(parts)


def split_batch_response(response: str, ids: list[str]) -> dict[str, str]:
    """Pull each item's answer out of one batched reply, keyed by id.

    Matching is by id, never by position. A reply that omits one item would
    otherwise shift every later answer onto the wrong item, which is worse
    than losing the batch: the output files would all look fine.

    An id that was not asked for is dropped, so a model inventing one cannot
    create an output file for it.
    """
    wanted = set(ids)
    found: dict[str, str] = {}
    matches = list(_ANSWER_RE.finditer(response))
    for number, match in enumerate(matches):
        item_id = match.group(1)
        start = match.end()
        end = matches[number + 1].start() if number + 1 < len(matches) else len(response)
        if item_id in wanted:
            found[item_id] = response[start:end].strip("\n")
    return found
