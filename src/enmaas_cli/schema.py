"""JSON Schema loading and response validation.

One implementation, shared by `ask` and `map`. Two copies drift apart: the
same malformed schema file can raise FileNotFoundError or ValueError from one
command and ValidationError from the other. When both surface the same way to
a user, nothing looks broken, which is how a hidden difference survives long
enough to become a real one.

Validation runs on every dialect, not only where the gateway enforces a schema.
The OpenAI dialect genuinely constrains output to the supplied schema. The
Anthropic dialect treats a tool `input_schema` as advisory: measured against a
control schema whose enum permitted only one impossible value, models on that
dialect returned well-formed JSON carrying a different value entirely. Checking
locally is what makes `--schema` mean the same thing on both.
"""

from __future__ import annotations

import json
from itertools import islice
from pathlib import Path
from typing import Any

import jsonschema
import referencing
import referencing.exceptions

from .auth import cut_redacted, redact

_MAX_REASON_CHARS = 200
# Real schemas nest a few dozen objects and lists at most. The limit bounds
# the reference check below and keeps a hostile file from using it to recurse.
_MAX_SCHEMA_DEPTH = 100
_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")
# An empty registry holds no documents and retrieves none, so a reference that
# is not inside the schema fails instead of being fetched.
_NO_RETRIEVAL: referencing.Registry[Any] = referencing.Registry()


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
        reason = _bounded(f"{exc.msg} at line {exc.lineno} column {exc.colno}")
        raise ValidationError(f"schema file contains invalid JSON: {path} ({reason})") from exc
    if not isinstance(loaded, dict):
        raise ValidationError(
            f"schema file must contain a JSON object, found {type(loaded).__name__}: {path}"
        )
    schema: dict[str, Any] = loaded
    _check_references(schema, path)
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        # The library message embeds the rejected schema value, so a schema
        # with a huge string in it produced an error of the same size. Name
        # the failed rule and where it sits instead.
        parts = [str(part)[:32] for part in islice(exc.path, 4)]
        rule = str(exc.validator or "schema")[:48]
        reason = _bounded(f"{rule} constraint failed at {json.dumps(parts, ensure_ascii=True)}")
        raise ValidationError(f"schema file is not a valid JSON Schema: {path} ({reason})") from exc
    return schema


def _check_references(schema: dict[str, Any], path: Path) -> None:
    """Refuse a reference that points outside the schema file.

    jsonschema retrieves such a reference over HTTP or from a file while it
    validates an answer, so a schema from another party could make enmaas
    contact any host (server-side request forgery). Only references that start
    with # stay inside the file. The message names the keyword, never the
    value, which can be a long or private URL.
    """
    pending: list[tuple[object, int]] = [(schema, 1)]
    while pending:
        node, depth = pending.pop()
        if depth > _MAX_SCHEMA_DEPTH:
            raise ValidationError(
                f"schema file is nested more than {_MAX_SCHEMA_DEPTH} levels deep: {path}"
            )
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in _REFERENCE_KEYWORDS
                    and isinstance(value, str)
                    and not value.startswith("#")
                ):
                    raise ValidationError(
                        f"schema file has a {key} that points outside the file; only references "
                        f"that start with # are allowed: {path}"
                    )
                if isinstance(value, (dict, list)):
                    pending.append((value, depth + 1))
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node if isinstance(item, (dict, list)))


def _bounded(reason: str) -> str:
    if len(reason) <= _MAX_REASON_CHARS:
        return reason
    return reason[: _MAX_REASON_CHARS - 3] + "..."


def validate_against_schema(text: str, schema: dict[str, Any], *, key: str = "") -> None:
    """Check model output against the schema. Raises ValidationError if it fails.

    Both the JSON decoder and the schema check recurse once per nesting
    level, so a deeply nested response raises RecursionError. That is a
    response the schema cannot accept, not a fault in this tool.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"response is not valid JSON: {exc}") from exc
    except RecursionError:
        raise ValidationError("response is nested too deeply") from None
    try:
        jsonschema.validate(payload, schema, registry=_NO_RETRIEVAL)
    except RecursionError:
        raise ValidationError("response is nested too deeply") from None
    except referencing.exceptions.Unresolvable:
        raise ValidationError("schema has a reference that cannot be resolved") from None
    except jsonschema.ValidationError as exc:
        # jsonschema's message can contain the entire rejected response.
        # Report the rule and a bounded field path without printing its value.
        parts = [cut_redacted(redact(str(part), key), 32) for part in islice(exc.absolute_path, 4)]
        location = json.dumps(parts, ensure_ascii=True)
        if len(location) > 120:
            location = cut_redacted(location, 117) + "..."
        rule = str(exc.validator or "schema")[:48]
        detail = ""
        if (
            exc.validator == "required"
            and isinstance(exc.instance, dict)
            and isinstance(exc.validator_value, list)
        ):
            missing = [name for name in exc.validator_value if name not in exc.instance]
            names = json.dumps(
                [cut_redacted(redact(name, key), 32) for name in missing[:4]], ensure_ascii=True
            )
            detail = f"; missing fields: {cut_redacted(names, 120)}"
        raise ValidationError(
            redact(
                f"response does not match schema at {location}: {rule} constraint failed{detail}",
                key,
            )
        ) from exc
