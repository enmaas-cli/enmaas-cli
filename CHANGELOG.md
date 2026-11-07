# Changelog

All notable changes to enmaas. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Nothing yet.

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

[Unreleased]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/enmaas-cli/enmaas-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/enmaas-cli/enmaas-cli/releases/tag/v0.1.0
