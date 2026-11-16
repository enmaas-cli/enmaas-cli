"""Argument parsing, command dispatch, exit codes."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import math
import os
import signal
import sys
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NoReturn, TextIO

import httpx

from . import gateway, openruns
from .ask import send_ask
from .auth import AuthError, error_text, printable, printable_block, redact, resolve_key
from .budget import RollingCheck, TokenBudget, metered, rolling_check
from .client import CallSettings, _reply_object, make_client
from .config import Config, ConfigError, default_config_path, load_config
from .dialect import (
    ANTHROPIC_VERSION,
    Dialect,
    DialectError,
    resolve_dialect,
)
from .errors import BadReplyError, CallError, ExitCode, where
from .outputs import (
    MAX_INSPECT_CHARS,
    allow_read,
    inspect_file,
    state_root,
)
from .receipts import (
    ask_json,
    map_json,
    map_receipt,
    refusal_json,
    refusal_receipt,
)
from .runner import MapJob, run_job
from .schema import ValidationError
from .tokens import MAX_TOKEN_COUNT, Usage
from .usage import (
    LedgerWrite,
    ask_event,
    can_append,
    format_summary,
    format_summary_json,
    load_events_counted,
    map_event,
    recorded_event,
    summarize_usage,
    write_event,
)


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
    # One count from the command line, held to the same limit as one count
    # from a reply. Recorded sums are clamped to MAX_RECORDED when written.
    if number > MAX_TOKEN_COUNT:
        raise argparse.ArgumentTypeError(f"{value} is above {MAX_TOKEN_COUNT}")
    return number


def nonnegative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value} is not a number") from None
    # The ledger reader refuses nan and inf, so they are never written.
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError(f"{value} is not a finite number")
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

    load_config has already dropped, with a warning, every value that is not
    a dialect.
    """
    if flag is not None:
        return flag
    return config.dialect_overrides.get(model)


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


async def run_models(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    dialect: Dialect | Literal["both"],
    timeout: float,
) -> list[dict[str, Any]]:
    """Query one or both catalogs. Neither is complete on its own."""
    wanted: list[Dialect] = ["anthropic", "openai"] if dialect == "both" else [dialect]
    found: dict[str, set[str]] = {}
    for d in wanted:
        headers = (
            {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
            if d == "anthropic"
            else {"Authorization": f"Bearer {key}"}
        )
        response = await client.get(f"{base_url}/v1/models", headers=headers, timeout=timeout)
        response.raise_for_status()
        # A catalog of the wrong shape is a bad reply. The messages name
        # the problem only, never the body.
        body = _reply_object(response, key)
        entries = body.get("data")
        if not isinstance(entries, list):
            raise BadReplyError(f"the {d} model catalog has no list of models")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise BadReplyError(
                    f"the {d} model catalog holds an entry that is not an object with a string id"
                )
            found.setdefault(entry["id"], set()).add(d)
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


def _warn_if_untracked(written: LedgerWrite, config: Config) -> None:
    """Say so when a ledger row was not written down.

    A write that was tried and failed is always reported, budget or not:
    otherwise a user whose ledger stopped taking rows, for example because
    it is a link an older version followed, learns nothing until the
    totals look wrong. An unwritable ledger also looks exactly like one
    showing no spend, so a configured ceiling would quietly stop binding.
    The work is already paid for, so this warns rather than failing.
    """
    if written.failure is not None:
        print(f"warning: usage was not recorded: {written.failure}", file=sys.stderr)
    if not written.recorded and config.budget_tokens is not None:
        print(
            "warning: a token budget is configured but this run's usage was "
            "not recorded, so it will not count against the ceiling",
            file=sys.stderr,
        )


# How each gateway URL problem is worded. The text after "not valid" is
# part of the message a user or a script may match.
GATEWAY_PROBLEMS = {
    gateway.WHITESPACE: "whitespace inside the URL",
    gateway.SCHEME: "scheme must be http or https",
    gateway.CLEARTEXT: "http is only allowed for localhost, use https",
    gateway.QUERY: "query and fragment are not allowed",
    gateway.CREDENTIALS: "credentials in the URL are not allowed",
    gateway.HOST: "missing host",
    gateway.PORT: "invalid port",
    gateway.EMPTY_PORT: "empty port",
}


def resolve_base_url(configured: str | None, env: Mapping[str, str], config_path: Path) -> str:
    """Pick the gateway URL: $ENMAAS_BASE_URL first, then base_url in the config.

    There is no built-in default. Each organization runs its own gateway,
    so a guessed address would send the key somewhere it was never meant
    to go. Raises ValueError with the message to show.
    """
    url = gateway.choose(configured, env)
    if not url:
        raise ValueError(
            f"no gateway URL configured: set base_url in {config_path}, "
            f"set ${gateway.BASE_URL_ENV}, or run ./install.sh --gateway URL"
        )
    checked = gateway.check(url)
    if checked.problem is not None:
        raise ValueError(f"gateway URL is not valid ({GATEWAY_PROBLEMS[checked.problem]})")
    return checked.url


def _setup_error(args: argparse.Namespace, message: str) -> int:
    """Report an error that ends the command, as its one result.

    A --json caller reads stdout only, so an error there must be JSON too.
    An empty stdout would leave it guessing why the run stopped.
    """
    if getattr(args, "json", False):
        print(json.dumps({"status": "error", "error": message}, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)
    return int(ExitCode.ERROR)


def _error_text(message: str, key: str) -> str:
    """The text of an error that ends a command.

    The key is removed first, then the text is made printable on one line,
    then cut. Redaction comes before the cut, or part of a key could survive.
    """
    return error_text(message, key)


def _emit(text: str, stream: TextIO | None = None) -> None:
    """Print one block and flush it. A reader that went away is not an error.

    The work is done and recorded before anything is printed, so a closed
    pipe must not turn a finished run into a failure or a traceback. The
    stream defaults to the current sys.stdout at call time, not at import.
    """
    target = sys.stdout if stream is None else stream
    try:
        print(text, file=target, flush=True)
    except BrokenPipeError:
        # Point the descriptor at /dev/null, so the flush at exit also
        # succeeds and Python does not report the closed pipe.
        with contextlib.suppress(OSError, ValueError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, target.fileno())
            finally:
                os.close(devnull)


@dataclass
class Interrupt:
    """The first stop signal this process received, if any."""

    signum: int | None = None
    # Set once the interrupted error has been printed, so it is printed once.
    reported: bool = False


@dataclass
class Final:
    """The exit code of a command that has finished and is printing its result.

    Set by each command's handler just before its final output,
    refusals and errors included. From then on a stop signal changes
    nothing: the ledger already says what happened, and the process exits
    with this code. The one exception is a result that itself reports the
    interrupt, which ends by the signal.
    """

    code: int | None = None
    # True when the printed result is a run stopped by the signal.
    interrupted: bool = False

    def overrides(self, interrupt: Interrupt) -> bool:
        """Whether this result decides the exit, so a pending signal is dropped."""
        if self.code is None or self.interrupted:
            return False
        interrupt.signum = None
        return True


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _install_handlers(handler: Callable[[int, object], None]) -> dict[int, Any]:
    """Install `handler` for both stop signals and return the handlers it replaced."""
    previous: dict[int, Any] = {}
    for signum in STOP_SIGNALS:
        # ValueError: not the main thread. OSError: the platform refused.
        with contextlib.suppress(ValueError, OSError):
            if signal.getsignal(signum) is signal.SIG_IGN:
                # A background job started with the signal ignored keeps
                # ignoring it.
                continue
            previous[signum] = signal.signal(signum, handler)
    return previous


def _restore_handlers(previous: dict[int, Any], *, process: bool) -> None:
    for number, handler in previous.items():
        with contextlib.suppress(ValueError, OSError):
            if process:
                signal.signal(number, signal.SIG_IGN)
            else:
                # A previous handler of None was not set from Python and
                # cannot be passed back; the default is the nearest.
                signal.signal(number, signal.SIG_DFL if handler is None else handler)


async def _guarded(
    args: argparse.Namespace,
    config: Config,
    base_url: str,
    key: str,
    interrupt: Interrupt,
    final: Final,
    *,
    process: bool = False,
) -> int:
    """Run the command with SIGINT and SIGTERM handled the way asyncio.Runner handles SIGINT.

    The first signal cancels the command, so an interrupted map writes its
    ledger event and prints its receipt before the process ends by the same
    signal. The second signal ends the process at once, even while it waits
    on a lock or a file, because a Python signal handler runs when a
    blocking system call is interrupted.

    Installed with signal.signal. A signal that is ignored when the command
    starts stays ignored. In-process, the previous handlers are put back on
    the way out, so calling main leaves them as they were. In process mode
    they are not: both signals are ignored from then on, because a handler
    put back would let a late signal end a finished run by its default
    action or print a traceback after main returned.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def cancel() -> None:
        # The result may have been reported by the time this runs. A cancel
        # then changes nothing, so none is sent.
        if final.code is None and task is not None:
            task.cancel()

    def on_signal(signum: int, frame: object) -> None:
        if final.code is not None and not final.interrupted:
            # The command finished, recorded and is printing its result. A
            # signal now changes nothing, so it is not even recorded.
            return
        if interrupt.signum is None:
            interrupt.signum = signum
            if final.code is not None:
                # The command finished and printed. A cancel now changes
                # nothing, so none is sent.
                return
            # Scheduled, not called here. A Python signal handler can run in
            # the middle of another callback, and cancelling there can cancel
            # a future between its done() check and set_result(). This also
            # wakes the loop, as asyncio.Runner does for SIGINT.
            loop.call_soon_threadsafe(cancel)
            return
        # Second signal. End now by that signal. An open-run record that is
        # still present is settled by the next command, so the spend is kept.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    previous = _install_handlers(on_signal)
    try:
        code = await _dispatch(args, config, base_url, key, final)
        # A cancel that arrived after the work was done can be absorbed by
        # an await on the way out, such as closing the client. The printed
        # result still decides the exit.
        final.overrides(interrupt)
        return code
    except asyncio.CancelledError:
        if final.code is not None:
            # The command had finished and printed before the cancel was
            # seen. It exits with its own code, or by the signal when the
            # result reports the interrupt.
            final.overrides(interrupt)
            return final.code
        interrupt.reported = True
        return _setup_error(args, "interrupted")
    finally:
        _restore_handlers(previous, process=process)


def _final_error(args: argparse.Namespace, final: Final, interrupt: Interrupt, message: str) -> int:
    """Print an error that escaped the command, as the command's result.

    The exit code is set before the print, as for every final output, so a
    stop signal from then on changes nothing. If the command had already
    printed its result, a second JSON object would break a --json reader,
    so the error goes to stderr as text and the result's code stands.
    """
    if final.code is None:
        final.code = int(ExitCode.ERROR)
        _setup_error(args, message)
    else:
        print(f"error: {message}", file=sys.stderr)
    code = final.code
    final.overrides(interrupt)
    return code


def _flush_quietly() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()


def _inspect(args: argparse.Namespace) -> int:
    # The file can hold an answer, so its text is printed inert. Its lines
    # and tabs are kept.
    text = inspect_file(args.path, max_chars=args.max_chars)
    sys.stdout.write(printable_block(text))
    return int(ExitCode.OK)


def _allow_read(args: argparse.Namespace) -> int:
    allow_read(args.path)
    return int(ExitCode.OK)


# Commands that read no config, no key and no network.
LOCAL_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "inspect": _inspect,
    "allow-read": _allow_read,
}
GATEWAY_COMMANDS = frozenset({"models", "ask", "map"})
# The commands that send requests that cost tokens.
SPENDING_COMMANDS = frozenset({"ask", "map"})


def _local(args: argparse.Namespace) -> int:
    try:
        return LOCAL_COMMANDS[args.command](args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(ExitCode.ERROR)


def _load_config(args: argparse.Namespace, config_path: Path) -> Config | None:
    """The config, with its warnings printed. None when the command must stop."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        # A malformed [budget] table is the only fatal config error.
        # Proceeding would run uncapped, which is what it was written to
        # prevent, so refuse rather than warn.
        if getattr(args, "json", False):
            _setup_error(args, _error_text(str(exc), ""))
        else:
            print(f"enmaas: {exc}", file=sys.stderr)
        return None
    for warning in config.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return config


def _gateway_access(
    args: argparse.Namespace, config: Config, config_path: Path
) -> tuple[str, str] | None:
    """The gateway URL and the key, empty for a command that sends nothing.

    None when either could not be found, after the error is reported.
    """
    if args.command not in GATEWAY_COMMANDS:
        return "", ""
    try:
        base_url = resolve_base_url(config.base_url, os.environ, config_path)
    except ValueError as exc:
        _setup_error(args, str(exc))
        return None
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
        _setup_error(args, str(exc))
        return None
    if key.warning:
        print(f"warning: {key.warning}", file=sys.stderr)
    return base_url, key.value


def _late_cancel(args: argparse.Namespace, final: Final, interrupt: Interrupt) -> int:
    """The exit after a cancel that was still pending when the command returned.

    Such a cancel cancels the task itself, outside every handler in _guarded.
    """
    if final.code is not None:
        final.overrides(interrupt)
        return final.code
    if not interrupt.reported:
        interrupt.reported = True
        _setup_error(args, "interrupted")
    return int(ExitCode.ERROR)


def _run_guarded(
    args: argparse.Namespace,
    config: Config,
    base_url: str,
    key: str,
    interrupt: Interrupt,
    final: Final,
    *,
    process: bool,
) -> int:
    """Run the command in an event loop. An error that escapes it is its one result."""
    try:
        code = asyncio.run(_guarded(args, config, base_url, key, interrupt, final, process=process))
    except asyncio.CancelledError:
        code = _late_cancel(args, final, interrupt)
    except (DialectError, ValidationError, ValueError, OSError) as exc:
        code = _final_error(args, final, interrupt, _error_text(str(exc), key))
    except Exception as exc:
        # Never the exception text: an error no handler expected can carry
        # part of a reply or a prompt. Where it happened is enough to report
        # it. CancelledError and KeyboardInterrupt are not Exception
        # subclasses, so they reach the handlers for them.
        message = f"internal error ({where(exc)})"
        code = _final_error(args, final, interrupt, _error_text(message, key))
        if os.environ.get("ENMAAS_DEBUG") == "1":
            print(redact(traceback.format_exc(), key), file=sys.stderr, end="")
    if interrupt.signum is not None:
        # A further signal while the output is flushed must not raise.
        for signum in STOP_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        _flush_quietly()
    return code


def _interrupted_outside(args: argparse.Namespace, final: Final, interrupt: Interrupt) -> int:
    """The exit after SIGINT outside the window where _guarded handles it.

    That is either before its handler is installed or after it is removed.
    """
    if final.overrides(interrupt) and final.code is not None:
        # The command had finished and printed. Nothing changes.
        return final.code
    if interrupt.signum is None:
        interrupt.signum = signal.SIGINT
    if not interrupt.reported and final.code is None:
        # A --json caller still gets one object.
        interrupt.reported = True
        _setup_error(args, "interrupted")
    return int(ExitCode.ERROR)


def main(argv: list[str] | None = None, *, process: bool = False) -> int:
    """Run one command and return its exit code.

    `process` is True only for the console script. Then the stop signals are
    not put back when the command ends: the process either ends by a signal
    it recorded, or returns its code with both signals ignored until it
    exits. In-process callers keep their own handlers.
    """
    args = build_parser().parse_args(argv)
    if args.command in LOCAL_COMMANDS:
        return _local(args)
    config_path = args.config or default_config_path()
    config = _load_config(args, config_path)
    if config is None:
        return int(ExitCode.ERROR)
    access = _gateway_access(args, config, config_path)
    if access is None:
        return int(ExitCode.ERROR)
    base_url, key = access

    interrupt = Interrupt()
    final = Final()
    try:
        code = _run_guarded(args, config, base_url, key, interrupt, final, process=process)
    except KeyboardInterrupt:
        code = _interrupted_outside(args, final, interrupt)
    if interrupt.signum is not None:
        # End by the same signal, so the parent sees how the process stopped
        # (130 or 143 in a shell) rather than an ordinary exit code.
        _flush_quietly()
        signal.signal(interrupt.signum, signal.SIG_DFL)
        os.kill(os.getpid(), interrupt.signum)
        return 128 + interrupt.signum
    return code


def _record(args: argparse.Namespace, config: Config, final: Final) -> int:
    """Record a run this CLI did not make.

    A ledger that only sees this CLI's own calls understates reality: a
    tool talking to the gateway directly is invisible by construction.
    Recording is a subcommand rather than a library function so a caller in
    any language can be counted without taking a dependency on this
    package.
    """
    usage = Usage(
        input_tokens=args.input,
        cached_tokens=args.cached,
        output_tokens=args.output,
        reasoning_tokens=args.reasoning,
    )
    event = recorded_event(
        command=args.command_name,
        model=args.model,
        dialect=args.dialect,
        usage=usage,
        elapsed=args.elapsed,
        status=args.status,
        error_kind=args.error_kind,
        # Charged by metered(), as a request this CLI sends is, so
        # externally recorded work counts against a budget identically.
        charge=metered(usage),
    )
    written = write_event(state_root() / "usage.jsonl", event)
    # Every handler sets this before its final output, so a stop signal
    # from then on changes nothing.
    final.code = int(ExitCode.OK)
    _warn_if_untracked(written, config)
    return int(ExitCode.OK)


def _usage(args: argparse.Namespace, config: Config, final: Final) -> int:
    ledger = state_root() / "usage.jsonl"
    events, skipped, not_regular = load_events_counted(ledger)
    final.code = int(ExitCode.OK)
    if not_regular:
        print(
            f"warning: {printable(str(ledger))} is not a regular file, "
            "so no usage was read from it",
            file=sys.stderr,
        )
    if skipped:
        print(
            f"warning: {skipped} rows of {printable(str(ledger))} could not be read "
            "and were skipped",
            file=sys.stderr,
        )

    if args.since is not None:
        cutoff = datetime.now(UTC) - timedelta(days=args.since)
        events = [e for e in events if e.timestamp >= cutoff]

    summary = summarize_usage(events)

    if args.json:
        print(format_summary_json(summary, config.prices, skipped_rows=skipped))
    else:
        print(format_summary(summary, since_days=args.since, prices=config.prices))

    return int(ExitCode.OK)


def _refused(
    args: argparse.Namespace, rolling: RollingCheck, final: Final, key: str = ""
) -> int | None:
    """Refuse a spending command the budget does not allow. None when it may go on."""
    if rolling.message:
        final.code = int(ExitCode.ABORTED)
        if getattr(args, "json", False):
            print(json.dumps(refusal_json(rolling, key=key), indent=2))
        else:
            print(refusal_receipt(args.command, rolling, key=key), file=sys.stderr)
        return int(ExitCode.ABORTED)
    if rolling.ceiling is not None and not can_append(state_root() / "usage.jsonl"):
        # A ledger that cannot be written reads as no spend on the next run,
        # so every run would start from zero and the budget would never bind.
        final.code = int(ExitCode.ERROR)
        return _setup_error(
            args,
            _error_text(
                f"the usage ledger {state_root() / 'usage.jsonl'} cannot be "
                "written, so the token budget cannot be tracked; fix it or remove the [budget] "
                "table from the config",
                key,
            ),
        )
    return None


async def _models(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    base_url: str,
    key: str,
    timeout: float,
    final: Final,
) -> int:
    try:
        rows = await run_models(
            client=client,
            base_url=base_url,
            key=key,
            dialect=args.dialect or "both",
            timeout=timeout,
        )
    except (httpx.HTTPError, CallError) as exc:
        final.code = int(ExitCode.ERROR)
        return _setup_error(args, _error_text(str(exc), key))
    final.code = int(ExitCode.OK)
    if args.json:
        # The same redaction as the text list. JSON encoding already
        # escapes control characters, so nothing else is needed.
        print(
            json.dumps(
                [{**row, "id": redact(row["id"], key)} for row in rows],
                indent=2,
            )
        )
    else:
        for row in rows:
            # A catalog id is gateway text, so it is printed inert.
            model_id = printable(redact(row["id"], key))
            print(f"{model_id:<45} {','.join(row['dialects'])}")
    return int(ExitCode.OK)


async def _ask(
    client: httpx.AsyncClient,
    settings: CallSettings,
    args: argparse.Namespace,
    config: Config,
    final: Final,
) -> int:
    model, model_defaulted = _resolve_model(args.model, config)
    dialect = _resolve_dialect_override(model, args.dialect, config)

    prompt = assemble_prompt(prompt=_read_prompt(args), files=args.file)
    result = await send_ask(
        client,
        settings,
        model=model,
        prompt=prompt,
        system=args.system,
        dialect=dialect,
        thinking=resolve_thinking(args.thinking, model),
        max_tokens=args.max_tokens,
        output=args.output,
        run_root=state_root(),
        save_thinking=args.save_thinking,
        schema_file=args.schema,
        schema_name=args.schema_name,
        model_defaulted=model_defaulted,
    )

    usage = result.usage
    event = ask_event(
        result,
        schema_used=args.schema is not None,
        charge=metered(usage) if usage else 0,
    )
    written = write_event(state_root() / "usage.jsonl", event)
    _warn_if_untracked(written, config)
    # Reported before the result is printed. From here a stop signal
    # changes nothing.
    final.code = result.exit_code

    if args.json:
        _emit(json.dumps(ask_json(result), indent=2))
    elif args.stdout and result.exit_code == ExitCode.OK:
        _emit(result.completion.text if result.completion else "")
        _emit(result.receipt, sys.stderr)
    else:
        stream = sys.stdout if result.exit_code == ExitCode.OK else sys.stderr
        _emit(result.receipt, stream)
    return result.exit_code


async def _map(
    client: httpx.AsyncClient,
    settings: CallSettings,
    args: argparse.Namespace,
    config: Config,
    rolling: RollingCheck,
    final: Final,
) -> int:
    model, model_defaulted = _resolve_model(args.model, config)
    dialect = _resolve_dialect_override(model, args.dialect, config)
    batch_defaulted = args.batch is None
    # Resolved before the run, because the open-run record carries it.
    resolved_dialect = resolve_dialect(model, dialect)
    # When the run raises, the record stays on purpose. The next ask or map
    # settles it, so spend from a run that died on an error counts.
    record = openruns.begin(state_root(), outdir=args.outdir, model=model, dialect=resolved_dialect)

    job = MapJob(
        model=model,
        items_path=args.items,
        template_path=args.template,
        outdir=args.outdir,
        concurrency=args.concurrency if args.concurrency is not None else config.concurrency,
        thinking=resolve_thinking(args.thinking, model),
        max_tokens=args.max_tokens,
        dialect=dialect,
        resume=args.resume,
        schema_file=args.schema,
        schema_name=args.schema_name,
        batch=args.batch,
        token_budget=args.token_budget,
        # Seeded with what the window has already consumed, so a long run
        # that crosses the standing ceiling partway stops there rather than
        # only being caught at the next invocation.
        rolling_budget=(
            TokenBudget(rolling.ceiling, spent=rolling.spent)
            if rolling.ceiling is not None
            else None
        ),
        # Tags the charge rows, so a settle counts only this run's rows.
        run_id=record.run_id if record is not None else None,
    )
    outcome = await run_job(client, settings, job)

    event = map_event(
        outcome, model=model, dialect=resolved_dialect, schema_used=args.schema is not None
    )
    written = write_event(state_root() / "usage.jsonl", event)
    _warn_if_untracked(written, config)
    # Removed after the ledger event, never before. A kill between the two
    # counts this run twice, which only over-counts. The other order could
    # lose the spend. When the ledger did not take the event, the record
    # stays, so a later command settles the run once it can.
    if written.recorded:
        openruns.end(state_root(), record)
    # Reported before the result is printed. From here a stop signal
    # changes nothing.
    final.code = outcome.exit_code
    final.interrupted = outcome.abort_reason == "interrupted"

    if args.json:
        obj = map_json(
            outcome,
            model=model,
            token_budget=args.token_budget,
            rolling_ceiling=rolling.ceiling,
            rolling_spent=rolling.spent,
            model_defaulted=model_defaulted,
            batch_defaulted=batch_defaulted,
        )
        _emit(json.dumps(obj, indent=2))
    else:
        receipt = map_receipt(
            outcome,
            model=model,
            token_budget=args.token_budget,
            rolling_ceiling=rolling.ceiling,
            rolling_spent=rolling.spent,
            model_defaulted=model_defaulted,
            batch_defaulted=batch_defaulted,
        )
        _emit(receipt)
    return outcome.exit_code


async def _dispatch(
    args: argparse.Namespace, config: Config, base_url: str, key: str, final: Final
) -> int:
    """Run a command that reads the config, through its handler."""
    if args.command == "record":
        return _record(args, config, final)
    if args.command == "usage":
        return _usage(args, config, final)

    if args.command in SPENDING_COMMANDS:
        # Charge any map run that died without writing its ledger event,
        # before the budget is checked, so the check sees that spend.
        openruns.settle(state_root(), state_root() / "usage.jsonl")

    # Both spending commands pass through here. Checked before the client
    # is opened, so an over-budget invocation costs nothing at all.
    rolling = rolling_check(config, state_root() / "usage.jsonl")
    if args.command in SPENDING_COMMANDS:
        refused = _refused(args, rolling, final, key)
        if refused is not None:
            return refused

    timeout = args.timeout if args.timeout is not None else config.timeout
    async with make_client(base_url) as client:
        if args.command == "models":
            return await _models(client, args, base_url, key, timeout, final)
        settings = CallSettings(base_url=base_url, key=key, timeout=timeout, retries=args.retries)
        if args.command == "ask":
            return await _ask(client, settings, args, config, final)
        return await _map(client, settings, args, config, rolling, final)


def entry() -> NoReturn:
    """The console script. Runs main in process mode and exits with its code."""
    sys.exit(main(process=True))
