# Contributing

## Branch protection is not enforced yet

This repository is private on a Free plan, where GitHub offers neither classic
branch protection nor rulesets. Pull requests and reviews are therefore
convention here, not a gate. Direct pushes to `main` are technically possible
and should not be used.

Protection becomes available if the repository is made public, or if the
organisation moves to a paid plan. The intended settings, ready to apply:

- require a pull request, with at least one approving review
- dismiss stale approvals when new commits are pushed
- require the `check (3.11)`, `check (3.12)`, `check (3.13)`, `check (3.14)` and `install` status checks
- block force pushes and branch deletion
- allow repository admins to bypass, so a solo maintainer is not locked out of
  merging their own work

## Workflow

Branch, open a pull request, let CI run, merge when it is green.

CI runs lint, format, strict type checking and the test suite on Python 3.11,
3.12, 3.13 and 3.14, and runs the installer end to end in a throwaway home. The 3.11 job exists because that is the declared floor and the
version a colleague on an older distribution is most likely to have.

## Before you push

```bash
make check
```

That runs `ruff check`, `ruff format --check`, `mypy` in strict mode, and
`pytest`. All four must pass. If you touched `install.sh` or anything it copies,
also run `make check-install`; it installs into a throwaway home and checks every
step, including a second run and `--uninstall`. Run it without redirecting output: a chain of
checks whose failure is hidden is worse than no chain, and that mistake has
already been made once in this repository's history.

Live tests hit the real gateway and need a credential. They are deselected by
default and never run in CI:

```bash
pytest -q -m live
```

## Things worth knowing

**Tests must not depend on your machine.** Two bugs here passed locally and
failed on a clean runner: a test that set the wrong environment variable name
and only worked because a real key happened to exist at the fallback path, and
a test that cleared a directory's execute bit, which behaves differently across
filesystems. If a test touches the environment, make it hermetic.

**The ledger records metadata only.** Never add prompt text, response text or
file paths to a usage event. A user should be able to share `usage.jsonl`
without disclosing what they were working on.

**No em dashes or en dashes** in prose, comments or docstrings.
