## What this changes

<!-- One or two sentences. What is different after this merges. -->

## Why

<!-- The problem being solved. If it is a bug, what the symptom was. -->

## Test plan

<!-- What you ran, and what it said. Paste real output, not a description.
     `make check` covers lint, format, mypy strict and the test suite. -->

```
make check
```

## Checklist

- [ ] `make check` passes locally
- [ ] CI is green on 3.11, 3.12 and 3.13
- [ ] New behaviour has a test that fails without the change
- [ ] No credential, prompt or response content added to logs or the ledger
- [ ] No em dashes in prose
