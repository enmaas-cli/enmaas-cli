# Security

## Reporting a problem

If you find a security problem in enmaas, do not open a public issue. Email
eparenti@redhat.com with the details and a way to reproduce it. You will get a
reply within three working days.

## What the guard is

The output guard denies the ordinary file reads a coding assistant makes on a
protected answer: the hosts' file tools and the common shell readers, including
pipelines, redirections, globs and the file commands inside `sed` and `awk`
programs. It reads Bash `$'...'` quoting the way Bash does, and denies a reader
command whose quoting it cannot decode. It answers inside a fixed time budget and
denies what it cannot finish checking. It protects against accidental reads by a well-behaved assistant. It is
not a sandbox: an assistant that writes its own program to open the file, copies
or hard-links the file first, or edits the marker files, is outside its scope,
and so is any tool the hosts do not route through `PreToolUse`. The markers live
in the user's own directories and are writable by the user.

## What counts

- The output guard letting an assistant read a protected answer through a
  supported tool or shell reader, or taking longer than its time budget to
  answer.
- The installer changing, deleting or exposing host settings it did not write.
- The usage ledger recording prompt or response content, or a key.
- Any path by which the key leaves the machine other than the gateway request.

## What the tool does with your key

The key is read from `--key-file`, `ENMAAS_KEY`, the `key_file` named in the
config file, or `~/.secrets/enmaas`, sent only to the configured `base_url` as a
bearer token over HTTPS (HTTP is allowed for localhost only), and never written to
the ledger, receipts or logs. A key that holds a character an HTTP header cannot
carry, such as a newline, a tab or a non-ASCII letter, is refused before any
request, and the error never shows any part of it. Gateway error bodies, the
receipt preview and the `models` output, as text or JSON, are redacted before
they are shown. A gateway that echoes the key inside an answer file is outside
what the tool can detect. A gateway on `localhost` or a loopback address
is always reached directly, never through a proxy from the environment.

## What the tool keeps on disk

The state directory (`~/.local/state/enmaas/runs/` by default) is created with
mode 0700. The usage ledger, the open-run records and each `map` index are
created with mode 0600. They are opened without following a symbolic link and
without blocking, and only a regular file is read or written, so a link or a
FIFO planted at one of those paths cannot redirect a write or hang a command.
With a token budget configured, a command that cannot write the ledger refuses
to start, because spend it cannot record would not count against the budget.
Before it sends a request it proves that the ledger opens for writing, that the
state directory has room for a row, and that the file size limit allows one.
Room can still run out while the request is in flight. The write then fails and
the command warns that the run was not recorded.

## What the tool prints

Text from the gateway or from a file, printed to a terminal, has every
character that cannot be printed replaced: receipt previews and error lines,
paths in receipts, the `models` list and `enmaas inspect`. A reply that holds a
terminal escape sequence therefore cannot set the clipboard or change the
title. Answer files keep the bytes the model sent. A `--schema` file can only
refer to its own parts, so checking an answer never fetches anything.
