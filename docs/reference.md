# enmaas reference

Every command, flag and configuration key. For installation read
[INSTALL.md](../INSTALL.md); for an overview read the [README](../README.md).

## Commands

### List models

```bash
enmaas models
```

Lists the merged catalog from both the anthropic and openai dialect endpoints. `--dialect {anthropic,openai,both}` narrows it; `both` is the default. `--json` prints it as JSON. `--timeout SECONDS` bounds each catalog request. A gateway that serves only one dialect rejects the other probe, so pass `--dialect openai` or `--dialect anthropic` there.

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

The full response is written to the file shown in the `output` line. The receipt includes a preview of at most 160 characters. Run directories under `$XDG_STATE_HOME/enmaas/runs/` (default `~/.local/state/enmaas/runs/`) are removed once they are 30 days old, at the start of the next `ask`. Only directories with a run name (timestamp plus four hex characters) are removed, and nothing is removed when the runs directory is a symbolic link. Pass `-o PATH` for an answer that must persist.

An answer is never written through a symbolic link, and only to a regular file. If the output path already exists as a link, `ask` fails and `map` marks that item failed with `refusing to write through a symbolic link`. A FIFO, a device or anything else that is not a regular file at the output path is refused too, without blocking: `ask` fails and `map` marks that item failed. The reply's charge is still recorded.

On an ordinary request failure the receipt goes to stderr and the exit code is 1, so a shell script can branch on it. With `--json` the error object goes to stdout. A refusal under a rolling `[budget]` exits 3.

### Map over multiple items

```bash
enmaas map --items items.jsonl --template template.txt --outdir out --concurrency 4
```

Output:

```
enmaas map ok
  model     rits/zai-org/glm-5-3  (default)
  batch     10  (default: template has ===PER ITEM===)
  items     12 total: 12 attempted, 12 ok, 0 failed, 0 skipped
  outdir    out
  index     out/results.jsonl
  tokens    in 1,204   out 96
  elapsed   6.2s
```

`(default)` marks a choice the CLI made for you. `index` is the per-item record
that `--resume` reads.

Each item in `items.jsonl` must have an `id` field. Ids must be unique, safe as filenames (no path separators or other filesystem-sensitive characters), and free of whitespace, because a batched reply names each answer by its id. The template can reference any other fields using `{field_name}` syntax. Output files are written to `outdir/<id>.md`.

When the template contains the `===PER ITEM===` marker, requests are batched. The text above the marker is sent once, exactly as written; only the text below it is filled in per item, so every `{field_name}` placeholder must sit below the marker.

The `--resume` flag skips items that both have an output file in the outdir and an `ok` row in `results.jsonl`, allowing you to continue an interrupted bulk run without re-processing completed items.

### Inspect an answer

```bash
enmaas inspect out/f001.md
enmaas inspect out/f001.md --max-chars 500
```

The default and maximum output is 2,000 characters, including the truncation
notice and final newline. It prints a prefix of the file. The command reads one
regular file and has no pagination or full-output option.

### Allow a deliberate full read

Run this in your own terminal to let an assistant read one protected output:

```bash
enmaas allow-read out/f001.md
```

The exception lasts until the next enmaas generation for that path. The output
guard denies assistant attempts to grant exceptions. The CLI creates exact file
markers; enforcement needs the guard registered with the host. See
[output protection](../skills/enmaas/references/output-protection.md) for
coverage and troubleshooting.

## Options

### Global flags

These go before the subcommand: `enmaas --key-file PATH models`.

- `--key-file PATH`: Read the gateway key from this file. Without it the CLI tries `$ENMAAS_KEY`, then the `key_file` set in the config file, then `~/.secrets/enmaas`. The key file must be a regular file of at most 4 KiB.
- `--config PATH`: Use this config file instead of `~/.config/enmaas/config.toml`.
- `--version`: Print `enmaas <version>` and exit 0.

### Common flags (`ask` and `map`)

- `-m, --model MODEL`: The model to use. Optional; without it the configured `default_model` applies and the receipt says `(default)`.
- `--schema PATH`: Constrain the answer to this JSON Schema. Validated locally on both dialects. See [Structured output](../skills/enmaas/SKILL.md#structured-output).
- `--schema-name NAME`: The name sent with the schema. Defaults to `StructuredOutput`. `ask` and unbatched `map` send the schema and its name with the request; batched `map` keeps the schema local and validates each answer after the reply is split.
- `--json`: Emit machine-readable JSON instead of plain-text receipts. For `ask`, prints an object with `status`, `model`, `dialect`, `path`, `tokens`, `elapsed`, and `error`. For `map`, prints an object with `status`, `model`, `batch`, `defaulted`, `abort_reason`, `total`, `ok`, `failed`, `internal_errors`, `skipped`, `attempted`, `outdir`, `index`, `tokens`, `elapsed`, `budget`, `failures`, and `error`. `internal_errors` counts the failed items that hit an error inside enmaas; they are already in `failed`, and are worth reporting. `abort_reason` is `breaker`, `budget`, `interrupted` or `index` when the run stopped early, and `error` holds the write error when it is `index`; both are null otherwise.
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
  The output guard denies assistant use of this option.
- `--save-thinking`: Save reasoning tokens to a separate file (when available).
- `--retries N`: Number of retries on transient failures. Defaults to 3.

If no prompt is given via `-p` or `--prompt-file`, stdin is read.

### Map-specific flags

- `--items PATH`: JSONL file with items to process. Each item must have an `id` field.
- `--template PATH`: Python `str.format` template file for generating prompts.
- `--outdir PATH`: Output directory for response files.
- `--concurrency N`: Number of concurrent requests. Must be positive. Defaults to `concurrency` from the config, whose built-in default is 4.
- `--retries N`: Number of retries per item. Defaults to 3.
- `--resume`: Skip items that already have an output file and an `ok` row in `results.jsonl`.
- `--batch N`: Items per request. Defaults to 10 when the template contains the `===PER ITEM===` marker and 1 otherwise. Above 1 requires the marker. The shared part of the template is sent once per group.
- `--token-budget N`: Stop the run once it has spent N metered tokens. Different from `--max-tokens`, which caps the output of one request. See [Token budget](#token-budget).

In a batched `map` run, `--max-tokens` is multiplied by the number of items in the group, so `--max-tokens 100` with ten items sends a ceiling of 1,000 for that request.

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

## Unencodable model output

Lone surrogates (U+D800 to U+DFFF) are the only codepoints UTF-8 cannot encode, and
a model can emit one: it travels as a plain ASCII `\ud800` escape in the JSON body,
so it survives transport intact and only fails at the point of writing the response
to disk.

Such characters are replaced with U+FFFD at parse time, before anything tries
to write them. The replacement is never silent: `ask` prints a warning line and
reports `replaced` under `--json`, and `map` records a count on that item's row in
`results.jsonl`.

A repaired item still counts as a success and its output is kept, but a repaired
row on its own does **not** qualify the item for `--resume` skipping. If the same
item also has an earlier clean `ok` row in `results.jsonl`, that row still makes
it skippable. The saved text differs from what the model sent, so treating
it as finished would mean a rerun could never obtain a clean version. Retrying it
costs a request, which `--token-budget` can bound.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (includes usage errors like invalid flags, and a `map` whose `results.jsonl` could not be written) |
| 2 | Partial failure (some items failed in a map run) |
| 3 | Run stopped early: circuit breaker tripped, or token budget spent |
| 130 | Stopped by SIGINT (Ctrl-C), as a shell reports it |
| 143 | Stopped by SIGTERM, as a shell reports it |

Usage errors (invalid flags, missing required arguments) exit 1, so exit code 2 unambiguously means partial failure.

An interrupted `ask` or `map` ends by the signal that stopped it, after it has
printed its receipt or error, so the caller sees how it stopped. A shell shows
128 plus the signal number; a program that starts enmaas sees the signal itself.
See [Runs that stop early](#runs-that-stop-early).

On exit code 3 from `map`, re-run with `--resume` to skip completed items. If the stop was a per-run `--token-budget`, raise it on the resumed command. If it was the rolling `[budget]` in the config, which can also stop `ask`, raise the ceiling in the config or wait for the window to move; `--resume` alone will stop again immediately.

## Circuit breaker

The circuit breaker aborts a bulk map run on:
- 6 consecutive failures, or
- More than 50% failures in a full 20-item window

Whichever comes first. In a batched run the counts are updated after each
group is recorded, so a streak of failures inside one group is judged together
with that group's successes. This catches a run that is failing. It does nothing
for a run where every call succeeds and the spend is the problem; that is what
the token budget below is for.

## Token budget

`--token-budget N` stops a map run once it has spent N metered tokens, exiting 3
the way a tripped circuit breaker does.

**Metered means uncached input plus output.** Cached input is excluded, so
batching helps a run go further under the same ceiling. Cache writes are charged,
because writing a cache entry is real work. Reasoning is charged too, but it needs
no separate term: it is already inside the output count on both dialects.

**It stops scheduling new requests and lets in-flight ones finish.** At `--concurrency N`, up to
N-1 further requests are already in flight and will finish. With `--batch`, those
are N-1 further groups, which can be a lot of tokens. There is no fixed token
bound without bounding request size.

**The ceiling belongs to the whole job across resumes.** Every request appends a
`{"type": "request", "charge": N, "items": K, "run": "<run id>"}` row to
`results.jsonl`, and a resumed run adds up every charge row, whichever run wrote
it, before spending anything. So `--resume` on its own re-trips immediately.
The `run` field is the id of the run's open-run record (see
[Runs that stop early](#runs-that-stop-early)). It is absent when no record was
written, for example with `ENMAAS_NO_USAGE_LOG` set. A charge is written capped
at 10^18 tokens, and a row with a larger charge is not counted. To carry on,
raise the ceiling deliberately:

```bash
enmaas map --items items.jsonl --template template.txt --outdir out --token-budget 2000000 --resume
```

That is the intended friction. A budget that reset on every invocation could be
bypassed by pressing up-arrow and Enter.

**What it does not catch.** `call` retries internally, and a non-200 response
carries no usage for the client to read, so tokens burned by an attempt that
failed in transport are invisible here. That spend cannot be known from this side.

## Configuration

Config file at `~/.config/enmaas/config.toml`. `base_url` is required; everything else is optional. `install.sh --gateway URL` writes `base_url` for you.

```toml
base_url = "https://GATEWAY-HOST"   # required; your organization gives you this
key_file = "/home/you/.secrets/enmaas"   # optional; only when the key is not at ~/.secrets/enmaas
default_model = "rits/zai-org/glm-5-3"
concurrency = 4
timeout = 280

[dialect_overrides]
"rits/zai-org/glm-5-3" = "anthropic"
```

`base_url` has no default: it must be set here or in `ENMAAS_BASE_URL`, which wins when both are set. It must use `https://`; `http://` is accepted only for `localhost`, `127.0.0.0/8` and `::1`. `models`, `ask` and `map` exit 1 with a message naming the fix when neither is set; the local commands do not need it. `key_file` may name the key file when it is somewhere other than `~/.secrets/enmaas`. The other fields shown are the defaults except `dialect_overrides`.

The `dialect_overrides` table maps model names to dialect strings (`"anthropic"` or `"openai"`). An explicit `--dialect` flag on the command line takes precedence. Invalid dialect values are ignored with a warning.

### Prices

```toml
[prices."gpt-6-sol"]
input = 1.25
output = 10.0

[prices."gemini-"]
input = 2.0
output = 8.0
```

Rates are USD per million tokens. A key is an exact model id or a prefix covering
a family; an exact id wins. Both `input` and `output` are required and must be
non-negative numbers. A malformed entry is reported as a warning and skipped, so a
model never looks free by accident. `rits/*` models are known to be free without
an entry, and that built-in rate also beats a prefix entry: to price a `rits/*`
model, name its exact id. The gateway publishes no prices, so these can only come
from whoever knows what the account is charged.

### Rolling token budget

```toml
[budget]
window_days = 30
tokens = 50_000_000
```

With this set, `ask` and `map` refuse to start once the usage ledger shows at
least `tokens` metered tokens inside the last `window_days` days, exiting 3. A map
run that crosses the ceiling partway through stops there rather than waiting to be
caught next time. The window matches `enmaas usage --since DAYS`. Enforcement adds up the
`charge` recorded per request, which covers every request the gateway metered,
including one that returned no text and one whose answer later failed
validation. The `usage` token totals come from the token counters, which skip an
unbatched `map` item whose answer failed validation, so the two can differ by
that work.

This lives in configuration on purpose: a standing policy you have to
remember to type on every command is no guard at all. For a one-off run against different settings, the
global `--config PATH` already exists.

**A malformed `[budget]` table is an error.** Every other key in
this file degrades to its default; this one does not. A half-written budget is a
request for enforcement that cannot be honoured, and warning while running
uncapped would be the same false assurance as a flag that silently does nothing.
Both keys are required, a typo in a key name is rejected, and values must be
positive integers.

A config file that exists but cannot be parsed at all is also fatal, for the same
reason: falling back to defaults would drop a configured `[budget]` along with
everything else, so a stray typo anywhere in the file would run uncapped.

For the same reason the command refuses to start when a budget is configured and
the ledger cannot be read, when the open-run records cannot all be read, or when
`ENMAAS_NO_USAGE_LOG` disables recording. A refusal over the ledger names it and
lists at most five problems, each as a line number and a fixed reason such as
`bad count` or `not JSON`, then how many more there are. It never shows a value
from the ledger. A refusal over the open-run records names `open-runs.jsonl`:
run the command again, or delete that file if you did not create it. A
write failure after the run has finished is different: the tokens are already
spent, so the command prints a warning and that spend goes uncounted. An unreadable ledger looks exactly like one showing zero spend. With no
budget configured the ledger is never consulted for enforcement at all, so a
damaged one does not stop work; `enmaas usage` keeps reporting what it can from
the rows it understands.

If a run finishes but its usage cannot be written back, and a budget is
configured, it prints a warning. That spend will not count against the ceiling, so
the ceiling has quietly stopped tracking.

**Not an enforcement boundary for runs side by side.** Each run reads the ledger
at startup and writes its event at the end. The check also adds what every `map`
run with an open-run record still present has charged so far, read from its
index (see [Runs that stop early](#runs-that-stop-early)). That covers runs in
progress and dead runs the command could not settle, for example because another
process held the record lock. The records are read before the
ledger, so a run that ends between the two reads is counted twice for that one
check, never zero times. When open runs add to the spend, the refusal says how
much and names `open-runs.jsonl`, so you can find a record that should not be
there. That narrows the gap but does not close it. Two runs started together against 9,000,000 spent of a 10,000,000
ceiling each see the other at zero, each believe 1,000,000 remains, and together
can spend 2,000,000. This is a guard against your own drift over time.
Enforcing a ceiling across concurrent use needs a server-side budget, which a
client cannot provide.

## Usage ledger

Every `ask` and `map` call appends one line to
`~/.local/state/enmaas/runs/usage.jsonl` (or `$XDG_STATE_HOME/enmaas/runs/usage.jsonl`).
The ledger holds metadata only: model, dialect, token counts, elapsed time and
status. `ask` and `map` never write prompts, responses, file paths or error
messages to it. `enmaas record` stores the `--command` and `--error-kind` values
you give it as they are, so keep those to tool and class names and the ledger
stays safe to share with a colleague.

```bash
enmaas usage                # all time
enmaas usage --since 7      # last 7 days
enmaas usage --json         # machine-readable
```

The summary shows totals and breakdowns by model and by command. Models with a
`[prices.*]` entry in the config are priced; `rits/*` models are known to be free;
anything else is reported as unpriced.

Every row is checked field by field. A count or charge must be an integer from 0
to 10^18, the elapsed time a finite number of zero or more, and the model,
command, dialect, status and error kind text of at most 1,024 characters.
`enmaas` writes its own rows inside those limits: counts and charges are capped at 10^18
and text is cut to 1,024 characters. A row that fails a check is treated as
damaged: `enmaas usage` skips it and warns how many rows it skipped (with `--json`
the count is also in `skipped_rows`), and a budget check refuses until it is
fixed. Model and command names from the ledger are printed on one line, with
characters that cannot be printed replaced.

Set `ENMAAS_NO_USAGE_LOG=1` to turn recording off. A ledger that cannot be
written never stops your command. With a `[budget]` configured you also get a
warning that the run will not count against the ceiling, and the command refuses
to start when the ledger cannot be read or recording is disabled, because it
cannot know what has been spent.

### Runs that stop early

A `map` run writes its ledger event when it ends. So that a run which never
reaches its end is still counted, it also keeps an open-run record.

**The open-run record.** When `map` starts, it adds one line to
`open-runs.jsonl` in the same directory as `usage.jsonl`. The line holds a random
run id, the absolute path of the output directory, the size in bytes of its
`results.jsonl` at the start, the start time, the process id, the model and the
dialect. It holds no prompt, answer or key. When `ask` or `map` creates that
directory, it gets mode 0700; a directory that already exists keeps its mode. The
file is created with mode 0600, and every change to it
holds a lock on `open-runs.lock` beside it; the lock file stays there. The line
is removed after the run's ledger event is written, and the file is removed when
no line is left. When the ledger event could not be written, the line stays, and
a later command charges the run once the ledger can be written. The record is
kept apart from the ledger because it must be removed once the run ends, and the
ledger only grows. `ENMAAS_NO_USAGE_LOG` turns the records off with the ledger.
If the record cannot be written, `map` warns that a kill would leave its spend
uncounted, and runs anyway.

**Every record is checked before it is used.** The record file and the lock file
must belong to you. A line is skipped when a field has the wrong type, when the
model is not 1 to 1,024 printable characters, or when the start time is more
than five minutes in the future. A file larger than 1 MiB or holding more than
256 records is ignored as a whole, with a warning that names it; delete it if you
did not create it. A new `map` that cannot add its record to such a file warns
with the file's name and the reason. With a `[budget]` configured, a record file
that cannot be read makes the budget check refuse. A record that cannot be
handled is dropped with a warning that names only the kind of error, and its
spend is not counted. The lock is held only to load and rewrite the record file,
never while an index is read, and is waited for at most five seconds. After that
the command goes on without it: no record is settled, a new `map` warns that its
record could not be written, and a run that is ending leaves its record for the
next command. The budget check still counts every record it finds. Nothing read
from a record or an index is printed; only the model and dialect of a record
reach the ledger.

**Killed.** A run that is killed (for example by SIGKILL, a crash or a lost
machine) leaves its record behind. So does a run that stops on an error before
it writes its event. The next `ask` or `map` checks each record. When its process
is gone, or the record is more than 48 hours old, it adds up the charge rows in
`results.jsonl` that carry that run's id in their `run` field and come after its
starting size, writes one ledger event with `error_kind` `Killed` and that
charge, and removes the record. Rows of other runs on the same directory, such
as the run a `--resume` continues or a run started beside it, are not counted.
The token counts of that event are empty, because only the charge rows survive.
The event carries the time it was settled, not the time of the run, so its
charge falls in the rolling window as of that later command. The age limit
exists because a process id can be reused, which would keep a record alive
forever.

The indexes are read as a stream, at most 64 MiB in each pass for all records
together. A command makes two passes, one to settle dead runs and one for the
budget check, so it reads at most 128 MiB. Records are read smallest index
first. Only lines that start with `{"type": "request"` and are at most 64 KiB
long are parsed; any other line is skipped. When a record's index does not fit
in what is left of the 64 MiB, settling it waits for the next command, and with
a `[budget]` configured the budget check refuses for that command. A record that
has the whole 64 MiB and still does not reach the end is settled, and counted by
the budget check, with what was read. Settle also removes temporary record files left in the state
directory for more than an hour.

**Interrupted.** The first SIGINT (Ctrl-C) or SIGTERM stops a `map` run. Rows
already written are complete, and the run writes its ledger event with
`error_kind` `Interrupted` and the charge of those rows. It removes its record
and prints its receipt, headed `enmaas map interrupted`, or with `--json` an
object with `status` `aborted` and `abort_reason` `interrupted`. Then it ends by
the same signal: 130 or 143 in a shell. Re-run with `--resume` to finish.

A second SIGINT or SIGTERM ends the process at once, even while it waits on a
file or a lock. A `map` record that is still there is then charged by the next
command from its rows, as `Killed`. A signal that is ignored when enmaas starts,
as in a background job started with SIGINT ignored, stays ignored.

An `ask` stopped while its request is in flight records nothing: no reply had
arrived, so there is no usage to record. It prints `error: interrupted`, or with
`--json` an object with `status` `error` and `error` `interrupted`, and ends by
the signal. A reply that had arrived is recorded: the ledger event is written
before the result is printed, and from then on the signal changes nothing.

A signal that arrives after a command has written its ledger event and started
to print its result changes nothing, including the exit status. The output and
the ledger stay as they are, and the command exits with its own code. The
installed `enmaas` command ignores SIGINT and SIGTERM from the moment the command
ends until the process exits, so a late signal cannot end it by its default
action or print a traceback.

**Index write failures.** `map` refuses to start when `results.jsonl` is a link,
is not a regular file, or belongs to another user. When a write to it fails
during the run, for example because a FIFO was put in its place, the run sends
no further requests and ends in order. It exits 1, writes its ledger event with
`error_kind` `IndexWriteError` and every charge that was billed, removes its
record, and prints a receipt headed `enmaas map stopped: the index could not be
written` with an `error` line, or with `--json` an object with `status`
`aborted`, `abort_reason` `index` and the error in `error`.

**When spend is counted twice.** Each of these counts a run twice, once:

- A stop that lands between the ledger write and the record removal, either in
  the run itself or while a later command settles the record.
- A run that ends while another process holds the record lock for more than
  five seconds. Its record stays after its own event, and the next command
  settles it as `Killed`.
- A run still alive after 48 hours. The next command settles it as `Killed`, and
  the run writes its own event when it ends.
- A run in another container or on another host that shares the state
  directory. Its process cannot be seen from here, so it looks dead and is
  settled while it runs.

Counting too much is the safe side for a budget. The other order of the ledger
write and the record removal could lose the spend instead.

**When spend is undercounted.**

- A killed run whose `results.jsonl` grew by more than 64 MiB after it started.
  The rest is not read.
- An output directory that was deleted or replaced, or whose `results.jsonl` was,
  before the next command. Its rows are gone.
- A request in flight when the run stopped. No reply arrived, so its cost is not
  known, though the gateway may still bill it.
- A dead run whose `results.jsonl` cannot be opened, for example because of its
  permissions. Its record is kept and each command tries again, and with a
  `[budget]` configured the budget check refuses meanwhile. Once the record is
  48 hours old it is dropped with a warning, and that spend is not counted.

### Counting work this CLI did not do

The ledger only sees calls made through this CLI. If another tool talks to the
gateway directly, record the run when it finishes so `usage` stays honest:

```bash
enmaas record --model rits/zai-org/glm-5-3 --command bug-hunt \
  --input 1067793 --output 49242 --elapsed 612
```

`--command` names whatever did the work. `record` refuses a negative count, a
count above 10^12, and an `--elapsed` that is negative or not a finite number.
The ledger caps what it stores at 10^18 per count and cuts text to 1,024
characters. Add `--cached` and `--reasoning` when
you have them, `--dialect anthropic` when the call used that API shape (the
default is `openai`), and `--status error --error-kind GatewayError` for a run
that failed.

## Output protection

`ask` and `map` mark every answer file they are about to write, before the model
request, by writing a record under a hidden `.enmaas-output` directory next to
it. The guard installed by `install.sh` reads those records and denies an
assistant's ordinary attempts to read the file: the hosts' file tools and the
common shell readers. It answers inside a time budget of three seconds and denies
a search it cannot finish checking in that time, so a slow check never turns into
a silent allow. On Claude Code it is registered with `onFailure: "block"`, so a
hook that fails or is killed blocks the call. The guard is for accidental reads;
an assistant that writes its own program to open the file is outside its scope. `enmaas inspect PATH` is the sanctioned
preview and `enmaas allow-read PATH`, run by a person, is the sanctioned
exception. Full coverage rules, resume behaviour and troubleshooting are in
[output protection](../skills/enmaas/references/output-protection.md).

`map` never writes `results.jsonl` through a symbolic link. When that path in
the output directory is a link or anything other than a regular file, `map`
stops with an error before it sends any request.
