# Security

## Reporting a problem

If you find a security problem in enmaas, do not open a public issue. Email
eparenti@redhat.com with the details and a way to reproduce it. You will get a
reply within three working days.

## What the guard is

The output guard denies the ordinary file reads a coding assistant makes on a
protected answer: the hosts' file tools and the common shell readers, including
pipelines, redirections, globs and the file commands inside `sed` and `awk`
programs. It answers inside a fixed time budget and denies what it cannot finish
checking. It protects against accidental reads by a well-behaved assistant. It is
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
the ledger, receipts or logs. Gateway error bodies are redacted before they are
shown. A gateway that echoes the key inside an answer is outside what the tool can
detect.
