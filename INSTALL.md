# Install enmaas

This guide takes about ten minutes. It covers a Linux or macOS machine with
Claude Code, Codex, both, or neither.

## Before you start

You need:

- Python 3.11 or newer on your `PATH` as `python3`. Check with `python3 --version`.
- `git`.
- `uv` or `pipx` to install the CLI. If you have neither, `python3 -m pip install --user uv` is the quickest way to get `uv`. Make sure `~/.local/bin` is on your `PATH` afterwards.
- An EnMaaS key. Request one on the
  [EnMaaS page on Source](https://source.redhat.com/departments/products_and_global_engineering/workload_management/enmaas).
  Steps 1, 2 and 5 work without it. Steps 4 and 6 need it.
- Access to this repository. If `git clone` is refused, ask the repository owner (eparenti) to add you.

Claude Code or Codex are optional. The CLI works on its own. The installer sets up
the agent pieces for each host whose configuration directory exists (`~/.claude`,
`~/.codex`). If you have installed a host but never started it, start it once
before step 2, or run the installer again afterwards.

## 1. Get the code

```bash
git clone https://github.com/enmaas-cli/enmaas-cli.git
cd enmaas-cli
```

## 2. Run the installer

```bash
./install.sh
```

It does four things and prints each one:

1. Installs the `enmaas` command with `uv tool install` (or `pipx`).
2. Copies the agent skill into `~/.claude/skills/enmaas/` and `~/.codex/skills/enmaas/`, for each host directory that exists. The skill is what teaches the assistant when to delegate and how to handle the results.
3. Installs the output guard, a small `PreToolUse` hook, and registers it in `~/.claude/settings.json` and `~/.codex/config.toml`. The guard stops an assistant from reading a full answer file into its context. Step 6 shows what that looks like.
4. Tells you whether a key file exists at `~/.secrets/enmaas`. It does not check the key itself; step 4 does.

Options:

| Flag | Effect |
|---|---|
| `--cli-only` | Install the command only. No skill, no guard. |
| `--skill-only` | Copy the skill and install the guard. No command. Add `--no-hook` for the skill alone. |
| `--no-hook` | Install the command and skill but skip the output guard. |
| `--dry-run` | Print what would happen and change nothing. |
| `--uninstall` | Remove the command, skill, guard and its registrations. |

The installer is safe to run again at any time. It reinstalls the parts you
selected and leaves everything else in your settings exactly as it was. It checks
both host files before writing and refuses to touch one it cannot edit safely.

The installer honours `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and an absolute
`XDG_DATA_HOME` if you have set them. The paths in this guide are the defaults.

## 3. Add your key

If you already have `~/.secrets/enmaas` from another tool, skip this step.

Create the key file with owner-only permissions, then paste the key into it with
your editor. One line, no quotes, no trailing space.

```bash
mkdir -p -m 700 ~/.secrets
[ -f ~/.secrets/enmaas ] || install -m 600 /dev/null ~/.secrets/enmaas
vi ~/.secrets/enmaas        # or any editor
```

The CLI also accepts `ENMAAS_KEY` in the environment, or `--key-file PATH` placed
before the subcommand, as in `enmaas --key-file PATH models`. The file is the
recommended place because every tool and every assistant session then finds it
without any setup.

## 4. Check that it works

List the model catalog. This authenticates and makes no model call.

```bash
enmaas models
```

Then make one small call:

```bash
echo "Reply with the single word OK." | enmaas ask
```

You should see a receipt like this:

```
enmaas ask ok
  model     rits/zai-org/glm-5-3  (default)  (openai dialect)
  output    /home/you/.local/state/enmaas/runs/2026-11-07T18-02-11-9f3a/response.md  (3 B)
  tokens    in 14   out 2
  elapsed   0.9s
  preview   OK
```

The default model is free. The answer went to the file on the `output` line. That
is how every call works: the file holds the answer, the receipt tells you where.

## 5. Finish the host setup

### Claude Code

Start a new Claude Code session, or restart the one you have open, so it reloads
settings. Type `/hooks` and confirm a `PreToolUse` entry whose command runs
`python3 -S` on `.../enmaas/guard/hooks/pre-tool-use.py`. The skill loads on its own from
`~/.claude/skills/enmaas/`.

### Codex

Start Codex. It asks you to review and trust the new hook the first time. Approve
it once. If it does not ask, open `~/.codex/config.toml` and confirm the block
between the `# >>> enmaas guard` and `# <<< enmaas guard` lines is present.

## 6. Your first delegation

In Claude Code or Codex, from the `enmaas-cli` directory, ask for something that
fits the tool: work that is independent per item, needs no tool calls, and is
useful as a file. For example:

> Use enmaas to summarise docs/reference.md into /tmp/reference-summary.md.

The assistant runs `enmaas ask`, gets a receipt, and tells you where the answer
is. When it tries to read the whole file back, the guard denies the read with a
message that starts `enmaas containment:`. It can preview up to 2,000 characters
with `enmaas inspect PATH`. If you want the assistant to read the whole file, run
this in your own terminal:

```bash
enmaas allow-read /tmp/reference-summary.md
```

That permission stays until the next enmaas call that targets the same path.

For bulk work, the assistant uses `enmaas map` with one output file per item. The
[README](README.md) has a three-item example you can paste, and the
[reference](docs/reference.md) has every flag.

## Update

```bash
cd enmaas-cli
git pull
./install.sh
```

A complete run leaves the command, the skill copy and the guard copy at the same
version. If the run stops partway, fix what it reports and run it again.

## Remove

```bash
./install.sh --uninstall
```

This removes the command, both skill copies, the guard and its two registrations.
An empty `"PreToolUse": []` list is left in `settings.json` so nothing else in
the file moves. Your key file, your config file and your past answers stay where
they are. Delete
`~/.secrets/enmaas`, `~/.config/enmaas/` and `~/.local/state/enmaas/` (or
`$XDG_STATE_HOME/enmaas/`) yourself if you want them gone.

## If something fails

| What you see | What it means | What to do |
|---|---|---|
| `The guard runs under the python3 on PATH, which is Python 3.9...` | The guard runs under whatever `python3` your shell finds, and that one is too old. | Put Python 3.11 or newer first on your `PATH`, or run `./install.sh --no-hook` to go without the guard. |
| `neither uv nor pipx found` | Nothing can install the command. | `python3 -m pip install --user uv`, then rerun. |
| `Claude Code: not installed here, skipping` or the same for Codex | That host's configuration directory does not exist yet. | Start the host once, then run `./install.sh` again. |
| `enmaas: command not found` | `~/.local/bin` is not on your `PATH`. | Add it to your shell profile, or run `uv tool update-shell` and open a new shell. |
| `no EnMaaS key found` | Step 3 is missing. | Create `~/.secrets/enmaas` as shown above. |
| HTTP 401 or 403 from `enmaas models` | The key is wrong, expired or pasted with extra characters. | Open the file, make sure it holds one line and nothing else. Request a new key if it still fails. |
| `/hooks` in Claude Code shows no enmaas entry | Settings were not reloaded, or the file was not written. | Restart Claude Code. If still missing, run `./install.sh` again and read its output. It refuses to write a `settings.json` that is not valid JSON and tells you so. |
| Codex never asked to trust the hook | Config was not reloaded. | Restart Codex. Check `~/.codex/config.toml` for the guard block. |
| `enmaas containment: ...` when the assistant reads a file | The guard is working. The file is a protected answer. | Let the assistant use `enmaas inspect PATH`, or run `enmaas allow-read PATH` yourself. |
| `enmaas containment: hook could not check tool input (SomeError)` | The guard could not run or could not parse the event. The name in brackets says which. | For a `FileNotFoundError` or `ImportError`, run `./install.sh` again. For a `ValueError`, the host sent an event the guard did not expect; report it with the full message. |
| `the enmaas guard marker lines appear inside other content`, `the text between the enmaas guard markers is not the block this installer writes`, or `removing the enmaas guard block would leave invalid TOML` | The guard block in `config.toml` was edited by hand, or text was added inside it. | Open `~/.codex/config.toml`, delete everything from `# >>> enmaas guard` to `# <<< enmaas guard`, then run the installer again. It changes nothing until every check passes. |
| `is a symbolic link to ..., which does not exist`, `is read-only`, or `Cannot write to ..., which is needed for ...` | A settings path points nowhere, or the installer cannot write where it needs to. | Fix the link or the permission, then run the installer again. |
| `holds NaN or Infinity` or `is not valid JSON (duplicate key ...)` | `settings.json` contains a value or a repeated key that a rewrite would silently change. | Fix the file by hand, then run the installer again. |
| `--uninstall` exits 1 and shows a `uv` or `pipx` error | The CLI is installed but the tool could not remove it. | Read the tool's error above the line, fix it, and run `./install.sh --uninstall` again. |
| `exhausted max_tokens during reasoning` | The model spent its output budget thinking. | Add `--thinking off`, or shorten the prompt. On the hosted free models, raising `--max-tokens` was measured to make no difference. |

If none of these match, open an issue in this repository with the full command and
the full output. Do not include your key.
