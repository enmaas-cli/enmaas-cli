"""Argument parsing, command dispatch, exit codes."""

from __future__ import annotations

import argparse
import asyncio
import enum
import importlib.metadata
import ipaddress
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import urlsplit

import httpx

from .auth import AuthError, redact, resolve_key
from .budget import TokenBudget, metered, rolling_spend
from .client import call
from .config import Config, ConfigError, default_config_path, load_config
from .dialect import (
    ANTHROPIC_VERSION,
    Dialect,
    DialectError,
    Usage,
    build_request,
    default_max_tokens,
    parse_response,
    resolve_dialect,
)
from .errors import CallError
from .outputs import (
    MAX_INSPECT_CHARS,
    allow_read,
    inspect_file,
    protect,
    state_root,
    write_output,
)
from .receipts import ask_receipt, create_run_dir, map_receipt, prune_runs
from .runner import run_map
from .schema import ValidationError, load_schema, validate_against_schema
from .usage import (
    UsageEvent,
    append_event,
    format_summary,
    format_summary_json,
    load_events,
    load_events_strict,
    summarize_usage,
)

BASE_URL_ENV = "ENMAAS_BASE_URL"


class ExitCode(enum.IntEnum):
    OK = 0
    ERROR = 1
    PARTIAL = 2
    ABORTED = 3


class Parser(argparse.ArgumentParser):
    """ArgumentParser that exits 1 on usage errors instead of 2."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(int(ExitCode.ERROR), f"{self.prog}: error: {message}\n")


def positive_int(value: str) -> int:
    """Validate a positive integer for argparse."""
    ival = int(value)
    if ival <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive integer")
    return ival


def inspect_chars(value: str) -> int:
    number = positive_int(value)
    if number > MAX_INSPECT_CHARS:
        raise argparse.ArgumentTypeError(f"max-chars cannot exceed {MAX_INSPECT_CHARS}")
    return number


def nonnegative_int(value: str) -> int:
    """Token counts may legitimately be zero, but never negative."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value} is not an integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"{value} is negative")
    return number


def nonnegative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value} is not a number") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"{value} is negative")
    return number


def positive_float(value: str) -> float:
    """Validate a positive float for argparse."""
    fval = float(value)
    if fval <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive number")
    return fval


def resolve_thinking(flag: str | None, model: str) -> bool:
    """Decide the `thinking` value to pass to build_request.

    True means send no reasoning knob and let the provider decide.
    False means send the explicit disable knob.

    Hosted rits/* models reason without bound on large prompts: measured, a
    45 KB prompt returned a 504 after 300 seconds on one and 32,768 tokens of
    invisible thinking on the other. So they default to reasoning off.
    Frontier models manage their own budgets and have not shown this, so they
    are left untouched unless the user asks.
    """
    if flag is not None:
        return flag == "on"
    return not model.startswith("rits/")


def assemble_prompt(*, prompt: str, files: list[Path]) -> str:
    """Attach files as labelled blocks. Contents are data, never executed."""
    parts = [prompt]
    for path in files:
        if not path.exists():
            raise FileNotFoundError(f"attachment does not exist: {path}")
        body = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"\n--- file: {path} ---\n{body}\n--- end: {path} ---")
    return "\n".join(parts)


def _resolve_model(flag: str | None, config: Config) -> tuple[str, bool]:
    """The model to call and whether it came from config rather than the flag.

    The second value exists so receipts can mark a defaulted model. A
    silent default is worse than an explicit one.
    """
    if flag is not None:
        return flag, False
    return config.default_model, True


def _resolve_dialect_override(model: str, flag: Dialect | None, config: Config) -> Dialect | None:
    """Apply a configured dialect override unless the flag was given.

    Returns the Literal type run_ask and run_map require. The config
    value is a plain str, so it is checked and then cast rather than
    returned as is; mypy strict rejects str where Dialect is expected.
    """
    if flag is not None:
        return flag
    override = config.dialect_overrides.get(model)
    if override is None:
        return None
    if override in ("anthropic", "openai"):
        return cast(Dialect, override)
    print(
        f"warning: invalid dialect override '{override}' for {model}, ignoring",
        file=sys.stderr,
    )
    return None


def package_version() -> str:
    try:
        return importlib.metadata.version("enmaas-cli")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="enmaas")
    parser.add_argument("--version", action="version", version=f"enmaas {package_version()}")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    models = sub.add_parser("models", help="list the gateway catalog")
    models.add_argument("--dialect", choices=["anthropic", "openai", "both"], default="both")
    models.add_argument("--json", action="store_true")
    models.add_argument("--timeout", type=positive_float)

    ask = sub.add_parser("ask", help="one-shot prompt, response written to a file")
    ask.add_argument("-m", "--model")
    ask.add_argument("-f", "--file", type=Path, action="append", default=[])
    ask.add_argument("-p", "--prompt")
    ask.add_argument("--prompt-file", type=Path)
    ask.add_argument("--system")
    ask.add_argument("-o", "--output", type=Path)
    ask.add_argument("--stdout", action="store_true")
    ask.add_argument("--json", action="store_true")
    ask.add_argument("--thinking", choices=["on", "off"], default=None)
    ask.add_argument("--save-thinking", action="store_true")
    ask.add_argument("--max-tokens", type=positive_int)
    ask.add_argument("--timeout", type=positive_float)
    ask.add_argument("--retries", type=int, default=3)
    ask.add_argument("--dialect", choices=["anthropic", "openai"])
    ask.add_argument("--schema", type=Path)
    ask.add_argument("--schema-name")

    mp = sub.add_parser("map", help="fan out one call per JSONL item")
    mp.add_argument("-m", "--model", help="defaults to default_model from config")
    mp.add_argument("--items", type=Path, required=True)
    mp.add_argument("--template", type=Path, required=True)
    mp.add_argument("--outdir", type=Path, required=True)
    mp.add_argument("--concurrency", type=positive_int)
    mp.add_argument(
        "--token-budget",
        type=positive_int,
        metavar="N",
        help=(
            "stop the run once it has spent N metered tokens (uncached "
            "input plus output). Not --max-tokens, which caps the output "
            "of a single request."
        ),
    )
    mp.add_argument("--retries", type=int, default=3)
    mp.add_argument("--timeout", type=positive_float)
    mp.add_argument("--thinking", choices=["on", "off"], default=None)
    mp.add_argument(
        "--batch",
        type=positive_int,
        default=None,
        metavar="N",
        help="items per request. Defaults to 10 when the template contains "
        "the ===PER ITEM=== marker and 1 otherwise. Cuts input sharply "
        "on bulk jobs because the shared preamble is sent once per batch.",
    )
    mp.add_argument("--resume", action="store_true")
    mp.add_argument("--json", action="store_true")
    mp.add_argument("--dialect", choices=["anthropic", "openai"])
    mp.add_argument("--max-tokens", type=positive_int)
    mp.add_argument("--schema", type=Path)
    mp.add_argument("--schema-name")

    rec = sub.add_parser(
        "record",
        help="record a run this CLI did not make, so it is counted too",
    )
    rec.add_argument("--model", required=True)
    rec.add_argument(
        # Explicit dest: argparse already uses "command" for the subcommand
        # name, and colliding would overwrite it with this value.
        "--command",
        required=True,
        dest="command_name",
        help="what did the work, e.g. the calling tool's name",
    )
    rec.add_argument("--input", type=nonnegative_int, required=True)
    rec.add_argument("--output", type=nonnegative_int, required=True)
    rec.add_argument("--cached", type=nonnegative_int)
    rec.add_argument("--reasoning", type=nonnegative_int)
    rec.add_argument("--elapsed", type=nonnegative_float, default=0.0)
    rec.add_argument("--status", choices=["ok", "error"], default="ok")
    rec.add_argument("--error-kind", help="class name only, never a message")
    rec.add_argument("--dialect", choices=["openai", "anthropic"], default="openai")

    usage = sub.add_parser("usage", help="show usage statistics")
    usage.add_argument("--json", action="store_true")
    usage.add_argument("--since", type=positive_int, metavar="DAYS", help="limit to last N days")

    inspect = sub.add_parser("inspect", help="print a bounded prefix of one file")
    inspect.add_argument("path", type=Path)
    inspect.add_argument("--max-chars", type=inspect_chars, default=MAX_INSPECT_CHARS)

    allow = sub.add_parser("allow-read", help="allow a full read of one protected output")
    allow.add_argument("path", type=Path)

    return parser


@dataclass(frozen=True)
class AskResult:
    receipt: str
    path: Path
    exit_code: int
    model: str
    dialect: str
    completion: Any | None  # Completion object or None on error
    elapsed: float
    error: str | None
    # Kept apart from completion: an empty answer has no completion, yet
    # its tokens were billed and must count against the budget.
    usage: Usage | None = None
    # The failure's class name, for the ledger. Taken from the exception
    # itself, because a message can mention any word.
    error_kind: str | None = None


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
        max_tokens=max_tokens if max_tokens is not None else default_max_tokens(thinking=thinking),
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
        data = await call(client, base_url, request, timeout=timeout, retries=retries, secret=key)
        completion = parse_response(data, resolved)
    except CallError as exc:
        elapsed = time.monotonic() - started
        error_msg = redact(str(exc), key)
        return AskResult(
            receipt=(
                f"enmaas ask FAILED\n"
                f"  model     {model}{model_mark}  ({resolved} dialect)\n"
                f"  error     {error_msg}"
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
                    f"  output    {path}\n"
                    f"  error     {error_msg}{repaired}"
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

    return AskResult(
        receipt=ask_receipt(
            model=model,
            dialect=resolved,
            completion=completion,
            path=path,
            elapsed=elapsed,
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


async def run_models(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    dialect: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """Query one or both catalogs. Neither is complete on its own."""
    wanted: list[Dialect] = (
        ["anthropic", "openai"] if dialect == "both" else [dialect]  # type: ignore[list-item]
    )
    found: dict[str, set[str]] = {}
    for d in wanted:
        headers = (
            {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
            if d == "anthropic"
            else {"Authorization": f"Bearer {key}"}
        )
        response = await client.get(f"{base_url}/v1/models", headers=headers, timeout=timeout)
        response.raise_for_status()
        for entry in response.json().get("data", []):
            found.setdefault(str(entry.get("id")), set()).add(d)
    return [
        {"id": model_id, "dialects": sorted(dialects)}
        for model_id, dialects in sorted(found.items())
    ]


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return str(args.prompt)
    if args.prompt_file is not None:
        return str(args.prompt_file.read_text(encoding="utf-8"))
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("no prompt: pass -p, --prompt-file, or pipe text on stdin")


def _warn_if_untracked(recorded: bool, config: Config) -> None:
    """Say so when spend a budget depends on was not written down.

    An unwritable ledger looks exactly like one showing no spend, so a
    configured ceiling would quietly stop binding. The work is already
    paid for, so this warns rather than failing.
    """
    if not recorded and config.budget_tokens is not None:
        print(
            "warning: a token budget is configured but this run's usage was "
            "not recorded, so it will not count against the ceiling",
            file=sys.stderr,
        )


@dataclass(frozen=True)
class RollingCheck:
    """Whether a standing budget permits this invocation, and where it stands."""

    spent: int
    ceiling: int | None
    message: str | None  # set when the command must refuse


def rolling_check(config: Config, ledger: Path) -> RollingCheck:
    """Decide whether a configured rolling budget allows more work.

    Returns the spend as well as the verdict, because a map run needs to
    carry the remaining allowance into run_map rather than recompute it.

    A function returning a message is testable without capturing stdout,
    which is why the decision lives here rather than inline in main.
    """
    if config.budget_tokens is None or config.budget_window_days is None:
        return RollingCheck(spent=0, ceiling=None, message=None)

    if os.environ.get("ENMAAS_NO_USAGE_LOG"):
        # Nothing is being recorded, so the ceiling could never bind and
        # every run would see zero spent. Enforcement that cannot observe
        # itself must say so rather than wave the work through.
        return RollingCheck(
            0,
            config.budget_tokens,
            message=(
                "a token budget is configured but ENMAAS_NO_USAGE_LOG disables "
                "the ledger, so spend cannot be tracked"
            ),
        )

    events, problems = load_events_strict(ledger)
    if problems:
        # An unreadable ledger is indistinguishable from one showing zero
        # spend. Without a budget configured this would only warn.
        return RollingCheck(
            0,
            config.budget_tokens,
            message=(
                "a token budget is configured but the usage ledger could not "
                f"be read: {'; '.join(problems[:3])}"
            ),
        )

    spent = rolling_spend(events, window_days=config.budget_window_days)
    if spent >= config.budget_tokens:
        return RollingCheck(
            spent,
            config.budget_tokens,
            message=(
                f"{spent:,} of {config.budget_tokens:,} tokens spent in the last "
                f"{config.budget_window_days} days"
            ),
        )
    return RollingCheck(spent, config.budget_tokens, message=None)


def resolve_base_url(configured: str | None, env: Mapping[str, str], config_path: Path) -> str:
    """Pick the gateway URL: $ENMAAS_BASE_URL first, then base_url in the config.

    There is no built-in default. Each organization runs its own gateway,
    so a guessed address would send the key somewhere it was never meant
    to go. Raises ValueError with the message to show.

    The URL is the gateway root. The CLI appends /v1/... to it, so a path
    the user typed, such as /v1, is kept as typed. The scheme is lowered,
    the host keeps its case, and trailing slashes are removed.
    """
    url = env.get(BASE_URL_ENV, "").strip() or (configured or "").strip()
    if not url:
        raise ValueError(
            f"no gateway URL configured: set base_url in {config_path}, "
            f"set ${BASE_URL_ENV}, or run ./install.sh --gateway URL"
        )
    return _validate_gateway_url(url)


def _is_loopback(host: str | None) -> bool:
    if host is None:
        # A missing host is reported by its own check below.
        return True
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_gateway_url(url: str) -> str:
    def invalid(reason: str) -> ValueError:
        return ValueError(f"gateway URL is not valid ({reason}): {url}")

    if any(ch.isspace() for ch in url):
        raise invalid("whitespace inside the URL")
    try:
        parts = urlsplit(url)
    except ValueError:
        # urlsplit only raises here for a malformed bracketed IPv6 host.
        raise invalid("missing host") from None
    if parts.scheme.lower() not in {"http", "https"}:
        raise invalid("scheme must be http or https")
    # The key travels in a header, so cleartext http would send it across
    # the network readable to anyone on the path. A local gateway or
    # tunnel is the only place where that is safe.
    if parts.scheme.lower() == "http" and not _is_loopback(parts.hostname):
        raise invalid("http is only allowed for localhost, use https")
    # Checked on the raw text: "https://h?" has an empty query, yet the
    # appended request path would still land after the "?".
    if "?" in url or "#" in url:
        raise invalid("query and fragment are not allowed")
    # The key travels in a header. Credentials in the URL would be a
    # second secret that ends up in logs and error messages.
    if "@" in parts.netloc:
        raise invalid("credentials in the URL are not allowed")
    # Without a host, "https://" plus "/v1/models" would send the key to a
    # host named "v1".
    if not parts.hostname:
        raise invalid("missing host")
    try:
        _ = parts.port
    except ValueError:
        # httpx would raise InvalidURL later, outside any handler.
        raise invalid("invalid port") from None
    # Request paths start with a slash, so a trailing one here would double it.
    return f"{parts.scheme.lower()}://{parts.netloc}{parts.path.rstrip('/')}"


def _setup_error(args: argparse.Namespace, message: str) -> int:
    """Report a URL or key failure before any request is made.

    A --json caller reads stdout only, so an error there must be JSON too.
    An empty stdout would leave it guessing why the run stopped.
    """
    if getattr(args, "json", False):
        print(json.dumps({"status": "error", "error": message}, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)
    return int(ExitCode.ERROR)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"inspect", "allow-read"}:
        try:
            if args.command == "inspect":
                sys.stdout.write(inspect_file(args.path, max_chars=args.max_chars))
            else:
                allow_read(args.path)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return int(ExitCode.ERROR)
        return int(ExitCode.OK)
    config_path = args.config or default_config_path()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        # A malformed [budget] table is the only fatal config error.
        # Proceeding would run uncapped, which is what it was written to
        # prevent, so refuse rather than warn.
        print(f"enmaas: {exc}", file=sys.stderr)
        return 1
    for warning in config.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    needs_gateway = args.command in {"models", "ask", "map"}
    base_url = ""
    key_value = ""
    if needs_gateway:
        try:
            base_url = resolve_base_url(config.base_url, os.environ, config_path)
        except ValueError as exc:
            return _setup_error(args, str(exc))
        try:
            key = resolve_key(
                key_file=args.key_file,
                env=dict(os.environ),
                home=Path.home(),
                # Passed unexpanded: a bad "~user" path must not fail a run
                # where --key-file or $ENMAAS_KEY wins.
                config_key_file=config.key_file or None,
            )
        except AuthError as exc:
            return _setup_error(args, str(exc))
        if key.warning:
            print(f"warning: {key.warning}", file=sys.stderr)
        key_value = key.value

    try:
        return asyncio.run(_dispatch(args, config, base_url, key_value))
    except (DialectError, ValidationError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(ExitCode.ERROR)


async def _dispatch(args: argparse.Namespace, config: Config, base_url: str, key: str) -> int:
    if args.command == "record":
        # A ledger that only sees this CLI's own calls understates reality:
        # a tool talking to the gateway directly is invisible by
        # construction. Recording is a subcommand rather than a library
        # function so a caller in any language can be counted without
        # taking a dependency on this package.
        recorded = append_event(
            state_root() / "usage.jsonl",
            UsageEvent(
                timestamp=datetime.now(UTC),
                command=args.command_name,
                model=args.model,
                dialect=args.dialect,
                input_tokens=args.input,
                cached_tokens=args.cached,
                output_tokens=args.output,
                reasoning_tokens=args.reasoning,
                elapsed=args.elapsed,
                status=args.status,
                schema_used=False,
                error_kind=args.error_kind,
                items_total=None,
                items_ok=None,
                items_failed=None,
                # Derived the same way metered() would, so externally
                # recorded work counts against a budget identically.
                charge=metered(
                    Usage(
                        input_tokens=args.input,
                        cached_tokens=args.cached,
                        output_tokens=args.output,
                        reasoning_tokens=args.reasoning,
                    )
                ),
            ),
        )
        _warn_if_untracked(recorded, config)
        return int(ExitCode.OK)

    # Handle usage command first (no timeout argument)
    if args.command == "usage":
        ledger = state_root() / "usage.jsonl"
        events = load_events(ledger)

        if args.since is not None:
            cutoff = datetime.now(UTC) - timedelta(days=args.since)
            events = [e for e in events if e.timestamp >= cutoff]

        summary = summarize_usage(events)

        if args.json:
            print(format_summary_json(summary, config.prices))
        else:
            print(format_summary(summary, since_days=args.since, prices=config.prices))

        return int(ExitCode.OK)

    # Both spending commands pass through here. Checked before the client
    # is opened, so an over-budget invocation costs nothing at all.
    rolling = rolling_check(config, state_root() / "usage.jsonl")
    if rolling.message and args.command in ("ask", "map"):
        if getattr(args, "json", False):
            # Exit 3 already means "stopped early", so a bare exit code
            # cannot distinguish a budget refusal from a breaker abort.
            # A --json caller gets the same abort_reason vocabulary it
            # would get from a run that started and then stopped.
            print(
                json.dumps(
                    {
                        "status": "refused",
                        "abort_reason": "budget",
                        "budget": {
                            "per_run": None,
                            "rolling": {
                                "ceiling": rolling.ceiling,
                                "spent": rolling.spent,
                            },
                            "tripped": "rolling",
                        },
                        "error": rolling.message,
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"enmaas {args.command} refused\n  budget    {rolling.message}",
                file=sys.stderr,
            )
        return int(ExitCode.ABORTED)

    timeout = args.timeout if args.timeout is not None else config.timeout
    async with httpx.AsyncClient() as client:
        if args.command == "models":
            try:
                rows = await run_models(
                    client=client,
                    base_url=base_url,
                    key=key,
                    dialect=args.dialect or "both",
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                print(f"error: {redact(str(exc), key)}", file=sys.stderr)
                return int(ExitCode.ERROR)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for row in rows:
                    print(f"{row['id']:<45} {','.join(row['dialects'])}")
            return int(ExitCode.OK)

        if args.command == "ask":
            model, model_defaulted = _resolve_model(args.model, config)
            dialect = _resolve_dialect_override(model, args.dialect, config)

            prompt = assemble_prompt(prompt=_read_prompt(args), files=args.file)
            result = await run_ask(
                client=client,
                base_url=base_url,
                key=key,
                model=model,
                prompt=prompt,
                system=args.system,
                dialect=dialect,
                thinking=resolve_thinking(args.thinking, model),
                max_tokens=args.max_tokens,
                timeout=timeout,
                retries=args.retries,
                output=args.output,
                run_root=state_root(),
                save_thinking=args.save_thinking,
                schema_file=args.schema,
                schema_name=args.schema_name,
                model_defaulted=model_defaulted,
            )

            usage_obj = result.usage
            recorded = append_event(
                state_root() / "usage.jsonl",
                UsageEvent(
                    timestamp=datetime.now(UTC),
                    command="ask",
                    model=result.model,
                    dialect=result.dialect,
                    input_tokens=usage_obj.input_tokens if usage_obj else None,
                    cached_tokens=usage_obj.cached_tokens if usage_obj else None,
                    output_tokens=usage_obj.output_tokens if usage_obj else None,
                    reasoning_tokens=usage_obj.reasoning_tokens if usage_obj else None,
                    elapsed=result.elapsed,
                    status="ok" if result.exit_code == ExitCode.OK else "error",
                    schema_used=args.schema is not None,
                    error_kind=result.error_kind,
                    items_total=None,
                    items_ok=None,
                    items_failed=None,
                    charge=metered(usage_obj) if usage_obj else 0,
                ),
            )
            _warn_if_untracked(recorded, config)

            if args.json:
                # Machine-readable output
                usage = result.usage
                ask_obj = {
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
                    # --json suppresses the receipt, so without this a
                    # repaired response would look untouched to a caller
                    # that only reads the machine-readable output.
                    "replaced": (result.completion.replaced if result.completion else 0),
                    "error": result.error,
                }
                print(json.dumps(ask_obj, indent=2))
            elif args.stdout and result.exit_code == ExitCode.OK:
                print(result.completion.text if result.completion else "")
                print(result.receipt, file=sys.stderr)
            else:
                stream = sys.stdout if result.exit_code == ExitCode.OK else sys.stderr
                print(result.receipt, file=stream)
            return result.exit_code

        model, model_defaulted = _resolve_model(args.model, config)
        dialect = _resolve_dialect_override(model, args.dialect, config)
        batch_defaulted = args.batch is None

        outcome = await run_map(
            client=client,
            base_url=base_url,
            key=key,
            model=model,
            items_path=args.items,
            template_path=args.template,
            outdir=args.outdir,
            concurrency=args.concurrency if args.concurrency is not None else config.concurrency,
            retries=args.retries,
            timeout=timeout,
            thinking=resolve_thinking(args.thinking, model),
            max_tokens=args.max_tokens,
            dialect=dialect,
            resume=args.resume,
            schema_file=args.schema,
            schema_name=args.schema_name,
            batch=args.batch,
            token_budget=args.token_budget,
            # Seeded with what the window has already consumed, so a long
            # run that crosses the standing ceiling partway stops there
            # rather than only being caught at the next invocation.
            rolling_budget=(
                TokenBudget(rolling.ceiling, spent=rolling.spent)
                if rolling.ceiling is not None
                else None
            ),
        )

        # Log usage (resolve dialect for logging)
        resolved_dialect = resolve_dialect(model, dialect)
        error_kind = None
        if outcome.abort_reason is not None or outcome.failed > 0:
            # Was hardcoded to CircuitBreakerError for any abort, which
            # would have filed a budget stop as a breaker trip.
            error_kind = {
                "breaker": "CircuitBreakerError",
                "budget": "TokenBudgetExceeded",
            }.get(outcome.abort_reason or "", "PartialFailure")
        recorded = append_event(
            state_root() / "usage.jsonl",
            UsageEvent(
                timestamp=datetime.now(UTC),
                command="map",
                model=model,
                dialect=resolved_dialect,
                input_tokens=outcome.input_tokens,
                cached_tokens=outcome.cached_tokens,
                output_tokens=outcome.output_tokens,
                reasoning_tokens=outcome.reasoning_tokens,
                elapsed=outcome.elapsed,
                status="error" if outcome.abort_reason is not None else "ok",
                schema_used=args.schema is not None,
                error_kind=error_kind,
                items_total=outcome.total,
                items_ok=outcome.ok,
                items_failed=outcome.failed,
                # Not derived from the counters above: this is the sum of
                # what each request actually cost, failures included.
                charge=outcome.charged,
            ),
        )
        _warn_if_untracked(recorded, config)

        if args.json:
            status = (
                "aborted"
                if outcome.abort_reason is not None
                else ("partial" if outcome.failed > 0 else "ok")
            )
            obj: dict[str, Any] = {
                "status": status,
                "model": model,
                "batch": outcome.batch,
                "defaulted": {"model": model_defaulted, "batch": batch_defaulted},
                # Which of the two stop conditions fired. "aborted" alone
                # cannot distinguish failures from overspending.
                "abort_reason": outcome.abort_reason,
                "total": outcome.total,
                "ok": outcome.ok,
                "failed": outcome.failed,
                "skipped": outcome.skipped,
                "attempted": outcome.attempted,
                "outdir": str(outcome.outdir),
                "index": str(outcome.outdir / "results.jsonl"),
                "tokens": {
                    "input": outcome.input_tokens,
                    "output": outcome.output_tokens,
                },
                "elapsed": outcome.elapsed,
                # Both ceilings reported separately, and "tripped"
                # names which one stopped the run. A single flattened
                # object could not say whether to raise the flag or the
                # config, which are different remedies.
                "budget": {
                    "per_run": (
                        None
                        if not args.token_budget
                        else {
                            # Job-wide when resumed, matching what the
                            # ceiling is compared against.
                            "ceiling": args.token_budget,
                            "spent": outcome.spent,
                        }
                    ),
                    "rolling": (
                        None
                        if rolling.ceiling is None
                        else {
                            "ceiling": rolling.ceiling,
                            # The accumulator, not rolling.spent +
                            # outcome.spent: on a resumed job the latter
                            # already carries prior invocations and so
                            # counts them twice.
                            "spent": (
                                outcome.rolling_spent
                                if outcome.rolling_spent is not None
                                else rolling.spent + outcome.charged
                            ),
                        }
                    ),
                    "tripped": outcome.tripped_budget,
                },
                "failures": [
                    {"id": item_id, "error": message} for item_id, message in outcome.failures
                ],
            }
            print(json.dumps(obj, indent=2))
        else:
            print(
                map_receipt(
                    outcome,
                    model=model,
                    token_budget=args.token_budget,
                    rolling_ceiling=rolling.ceiling,
                    rolling_spent=rolling.spent,
                    model_defaulted=model_defaulted,
                    batch_defaulted=batch_defaulted,
                )
            )
        return outcome.exit_code
