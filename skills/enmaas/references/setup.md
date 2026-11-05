# Set up enmaas for the user

Read this when the user requests setup or when the command or hook is missing.
Complete the local setup yourself where authorized. Ask the user to enter their
key and review host trust prompts. Do not send them through a manual build guide.

## Locate and install the CLI

From this skill's directory, the plugin root is `../..`. Its `cli/`
directory contains `pyproject.toml`; its `hooks/` directory contains the guard.
Use the root of the loaded plugin, not a guessed cache path or the first result
from a search through old installed versions.

Check for Python 3.11 or newer and for `enmaas` on the assistant's `PATH`.
If installation is needed, use the plugin's CLI directory:

```bash
uv tool install -e /absolute/path/to/enmaas/cli
```

After a plugin update, use `uv tool install --force -e` with the same directory
to update an existing installation. The CLI and hook must come from the same
plugin version. An older CLI may lack `inspect` or output markers.

If `uv` is unavailable, use a virtual environment and install the CLI there.
Keep its executable path available to subsequent assistant tool calls. Do not
rely on activation in one shell persisting into the next. Avoid system-wide pip
installation. Check `enmaas --help` and `enmaas inspect --help` before proceeding.

## Connect the user's key

Credential lookup order is `--key-file`, then `ENMAAS_KEY`, then
`~/.secrets/enmaas`. Preserve an existing working credential source.

For a new key file, create its parent directory and an empty file accessible
only to the user. Use directory mode 700 and file mode 600. Do not truncate an
existing file. Ask the user to paste their key in their editor, not in the chat.
The file should contain only the token, without surrounding quotes or whitespace.

Once the user has saved it, run `enmaas models`. This checks authentication and
lists models without a generation request. Do not print the key or include it
in diagnostic output. A failed listing is a connection or credential problem;
it is not evidence that a model request succeeded or failed.

## Check the output guard

The installed plugin bundles `hooks/hooks.json`. It runs
`python3 -S "${CLAUDE_PLUGIN_ROOT}/hooks/pre-tool-use.py"` and imports its policy
from the plugin's source. It needs no agent-efficiency or installed Python
dependencies. The `python3` available to the host must meet the version requirement.

Have the user open `/hooks` and check that the enmaas `PreToolUse` command is
listed. Codex also requires the user to review and trust it. Changed hooks can
require a fresh trust review. Follow any host request to restart or reload.
Do not register a second project hook when the plugin already supplies one.

If the hook is absent, check that the plugin is enabled and the host loaded its
hook configuration. Installing the CLI or copying skill text alone does not
load a hook. A working model listing does not prove that the guard is active.

Report which checks passed and any remaining user action. Do not claim setup
is complete while the command is missing, authentication fails, or the guard
is disabled or untrusted.
