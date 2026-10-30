"""JSON Schema loading and response validation.

One implementation, shared by `ask` and `map`. These functions previously
existed as near-copies in cli.py and runner.py, and the copies had already
drifted: the same malformed schema file raised FileNotFoundError or ValueError
from one command and ValidationError from the other. Both happened to surface
the same way to a user, so nothing was visibly broken, which is exactly how a
latent divergence survives long enough to become a real one.

Validation runs on every dialect, not only where the gateway enforces a schema.
The OpenAI dialect genuinely constrains output to the supplied schema. The
Anthropic dialect treats a tool `input_schema` as advisory: measured against a
control schema whose enum permitted only one impossible value, models on that
dialect returned well-formed JSON carrying a different value entirely. Checking
locally is what makes `--schema` mean the same thing on both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema


class ValidationError(Exception):
    """A schema file could not be used, or a response did not conform to it.

    Raised for both cases on purpose: each is a reason the caller cannot trust
    the output, and both map to the same exit code. Load failures are raised
    before any request is made, so a broken schema file aborts a run rather
    than being recorded as a per-item failure. Keep `load_schema` outside any
    per-item error handling, or that distinction is lost.
    """


def load_schema(path: Path) -> dict[str, Any]:
    """Load and check a JSON Schema file before any request is made."""
    if not path.exists():
        raise ValidationError(f"schema file does not exist: {path}")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"schema file contains invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValidationError(
            f"schema file must contain a JSON object, found {type(loaded).__name__}: {path}"
        )
    schema: dict[str, Any] = loaded
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ValidationError(f"schema file is not a valid JSON Schema: {exc}") from exc
    return schema


def validate_against_schema(text: str, schema: dict[str, Any]) -> None:
    """Check model output against the schema. Raises ValidationError if it fails."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"response is not valid JSON: {exc}") from exc
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise ValidationError(f"response does not match schema: {exc.message}") from exc
