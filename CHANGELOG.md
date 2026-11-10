# Changelog

All notable changes to enmaas. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- The output guard now runs as `python3 -I -S`, in Python's isolated mode.
  Before, when the environment a host passed to the guard set `PYTHONPATH` to
  a directory that holds files from a repository, such as `.`, a `json.py` in
  that directory ran as your user on every guarded tool call, and the guard
  made no decision. The plugin registration, the registration `install.sh`
  writes and the installer's own guard check all use the new form. Run
  `./install.sh` again to replace a registration made by an earlier version.
  Codex then asks you to trust the changed hook.
- A gateway on `localhost` or a loopback address is now always reached
  directly. Before, when `HTTP_PROXY`, `HTTPS_PROXY` or `ALL_PROXY` was set,
  in either case, a plain `http://localhost` gateway request went to that
  proxy with the key in its header, so whoever ran the proxy could read the
  key. TLS settings such as `SSL_CERT_FILE` still apply, and any other gateway
  still uses the proxy variables.
- `install.sh --dry-run` no longer prints the whole Claude Code
  `settings.json`, Codex `config.toml` or enmaas `config.toml` it would write.
  Before, a dry run that would change one of those files printed every line of
  it, so tokens in its `env` block or in a provider header showed in the
  output, and in any log it was pasted into. It now prints each file's path and
  what it would add, replace, remove or set. An error about a file that does
  not parse names the place it stops parsing and no longer quotes a key from
  it.
- The output guard now reads Bash `$'...'` and `$"..."` quoting the way Bash
  does. Before, a shell read such as `cat $'answer.md'` or
  `cat $'\141nswer.md'` was checked under a different name than the file Bash
  opened, so an assistant could read a protected answer in full through either
  host. A reader command with a `$'...'` string the guard cannot decode, such as
  one with an unknown escape or a NUL, is now denied.
- The output guard now checks a file that a reader loads as input before it
  allows a search that prints only file names or counts. Before,
  `rg -l -f answer.md other.txt` was allowed, and ripgrep printed the text of
  the protected answer in its error when that text was not a valid pattern.
  The check covers pattern files (`grep` and `rg -f`), ignore and exclude
  files, `sed` and `awk` script files, `jq --rawfile` and `--slurpfile`,
  `diff --from-file` and `--to-file`, `sort --files0-from`, `less` key and tag
  files, and `strings @file`.
- A `--schema` file can no longer make enmaas fetch anything. Before, a
  `$ref` to an `http://` or `file://` address in the schema was retrieved
  while an answer was checked, so a schema from another party could make
  enmaas contact any host, after the paid request was already sent. Now a
  `$ref`, `$dynamicRef` or `$recursiveRef` that does not start with `#` stops
  the command before any request, and validation has nothing it could
  retrieve.
- The usage ledger, the open-run records and each `map` index are now created
  with mode 0600, and the state directory with mode 0700 by every command that
  creates it, including `enmaas record`. Before, under the usual umask 022, the
  ledger and the index were readable by every user on the machine, and so was
  the state directory when `enmaas record` created it. A file that already
  exists keeps its mode. The ledger, like the index, is now opened without
  following a symbolic link and without blocking, and only a regular file is
  read or written. Before, a link at `usage.jsonl` sent the ledger rows to
  the link's target, and a FIFO there hung every `ask`, `map` and `usage` with
  no message. Now a budgeted command refuses and names the file, `enmaas usage`
  warns and exits 0, and an unbudgeted `ask` or `map` keeps its answer.
- A malformed gateway reply, a stop signal or a crash no longer loses the
  spend of the requests a run already made. Before, a reply such as
  `{"choices": [1]}` ended a `map` run with a traceback before its ledger
  event was written, so with a rolling `[budget]` the next run started as if
  that spend never happened and could pass the ceiling. The charge of every
  reply that arrived now reaches the ledger, through the run's own event or,
  for a run that was killed, through its open-run record. The reference lists
  the cases that can still be undercounted.
- With a rolling `[budget]` configured, `ask` and `map` now refuse to start
  when the usage ledger cannot be written, and send no request. Before, a
  missing ledger in a state directory that could not be written read as no
  spend, so every run spent, failed to record it, and let the next run start
  from zero: the budget never bound. The command now exits 1 with
  `the usage ledger <path> cannot be written, so the token budget cannot be
  tracked; fix it or remove the [budget] table from the config`, or with
  `--json` the error object. The same holds for a read-only ledger, a full
  disk and a ledger at the process file size limit: before it sends, the
  command writes 64 KiB to a hidden file in the state directory and removes
  it, and checks the ledger can grow under the size limit. Room can still run
  out while a request is in flight, and then the command warns.
- An API key that holds a character an HTTP header cannot carry, such as a
  newline, a carriage return, a tab or a non-ASCII letter, is now refused at
  the start, before any request, with an error that names where the key came
  from and shows no part of it. Before, a key pasted with a second line
  failed inside the request, and the transport error printed both lines of
  it with the newline escaped, where redaction did not find it. The first
  line can be a working key.
- Text from a gateway reply or a file can no longer act on the terminal.
  Before, a reply that held an escape sequence such as OSC 52, which asks
  the terminal to set the clipboard, reached the terminal through the receipt
  preview, and so could a model id in the `models` list, a file name in a
  receipt path, and the text `enmaas inspect` printed. Gateway error lines
  could also add a forged line to a receipt. Now each of these prints every
  character that cannot be printed as `?`, a newline or a tab in an error or
  a preview as a space, and `inspect` keeps newlines and tabs. Answer files,
  `ask --json` and `results.jsonl` keep the text as sent, and the `map --json`
  failure list keeps it as sent up to 200 characters.
- The skill's commands that count and list failures from `results.jsonl` now
  run Python in isolated mode, `python3 -I -c`. Before, Python put the current
  directory first on its import path, so running them inside a repository
  that held a `json.py` ran that file as your user. The listing also replaces
  every character that cannot be printed with `?` and cuts each id to 100
  characters and each error to 200, so a planted row cannot flood or drive
  the terminal.

### Fixed

- A gateway reply with status 200 whose body is not a JSON object, such as an
  HTML page from a proxy or a JSON list, now fails that request with
  `BadReplyError` in place of a traceback. It is not retried, because the
  gateway may already have billed it. The error names the content type and the
  body size and never quotes the body.
- The content type named in that error now has the API key removed before it
  is cut to 80 characters, so no part of a key survives the cut. Characters
  that cannot be printed on one line are replaced.
- A reply that reports a token count above 10**12 no longer ends the run. Such
  a count reads as not reported, the same as a negative one.
- A reply nested too deeply to decode now fails as `BadReplyError`, and a
  response nested too deeply for `--schema` fails the schema check. A `map`
  item no longer reports either as an internal error.
- An `ask` that fails after the reply arrived, for example because the answer
  file cannot be written, now exits 1 with a failure receipt and records the
  charge in the usage ledger. An unexpected error there is reported as
  `internal error (<class> at <file>:<line>)` and never shows its text.
- A `map` run whose `results.jsonl` cannot be written now sends no further
  requests and ends in order: it exits 1, writes a ledger event with
  `error_kind` `IndexWriteError` that holds every charge that was billed, and
  prints a receipt headed `enmaas map stopped: the index could not be written`,
  or with `--json` an object with `abort_reason` `index` and the error in
  `error`. The index must be a regular file you own, and it is opened without
  blocking, so a FIFO put in its place cannot stall the run.
- An answer is written only to a regular file. A FIFO or a device at the answer
  path fails that `ask` or `map` item without blocking, and the reply's charge
  is still recorded.
- An API key echoed back JSON-escaped in a gateway error body, as happens for a
  key holding a quote or a backslash, is now removed from the error message.
- A client error during a request, such as a reply that cannot be decoded, now
  fails that request at once with `GatewayError` in place of a traceback. It is
  not retried, for the same reason.
- A reply field of an unexpected type, such as a `choices` value that is not a
  list of objects, now fails the request with `BadReplyError` in place of a
  traceback. A missing or null field still reads as an empty answer. The error
  keeps the token counts the reply reported, so `ask` and `map` charge them.
- An OpenAI reply whose `content` is a list of parts is now read: its text
  parts are joined in order. An empty list reads as an empty answer, the same
  as a missing `content`. A non-empty list with no text part fails as
  `BadReplyError`.
- One bad reply in a `map` run now fails only the items it covered. The other
  items still run.
- An error inside enmaas that no handler expected now fails only the `map` item
  or group it hit, with a message such as
  `internal error (TypeError at runner.py:123)`, in place of stopping the run.
  The message never holds the error text, which could quote a reply.
- The usage ledger now records the class of the error that failed an `ask`. It
  used to guess from the message, so a gateway error that mentioned
  "validation" was filed as `ValidationError` and a schema failure as `Error`.
- A `map` run that is killed no longer loses its spend. Each run keeps an
  open-run record in `open-runs.jsonl` beside the ledger, and the next `ask` or
  `map` charges a dead run's rows to the ledger as `Killed`. Only the charge
  rows that run wrote itself count, so a resumed directory or a second run on
  the same directory is not charged twice. A run whose ledger event could not
  be written keeps its record, so a later command charges it. Every record is
  checked before it is used: the files must belong to you, the start time
  cannot be in the future, and a record that cannot be handled is dropped
  with a warning in place of stopping the command. The lock on the records is
  waited for at most five seconds and is never held while an index is read,
  and a record another command settled in the meantime is not charged again.
  A dead run whose index cannot be opened keeps its record until it is 48
  hours old, then is dropped with a warning. A `Killed` event is written only
  when the record file can also be rewritten, so a damaged state directory
  cannot charge the same run on every command. Temporary record files left
  for more than an hour are removed.
- SIGINT and SIGTERM no longer end a `map` run before it records anything. The
  run writes its ledger event as `Interrupted`, prints its receipt, and then
  ends by the same signal. An `ask` interrupted before its reply arrived prints
  `error: interrupted` in place of a traceback and records nothing; a reply
  that had arrived is recorded. A signal that is ignored when enmaas starts
  stays ignored. The installed `enmaas` command ignores both signals from the
  moment the command ends, so a late signal cannot end a finished run or print
  a traceback. Calling `main` in-process leaves the signal handlers as they
  were, with the default put back for a handler not set from Python.
- A reader that closes stdout before the receipt is printed no longer makes a
  finished `ask` or `map` exit 120 with a `BrokenPipeError`.
- With `--json`, `ask` and `map` now print exactly one JSON object on stdout
  on every exit, and `models` prints the error object when it fails. A
  missing items file, a bad template, attachment,
  prompt or schema, an unknown model dialect, a refused index and a malformed
  `[budget]` table used to print text on stderr only. The error object is
  `{"status": "error", "error": ...}`; its text has the key removed, is printed
  on one line, and is cut to 200 characters.
- An error inside enmaas that no handler expected no longer prints a
  traceback. It is reported as `internal error (<class> at <file>:<line>)`,
  never with its text. `ENMAAS_DEBUG=1` adds the traceback on stderr, with the
  key removed.
- A model catalog that is not a JSON object with a `data` list of objects with
  a string `id`, such as an HTML page or a JSON list, now makes `models` exit 1
  with one `error:` line naming the problem in place of a traceback. The error
  never quotes the body.

### Changed

- `map --json` has a new `internal_errors` field, and the `map` receipt shows an
  `internal` line when it is above zero. Those items are already counted in
  `failed`. Please report them.
- The `map` receipt and `map --json` list at most the first 20 failures, each
  message cut to 200 characters without splitting the redaction placeholder.
  The receipt then says how many more the index holds, and `map --json` has a
  new `failures_truncated` field. `results.jsonl` keeps every message whole,
  and the skill gives commands that count and list failures from it.
- The reference documents every field of the `ask`, `map`, refusal and error
  JSON objects and of the `models` list.
- The usage ledger can record `BadReplyError` as the `error_kind` of an `ask`.
- The rolling budget check now adds what every `map` run with an open-run
  record still present has charged so far, whether it is still running or
  died and could not be settled. It reads them before the ledger, so a run
  that ends during the check is counted, and its refusal says how much came
  from open runs and names `open-runs.jsonl`. When the records cannot all be
  read, the check refuses and names that file.
- A budget refusal over a damaged ledger names the ledger and lists at most
  five problems, each as a line number and a fixed reason. It never shows a
  value or a key from the ledger.
- A second SIGINT or SIGTERM ends the process at once, even while it waits on
  a file or a lock. The next command charges a `map` run stopped this way from
  its open-run record. A signal that arrives after a command has printed its
  result changes neither the ledger, the output nor the exit status.
- Each charge row in `results.jsonl` now names the run that wrote it in a `run`
  field, when the run has an open-run record.
- `map` refuses to start when `results.jsonl` in the output directory is a
  symbolic link, not a regular file, or owned by another user, and it never
  writes the index through a link.
- Reading open runs is bounded: each pass, one to settle dead runs and one for
  the budget check, reads at most 64 MiB of index for all records together,
  smallest index first, so one command reads at most 128 MiB. A line counts
  only when it is a whole charge row as `map` writes it for that record's run,
  and no line is parsed as JSON, so an index planted with lines that look like
  charge rows no longer costs tens of seconds on every budgeted command. A
  killed run whose index grew by more than 64 MiB is undercounted. An `open-runs.jsonl` over
  1 MiB or with more than 256 records is ignored with a warning that names it.
- `enmaas record` refuses a count above 10**12 and an `--elapsed` that is not
  a finite number. Every count and charge written to the index, the ledger or
  an open-run sum is capped at 10**18, and the readers accept values up to
  that, so a row enmaas writes always reads back. A larger value is skipped in
  a charge row and treated as a damaged row in the usage ledger.
- Ledger rows are checked field by field: text fields hold at most 1,024
  characters and the elapsed time must be a finite number of zero or more.
  `enmaas usage` skips a row that fails a check and warns how many it skipped,
  and `usage --json` reports the count in `skipped_rows`.
- `enmaas usage` prints model and command names from the ledger on one line,
  with characters that cannot be printed replaced. An open-run record is used
  only when its model is 1 to 1,024 printable characters.
- `map` has new `abort_reason` values, `interrupted` and `index`, with the
  receipt headers `enmaas map interrupted` and `enmaas map stopped: the index
  could not be written`. `map --json` has a new `error` field for the index
  error. The ledger can record `Killed`, `Interrupted` and `IndexWriteError`
  as the `error_kind` of a `map`.
- The `enmaas` console script now starts through `enmaas_cli.cli:entry`.

## [0.7.1] - 2026-11-09

### Added

- `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`, so a plugin
  marketplace can list this repository directly with a `github` source. The
  skill, `hooks/hooks.json` and the guard's files already sat where a plugin
  expects them, and `hooks.json` already resolved the guard through
  `CLAUDE_PLUGIN_ROOT`, so the manifests were the only missing piece.
- A test holding the package version and both manifest versions together, and
  pinning the manifest name a marketplace entry installs by.

### Changed

- The skill's setup reference and the README split the two install routes. A
  marketplace supplies the skill and the guard, so installing from a plugin
  means `install.sh --cli-only`. The full installer on top of a plugin
  registers a second guard and copies a second skill.
- `INSTALL.md` no longer tells readers to request access to a repository that
  is public. `CONTRIBUTING.md` describes the ruleset that now protects `main`
  and release tags, in place of the note saying protection was unavailable.

## [0.7.0] - 2026-11-08

### Changed

- The output guard answers inside a three second budget and denies a search it
  cannot finish checking, so a slow check can no longer time out into an allow.
  `install.sh` registers the Claude Code hook with `onFailure: "block"`.
- The guard checks the file commands inside `sed` (`r`, `R`, `w`, `W`) and `awk`
  (`getline <`) programs.
- `http://` gateway URLs are accepted only for localhost; everything else must be
  `https://`. The installer applies the same rule.
- Answers are never written through a symbolic link; an output path that already
  exists as a link fails that item.
- Run-directory cleanup removes only run-named directories and skips a symlinked
  runs directory.
- Schema-file and gateway-usage diagnostics are bounded; usage counts that are
  not non-negative integers are recorded as unknown.
- The key file must be a regular file of at most 4 KiB. `Retry-After` is capped at
  60 seconds. Error bodies are redacted before they are truncated.

## [0.6.0] - 2026-11-07

### Changed

- The gateway URL is a required setting. Set `base_url` in the config file or
  `ENMAAS_BASE_URL`; `install.sh --gateway URL` writes it for you and asks once if
  it is missing. Commands that call the gateway exit 1 with a message naming the
  fix when no URL is configured.
- `install.sh --key-file PATH` names the key file. A path other than the default is
  recorded as `key_file` in the config and the CLI reads it after `ENMAAS_KEY`.
- Live tests read the gateway from `ENMAAS_BASE_URL` and skip with a reason when it
  is unset.

## [0.5.0] - 2026-11-07

First release from this repository. Earlier versions shipped inside an internal
plugin marketplace.

### Added

- `install.sh` installs the CLI, copies the agent skill for Claude Code and Codex,
  installs the output guard and registers it as a `PreToolUse` hook in both hosts.
  `--uninstall` reverses all of it. `--dry-run`, `--no-hook`, `--cli-only` and
  `--skill-only` narrow it. Host files are validated before any edit, edits keep
  everything they did not write, and a second run reinstalls the selected parts
  without duplicating a registration or touching other settings.
- `enmaas --version`.
- `scripts/test-install.sh` (`make check-install`) proves the installer in a
  throwaway home, including idempotency, dry run and uninstall.
- `scripts/check-host-hooks.py` drives the real `claude` and `codex` binaries to
  check the guard on 57 tool-call cases, and can be pointed at the files the
  installer wrote.
- Install guide, command reference, security policy, issue templates, Apache 2.0
  license.

### Changed

- CI runs on Python 3.11 through 3.14 and runs the installer end to end.
- The guard and its tests work from this repository's layout and no longer depend
  on how the package was installed.

## [0.4.0] - 2026-11-06

### Added

- `map` batches ten items per request by default when the template carries the
  `===PER ITEM===` marker. `--batch N` overrides either way.
- Receipts and `map --json` show the batch size and mark a defaulted model or batch.
- The skill carries the delegate and do-not-delegate lists and documents the defaults.

### Changed

- `map` no longer requires `-m`; it falls back to the configured default model.
- `gpt-5-*` under `--thinking off` sends `reasoning_effort: minimal` with a warning,
  since those models reject `none`. `map` prints the warning once per run.

## [0.3.0] - 2026-11-05

### Added

- Output protection: `ask` and `map` mark every answer file before the request, and
  a `PreToolUse` guard for Claude Code and Codex denies reading those files through
  the hosts' file tools and common shell readers. `enmaas inspect PATH` previews up
  to 2,000 characters; `enmaas allow-read PATH`, run by a person, grants a full read.
- Token budgets: `--token-budget N` stops a `map` run once it has spent N metered
  tokens, and a `[budget]` table in the config enforces a rolling ceiling across
  `ask` and `map`. Every `map` request records its charge in `results.jsonl`, and
  every `ask` records its charge in the usage ledger.
- Unencodable characters in a model response are replaced and reported instead of
  crashing the run.

## [0.2.0] - 2026-11-03

### Added

- `enmaas record` counts gateway work done by other tools, so `usage` stays honest.
- `[prices.*]` config table, USD per million tokens, so `usage` can price calls.
- `map` records cached and reasoning token counts per call.

### Fixed

- `gpt-*` models were rejected on every request because the body carried the wrong
  token-limit field; it is now chosen per model.
- `gemini-*` rejected `--thinking off`; it now receives the lowest reasoning effort
  it accepts, with a warning.
- `enmaas usage` no longer requires a credential.

## [0.1.0] - 2026-10-30

### Added

- `enmaas ask` for one-shot prompts and `enmaas map` for fan-out over a JSONL file,
  with concurrency, retries, a circuit breaker and `--resume`. Answers are written
  to files; the terminal gets a receipt.
- `--schema` for JSON Schema output, validated locally on both wire dialects.
- A usage ledger with `enmaas usage`.
- A skill that tells Claude Code and Codex when to delegate.

[Unreleased]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/enmaas-cli/enmaas-cli/releases/tag/v0.1.0
