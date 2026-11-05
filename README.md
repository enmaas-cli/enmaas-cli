# enmaas-cli

This package is distributed as part of the `enmaas` plugin in
RedHatTraining/ptl-marketplace, which is its home. For installation,
credentials and usage guidance, read the plugin README one directory up; this
file documents the package itself.

A CLI for delegating work to EnMaaS-hosted models. Responses are written to files and only a short receipt goes to stdout, so an agent that shells out to it does not pay to read the whole answer.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

Or with uv:

```bash
uv tool install -e .
```

The API key is read from `--key-file`, then `$ENMAAS_KEY`, then `~/.secrets/enmaas`.

## Commands

### List models

```bash
enmaas models
```

Lists the merged catalog from both the anthropic and openai dialect endpoints.

### Ask a single question

```bash
echo "Name the capital of France in one word." | enmaas ask -m rits/zai-org/glm-5-3
```

Output:

```
enmaas ask ok
  model     rits/zai-org/glm-5-3  (openai dialect)
  output    /home/user/.local/state/enmaas/runs/2026-10-26T20-01-27-cfa2/response.md  (5 B)
  tokens    in 21   out 3
  elapsed   1.3s
  preview   Paris
```

The full response is written to the file shown in the `output` line. The receipt includes a short preview.

On failure, the receipt goes to stderr and the exit code is 1, allowing shell-level branching on success vs. error.

### Map over multiple items

```bash
enmaas map -m rits/zai-org/glm-5-3 --items items.jsonl --template template.txt --outdir out --concurrency 4
```

Output:

```
enmaas map ok
  items     8 total: 8 ok, 0 failed, 0 skipped
  outdir    out
  tokens    in 224   out 40
  elapsed   5.0s
```

Each item in `items.jsonl` must have an `id` field. The `id` value must be a safe filename (no path separators or other filesystem-sensitive characters). The template can reference any other fields using `{field_name}` syntax. Output files are written to `outdir/<id>.md`.

The `--resume` flag skips items that both have an output file in the outdir and an `ok` row in `results.jsonl`, allowing you to continue an interrupted bulk run without re-processing completed items.

## Options

### Common flags

- `--json`: Emit machine-readable JSON instead of plain-text receipts. For `ask`, prints an object with `status`, `model`, `dialect`, `path`, `tokens`, `elapsed`, and `error`. For `map`, prints an object with `status`, `total`, `ok`, `failed`, `skipped`, `attempted`, `outdir`, `index`, `tokens`, `elapsed`, and `failures`.
- `--dialect {anthropic,openai}`: Override dialect selection. By default, the dialect is inferred from the model name.
- `--max-tokens N`: Maximum output tokens. Must be positive.
- `--timeout SECONDS`: Request timeout. Must be positive. Defaults to 280 seconds.
- `--thinking {on,off}`: Control reasoning token generation. Defaults to `off` for `rits/*` models and provider default for frontier models.

### Ask-specific flags

- `-p, --prompt TEXT`: Prompt text.
- `--prompt-file PATH`: Read prompt from file.
- `-f, --file PATH`: Attach a file to the prompt. Can be specified multiple times.
- `--system TEXT`: System message.
- `-o, --output PATH`: Write response to this path instead of a timestamped run directory.
- `--stdout`: Print the response body to stdout and the receipt to stderr.
- `--save-thinking`: Save reasoning tokens to a separate file (when available).
- `--retries N`: Number of retries on transient failures. Defaults to 3.

If no prompt is given via `-p` or `--prompt-file`, stdin is read.

### Map-specific flags

- `--items PATH`: JSONL file with items to process. Each item must have an `id` field.
- `--template PATH`: Jinja2 template file for generating prompts.
- `--outdir PATH`: Output directory for response files.
- `--concurrency N`: Number of concurrent requests. Must be positive. Defaults to 1.
- `--retries N`: Number of retries per item. Defaults to 3.
- `--resume`: Skip items that already have an output file and an `ok` row in `results.jsonl`.
- `--token-budget N`: Stop the run once it has spent N metered tokens. Not the same as `--max-tokens`, which caps the output of a single request. See [Token budget](#token-budget).

## Model selection

**Free models** (zero usage cost):
- `rits/zai-org/glm-5-3` (1M context)
- `rits/deepseek-ai/DeepSeek-V4.1-Flash-1017`

**Budget-consuming models**:
- `claude-*` family
- `gpt-*` family
- `gemini-*` family

For a second opinion on work done by a Claude model, pick a different family. Shared training lineage means shared blind spots.

## Reasoning costs output budget

**This is the most important operational note.** Reasoning tokens come out of `max_tokens`, so on the hosted models reasoning can consume the entire budget and return zero text with HTTP 200.

Hosted `rits/*` models default to `--thinking off` for this reason. Frontier models (`gpt-*`, `claude-*`, `gemini-*`) are left to the provider's default, since they manage their own reasoning budgets and have not shown this behavior.

Measured on a 45 KB prompt with `rits/*` models:
- With `--thinking on`: GLM returned a 504 at 300s; DeepSeek produced 32,768 tokens of invisible reasoning and zero text.
- With `--thinking off`: GLM returned 8,258 chars in 35.7s; DeepSeek returned 12,587 chars in 18.6s.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (includes usage errors like invalid flags) |
| 2 | Partial failure (some items failed in a map run) |
| 3 | Run stopped early: circuit breaker tripped, or token budget spent |

Usage errors (invalid flags, missing required arguments) exit 1, so exit code 2 unambiguously means partial failure.

On exit code 3, re-run with `--resume` to skip already-completed items and continue from where the circuit breaker stopped the run.

## Circuit breaker

The circuit breaker aborts a bulk map run on:
- 6 consecutive failures, or
- More than 50% failures in a full 20-item window

Whichever comes first. This catches a run that is failing, but not one where
every call succeeds and the spend is the problem. That is what the token budget
below is for.

## Token budget

`--token-budget N` stops a map run once it has spent N metered tokens, exiting 3
the way a tripped circuit breaker does.

**Metered means uncached input plus output.** Cached input is excluded, so
batching helps a run go further under the same ceiling. Cache writes are charged,
because writing a cache entry is real work. Reasoning is charged too, but it needs
no separate term: it is already inside the output count on both dialects.

**It stops scheduling rather than hard-stopping.** At `--concurrency N`, up to
N-1 further requests are already in flight and will finish. With `--batch`, those
are N-1 further groups, which can be a lot of tokens. There is no fixed token
bound without bounding request size.

**The ceiling belongs to the job, not to one command.** Every request appends a
`{"type": "request", "charge": N}` row to `results.jsonl`, and a resumed run adds
those up before spending anything. So `--resume` on its own re-trips immediately.
To carry on, raise the ceiling deliberately:

```bash
enmaas map ... --token-budget 2000000 --resume
```

That is the intended friction. A budget that reset on every invocation could be
bypassed by pressing up-arrow and Enter.

**What it does not catch.** `call` retries internally, and a non-200 response
carries no usage for the client to read, so tokens burned by an attempt that
failed in transport are invisible here. That spend is unknowable from this side
rather than merely unimplemented.

## Configuration

Optional config file at `~/.config/enmaas/config.toml`:

```toml
base_url = "https://api.enmaas.devshift.net"
default_model = "rits/zai-org/glm-5-3"
concurrency = 4
timeout = 280

[dialect_overrides]
"rits/zai-org/glm-5-3" = "anthropic"
```

All fields shown are the defaults except `dialect_overrides`.

The `dialect_overrides` table maps model names to dialect strings (`"anthropic"` or `"openai"`). An explicit `--dialect` flag on the command line takes precedence. Invalid dialect values are ignored with a warning.

### Rolling token budget

```toml
[budget]
window_days = 30
tokens = 50_000_000
```

With this set, `ask` and `map` refuse to start once the usage ledger shows more
than `tokens` metered tokens inside the last `window_days` days, exiting 3. A map
run that crosses the ceiling partway through stops there rather than waiting to be
caught next time. The window matches `enmaas usage --since DAYS`, so reporting and
enforcement count the same way.

Configuration rather than a flag on purpose: a standing policy you have to
remember to type is not a guard. For a one-off run against different settings, the
global `--config PATH` already exists.

**A malformed `[budget]` table is an error, not a warning.** Every other key in
this file degrades to its default; this one does not. A half-written budget is a
request for enforcement that cannot be honoured, and warning while running
uncapped would be the same false assurance as a flag that silently does nothing.
Both keys are required, a typo in a key name is rejected, and values must be
positive integers.

A config file that exists but cannot be parsed at all is also fatal, for the same
reason: falling back to defaults would drop a configured `[budget]` along with
everything else, so a stray typo anywhere in the file would run uncapped.

For the same reason the command refuses, rather than proceeding, when a budget is
configured and the ledger cannot be read, or when `ENMAAS_NO_USAGE_LOG` disables
recording. An unreadable ledger looks exactly like one showing zero spend. With no
budget configured the ledger is never consulted for enforcement at all, so a
damaged one does not stop work; `enmaas usage` keeps reporting what it can from
the rows it understands.

If a run finishes but its usage cannot be written back, and a budget is
configured, it prints a warning. That spend will not count against the ceiling, so
the ceiling has quietly stopped tracking.

**Single process only.** Each run reads the ledger at startup and writes its event
at the end. Two runs started together against 9,000,000 spent of a 10,000,000
ceiling will each believe 1,000,000 remains, and together spend 2,000,000. A run
killed partway writes no event, so its spend goes unrecorded. This is a guard
against your own drift over time, not an enforcement boundary against concurrent
use. Enforcing that properly needs a server-side budget, which a client cannot
provide.

## Development

```bash
make check
```

Runs ruff, mypy strict, and pytest (117 unit tests).

```bash
make live
```

Runs the live test suite against the real gateway. Uses only the free hosted models and costs nothing.
