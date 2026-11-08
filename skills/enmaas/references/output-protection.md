# Output protection reference

Read this for unexpected denials, resume behavior, or questions about coverage.
Ordinary delegation needs only the receipt and the main skill's preview rules.

## Which files are protected

| Output | Rule |
|---|---|
| New `map` output | Protect filenames scheduled for generation before any model request |
| `map --resume` | Preserve skipped files and approvals; protect outputs scheduled for retry |
| `ask`, including `ask -o PATH` | Protect the answer before the model request |
| Saved reasoning | Protect `thinking.md` |
| Schema-invalid `ask` answer | Keep the saved answer protected; receipt gives its path |
| Receipts and neighboring source | Remain readable |

Each output parent has a hidden `.enmaas-output` directory with one record per
generated filename. Relative paths and symlink aliases resolve to the same file.
Moving the directory with its markers preserves protection. Copying an answer
alone does not. Do not move or copy files to bypass a denial.

Older default `ask` files named `response.md` or `thinking.md` directly inside
a state-root run directory are recognized without markers. Older custom paths
need a new generation to gain markers. A map resume marks only outputs scheduled for retry; it leaves skipped and
removed historical IDs unchanged. Treat older unmarked
answers as outputs too, even when the hook would permit reading them.

## Supported reads

The hook checks Claude `Read`, content-returning `Grep`, and common literal
shell reads in Claude Code and Codex. Shell coverage includes `cat`, `head`,
`tail`, `sed`, `awk`, `grep`, `rg`, `jq`, and `diff`, and the file commands
inside `sed` (`r`, `R`, `w`, `W`) and `awk` (`getline <`) programs. It handles
quoted paths, globs, home paths, brace alternatives, simple directory changes,
pipelines, heredocs, and input redirection. Common shell keywords and execution wrappers
are checked, along with command substitutions.
Filename-only searches and counts are allowed as metadata.

Use absolute paths from receipts. The hook does not evaluate environment
variables or arbitrary programs to discover file paths. Reader commands fed
filenames through `xargs` are rejected because their paths are dynamic.
It guards against accidental reads; it is not a filesystem security boundary.
Unsupported readers and disabled hooks do not grant permission to read answers.

## Respond to a denial or failure

| Situation | Next action |
|---|---|
| Full-answer read denied | Use the receipt, or inspect one bounded prefix if useful |
| Broad search rejected | Narrow the search to relevant source files |
| Hook cannot check the call | Check its installation and the supported tool input |
| Marker write fails | Check output-directory permissions; generation stops before a request |
| Full answer needed | Explain why and give the user `enmaas allow-read PATH` to run |

For `rg`, the guard enumerates filenames using the same traversal and ignore
options. It does not read search results. Other directory checks use a
conservative filesystem scan. Checks stop at 250,000 entries or three seconds, whichever comes first, and a
check that stops early is a denial. On Claude Code the hook is registered with
`onFailure: "block"`, so a hook that fails or is killed blocks the call. Narrow a rejected search instead of switching readers or disabling
protection. Piped searches without file operands inspect their upstream command;
they do not scan the working directory. Native Grep defaults to filename output.

A human-granted exception persists across sessions. The next generation for
that path restores protection. Resume preserves approval when it skips that
file; a retry that generates an answer restores protection.
Do not run `allow-read` yourself. Do not use `ask --stdout` to avoid file output.

`inspect` accepts one regular file, with a total limit of 1 to 2,000 characters.
The limit includes the truncation notice and final newline. Very small limits
can show only part of the notice. There is no offset or pagination option.
Repeated previews still enlarge context; do not collect a preview of every file.
