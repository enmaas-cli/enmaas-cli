"""One `ask`: a single prompt sent to the gateway, the answer written to a file."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .auth import printable, redact
from .client import CallSettings, call
from .dialect import (
    DEFAULT_MAX_TOKENS,
    Completion,
    Dialect,
    build_request,
    parse_response,
    resolve_dialect,
)
from .errors import CallError, ExitCode, where
from .outputs import protect, write_output
from .receipts import ask_receipt, create_run_dir, prune_runs
from .schema import ValidationError, load_schema, validate_against_schema
from .tokens import Usage


@dataclass(frozen=True)
class AskResult:
    receipt: str
    path: Path
    exit_code: int
    model: str
    dialect: str
    # None when the request failed or the answer was not saved.
    completion: Completion | None
    elapsed: float
    error: str | None
    # Kept apart from completion: an empty answer has no completion, yet
    # its tokens were billed and must count against the budget.
    usage: Usage | None = None
    # The failure's class name, for the ledger. Taken from the exception
    # itself, because a message can mention any word.
    error_kind: str | None = None


def _failed_after_reply(
    model: str,
    model_mark: str,
    resolved: Dialect,
    path: Path,
    started: float,
    completion: Completion,
    *,
    error: str,
    error_kind: str,
) -> AskResult:
    """The result of an ask that failed after the gateway replied.

    It keeps the reply's usage, because the request was billed. The answer
    may not have been saved, so the result carries no completion.
    """
    return AskResult(
        receipt=(
            f"enmaas ask FAILED\n"
            f"  model     {model}{model_mark}  ({resolved} dialect)\n"
            f"  error     {printable(error)}"
        ),
        path=path,
        exit_code=ExitCode.ERROR,
        model=model,
        dialect=resolved,
        completion=None,
        elapsed=time.monotonic() - started,
        error=error,
        usage=completion.usage,
        error_kind=error_kind,
    )


async def send_ask(
    client: httpx.AsyncClient,
    settings: CallSettings,
    *,
    model: str,
    prompt: str,
    system: str | None,
    dialect: Dialect | None,
    thinking: bool,
    max_tokens: int | None,
    output: Path | None,
    run_root: Path,
    save_thinking: bool,
    schema_file: Path | None = None,
    schema_name: str | None = None,
    model_defaulted: bool = False,
) -> AskResult:
    """Send one prompt, write the answer and report the result."""
    key = settings.key
    prune_runs(run_root)
    run_dir = create_run_dir(run_root)
    path = output if output is not None else run_dir / "response.md"

    protect(path)
    if save_thinking:
        protect(run_dir / "thinking.md")

    schema = load_schema(schema_file) if schema_file is not None else None

    resolved = resolve_dialect(model, dialect)
    model_mark = "  (default)" if model_defaulted else ""
    request = build_request(
        model=model,
        prompt=prompt,
        dialect=resolved,
        max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
        thinking=thinking,
        system=system,
        key=key,
        schema=schema,
        schema_name=schema_name,
    )

    for warning in request.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    started = time.monotonic()
    try:
        data = await call(
            client,
            settings.base_url,
            request,
            timeout=settings.timeout,
            retries=settings.retries,
            secret=key,
        )
        completion = parse_response(data, resolved)
    except CallError as exc:
        elapsed = time.monotonic() - started
        error_msg = redact(str(exc), key)
        return AskResult(
            receipt=(
                f"enmaas ask FAILED\n"
                f"  model     {model}{model_mark}  ({resolved} dialect)\n"
                # Printable on one line: the message can hold gateway text.
                f"  error     {printable(error_msg)}"
            ),
            path=path,
            exit_code=ExitCode.ERROR,
            model=model,
            dialect=resolved,
            completion=None,
            elapsed=elapsed,
            error=error_msg,
            # Usage() means the reply reported nothing, which is not a charge.
            usage=exc.usage if exc.usage != Usage() else None,
            error_kind=type(exc).__name__,
        )

    # The gateway billed the request once the reply arrived. A failure from
    # here on still returns a result that carries the usage, so the ledger
    # event is written. CancelledError is not an Exception and passes through.
    try:
        if schema is not None:
            try:
                validate_against_schema(completion.text, schema)
            except ValidationError as exc:
                elapsed = time.monotonic() - started
                error_msg = str(exc)
                path.parent.mkdir(parents=True, exist_ok=True)
                write_output(path, completion.text)
                # The altered response is still written to disk above, so the
                # repair has to be reported even on the failure path. Without
                # this, a schema error would be the only thing mentioned while
                # the saved file silently differs from what the model sent.
                repaired = (
                    f"\n  warning   {completion.replaced} character(s) could not be "
                    f"encoded and became U+FFFD"
                    if completion.replaced
                    else ""
                )
                return AskResult(
                    receipt=(
                        f"enmaas ask FAILED\n"
                        f"  model     {model}{model_mark}  ({resolved} dialect)\n"
                        f"  output    {printable(str(path))}\n"
                        f"  error     {printable(error_msg)}{repaired}"
                    ),
                    path=path,
                    exit_code=ExitCode.ERROR,
                    model=model,
                    dialect=resolved,
                    completion=completion,
                    elapsed=elapsed,
                    error=error_msg,
                    usage=completion.usage,
                    error_kind="ValidationError",
                )

        elapsed = time.monotonic() - started
        path.parent.mkdir(parents=True, exist_ok=True)
        write_output(path, completion.text)
        if save_thinking and completion.reasoning:
            write_output(run_dir / "thinking.md", completion.reasoning)
    except OSError as exc:
        return _failed_after_reply(
            model,
            model_mark,
            resolved,
            path,
            started,
            completion,
            error=redact(str(exc), key),
            error_kind="OSError",
        )
    except Exception as exc:
        # Never the exception text, which could quote the reply or the prompt.
        return _failed_after_reply(
            model,
            model_mark,
            resolved,
            path,
            started,
            completion,
            error=f"internal error ({where(exc)})",
            error_kind=type(exc).__name__,
        )

    return AskResult(
        receipt=ask_receipt(
            model=model,
            dialect=resolved,
            completion=completion,
            path=path,
            elapsed=elapsed,
            key=key,
            model_defaulted=model_defaulted,
        ),
        path=path,
        exit_code=ExitCode.OK,
        model=model,
        dialect=resolved,
        completion=completion,
        elapsed=elapsed,
        error=None,
        usage=completion.usage,
    )


async def run_ask(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    model: str,
    prompt: str,
    system: str | None,
    dialect: Dialect | None,
    thinking: bool,
    max_tokens: int | None,
    timeout: float,
    retries: int,
    output: Path | None,
    run_root: Path,
    save_thinking: bool,
    schema_file: Path | None = None,
    schema_name: str | None = None,
    model_defaulted: bool = False,
) -> AskResult:
    """send_ask with its gateway settings given as keyword arguments."""
    return await send_ask(
        client,
        CallSettings(base_url=base_url, key=key, timeout=timeout, retries=retries),
        model=model,
        prompt=prompt,
        system=system,
        dialect=dialect,
        thinking=thinking,
        max_tokens=max_tokens,
        output=output,
        run_root=run_root,
        save_thinking=save_thinking,
        schema_file=schema_file,
        schema_name=schema_name,
        model_defaulted=model_defaulted,
    )
