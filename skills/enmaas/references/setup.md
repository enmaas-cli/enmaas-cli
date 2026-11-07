# Set up enmaas for the user

Read this when the user requests setup or when the command or guard is missing.
Complete the local setup yourself where authorized. Ask the user to enter their
key and to answer host trust prompts. Do not send them through a manual build.

## Check prerequisites first

Check, in this order, before running anything else: `python3 --version` is 3.11
or newer; `git` is on `PATH`; `uv` or `pipx` is on `PATH` (if neither is, run
`python3 -m pip install --user uv` and make sure `~/.local/bin` is on `PATH`);
the host's configuration directory exists (`~/.claude` or `~/.codex`, or the
`CLAUDE_CONFIG_DIR` / `CODEX_HOME` override), because the installer skips a host
whose directory is absent.

## Install or update the CLI, skill and guard

Everything installs from a clone of `https://github.com/enmaas-cli/enmaas-cli`:

```bash
git clone https://github.com/enmaas-cli/enmaas-cli.git
cd enmaas-cli
./install.sh
```

To update an existing install, `git pull` in that clone and run `./install.sh`
again. A complete run leaves the command, the skill copy and the guard copy at
the same version; if it stops partway, fix what it reports and run it again. An
older CLI may lack `inspect`, `--version` or output markers.

After it finishes, check `enmaas --version`, `enmaas --help` and
`enmaas inspect --help` on the assistant's `PATH` before proceeding.

## Connect the user's key

Credential lookup order is `--key-file`, then `ENMAAS_KEY`, then
`~/.secrets/enmaas`. Preserve an existing working credential source.

For a new key file, create its parent directory and an empty file accessible
only to the user. Use directory mode 700 and file mode 600. Do not truncate an
existing file. Ask the user to paste their key in their editor and never in the
chat. The file should contain only the token, without quotes or whitespace.

Once the user has saved it, run `enmaas models`. This checks authentication and
lists models without a generation request. Do not print the key or include it in
diagnostic output. A failed listing is a connection or credential problem; it is
no evidence about whether a model request would succeed.

## Check the output guard

`install.sh` copies the guard to `~/.local/share/enmaas/guard/` (or
`$XDG_DATA_HOME/enmaas/guard/`) and registers
`python3 -S ".../guard/hooks/pre-tool-use.py"` as a `PreToolUse` hook in
`~/.claude/settings.json` and in `~/.codex/config.toml` (between
`# >>> enmaas guard` and `# <<< enmaas guard` marker lines). `CLAUDE_CONFIG_DIR`
and `CODEX_HOME` move those two files when set. The guard needs no
installed packages. The `python3` on the host's `PATH` must be 3.11 or newer.

Have the user open `/hooks` in Claude Code and confirm the enmaas `PreToolUse`
command is listed. Codex asks the user to review and trust the hook on first
start; a changed hook can require a fresh trust review. Follow any host request
to restart or reload. Do not add a second registration when one exists.

If the hook is absent, run `./install.sh` again from the clone and read its
output. Copying the skill alone does not load a hook. A working model listing
does not prove the guard is active.

Report which checks passed and any remaining user action. Do not claim setup is
complete while the command is missing, authentication fails, or the guard is
disabled or untrusted.
