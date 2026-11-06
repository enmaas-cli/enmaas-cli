"""Cost a run, without inventing prices.

The gateway publishes none: its `/v1/models` catalog carries only `id`,
`object`, `created` and `owned_by`. So this module prices what it has been
told about, reports the rest as unpriced rather than as free, and keeps the
reference comparison separate from any claim about real spend.

Two different numbers, deliberately not added together:

- **cost**, what a call actually cost on the gateway. Known only for models
  with a configured rate, and for the hosted `rits/*` models, which are
  free. Everything else is None.
- **Opus-equivalent**, what the same tokens would have cost had the session
  generated them itself. A comparison, not an invoice, and the reason
  delegating is worth measuring.
"""

from __future__ import annotations

from dataclasses import dataclass

# Hosted on the gateway at no cost. The only price this tool can assert
# without being told it.
_FREE_PREFIXES = ("rits/",)


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    label: str = ""


# Claude Opus 5 list rate, used only for the comparison column. Opus here
# runs through Vertex, which is partner-priced separately, so this is an
# order of magnitude rather than an invoice. Stated rather than buried: a
# dollar figure nobody can reproduce is worse than no dollar figure.
OPUS_REFERENCE = ModelPrice(
    input_per_mtok=5.00,
    output_per_mtok=25.00,
    label="Claude Opus 5 list rate, $5.00 in / $25.00 out per MTok",
)

_FREE = ModelPrice(input_per_mtok=0.0, output_per_mtok=0.0, label="free on the gateway")


def price_for(model: str, table: dict[str, ModelPrice]) -> ModelPrice | None:
    """The rate for a model, or None when nobody has supplied one.

    An exact id wins over a prefix, because prices are usually quoted per
    family but a single model occasionally differs.
    """
    if model in table:
        return table[model]
    if model.startswith(_FREE_PREFIXES):
        return _FREE
    best: ModelPrice | None = None
    best_len = -1
    for key, price in table.items():
        if model.startswith(key) and len(key) > best_len:
            best, best_len = price, len(key)
    return best


def cost_for(
    model: str,
    input_tokens: int,
    output_tokens: int,
    table: dict[str, ModelPrice],
) -> float | None:
    """What this call cost, or None when the model has no known rate.

    None and 0.0 mean different things and must not be collapsed: an
    unpriced model reported as free would understate every total silently.
    """
    price = price_for(model, table)
    if price is None:
        return None
    return input_tokens / 1e6 * price.input_per_mtok + output_tokens / 1e6 * price.output_per_mtok


def opus_equivalent(input_tokens: int, output_tokens: int) -> float:
    """What the same tokens would have cost at the reference rate."""
    return (
        input_tokens / 1e6 * OPUS_REFERENCE.input_per_mtok
        + output_tokens / 1e6 * OPUS_REFERENCE.output_per_mtok
    )
