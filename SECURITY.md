# Security

## Reporting a problem

If you find a security problem in enmaas, do not open a public issue. Email
eparenti@redhat.com with the details and a way to reproduce it. You will get a
reply within three working days.

## What counts

- The output guard letting an assistant read a protected answer through a
  supported tool or shell reader.
- The installer changing, deleting or exposing host settings it did not write.
- The usage ledger recording prompt or response content, or a key.
- Any path by which the key leaves the machine other than the gateway request.

## What the tool does with your key

The key is read from `--key-file`, `ENMAAS_KEY` or `~/.secrets/enmaas`, sent only
to the configured `base_url` as a bearer token, and never written to the ledger,
receipts or logs.
