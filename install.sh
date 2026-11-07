#!/usr/bin/env bash
# Install the enmaas CLI, its agent skill and its output guard.
#
# The CLI is the tool. The skill tells an agent when to use it. The guard is a
# hook that stops an agent from reading a protected answer file directly.
# Claude Code and Codex use the same skill format and the same hook data, so
# one copy of each serves both. Each part goes to every host found here.
#
# Usage:
#   ./install.sh                 install the CLI, the skill and the guard
#   ./install.sh --cli-only      install just the CLI
#   ./install.sh --skill-only    install just the skill and the guard
#   ./install.sh --no-hook       skip the guard
#   ./install.sh --uninstall     remove what this script installs
#   ./install.sh --dry-run       print what would happen and change nothing
#   ./install.sh -h, --help      show this help

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SRC="$HERE/skills/enmaas"
HOOKS_JSON="$HERE/hooks/hooks.json"
GUARD_FILES=(
  hooks/pre-tool-use.py
  src/enmaas_cli/__init__.py
  src/enmaas_cli/outputs.py
  src/enmaas_cli/containment.py
)

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
DATA_DIR="${XDG_DATA_HOME:-}"
[[ "$DATA_DIR" == /* ]] || DATA_DIR="$HOME/.local/share"
GUARD_PARENT="$DATA_DIR/enmaas"
GUARD_DIR="$GUARD_PARENT/guard"
GUARD_SCRIPT="$GUARD_DIR/hooks/pre-tool-use.py"

DO_CLI=1
DO_SKILL=1
DO_HOOK=1
DRY_RUN=0
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --cli-only)   DO_SKILL=0; DO_HOOK=0 ;;
    --skill-only) DO_CLI=0 ;;
    --no-hook)    DO_HOOK=0 ;;
    --dry-run)    DRY_RUN=1 ;;
    --uninstall)  UNINSTALL=1 ;;
    -h|--help)    sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
run() { if [[ $DRY_RUN -eq 1 ]]; then say "  would run: $*"; else "$@"; fi; }
die() { printf '%s\n' "$*" >&2; exit 1; }
indent() { printf '%s\n' "$1" | sed 's/^/    /'; }

# A new guard copy is built in STAGE and the old one waits in ASIDE until the
# swap is done. If the script stops part way, put the old copy back.
STAGE=""
ASIDE=""
cleanup() {
  if [[ -n "$STAGE" ]]; then
    rm -rf "$STAGE"
  fi
  if [[ -n "$ASIDE" ]]; then
    if [[ -e "$ASIDE/guard" && ! -e "$GUARD_DIR" ]]; then
      mv "$ASIDE/guard" "$GUARD_DIR" || return 0
    fi
    rm -rf "$ASIDE"
  fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# --- Host configuration edits ------------------------------------------------
#
# One Python helper does every settings.json and config.toml edit, so the
# install and uninstall paths share the same parsing and the same rules.
# Arguments: action (claude-add, claude-remove, codex-add, codex-remove),
# target file, mode, matcher, guard script path. Mode 0 writes, 1 prints what
# it would write, and check only validates, so a broken file stops the run
# before anything has changed. It prints one status line.
#
# Our hook is any handler whose command contains GUARD_ID. Only those handlers
# are added or removed; other handlers, groups and keys stay as they are.

edit_config() {
  python3 -I -B - "$@" <<'EOF'
import contextlib
import copy
import json
import os
import shlex
import sys
import tempfile

try:
    import tomllib
except ImportError:
    # Adding the guard checks the Python version first. Changing config.toml
    # without this module is refused below.
    tomllib = None

action, path, mode, matcher, guard_script = sys.argv[1:6]
GUARD_ID = "enmaas/guard/hooks/pre-tool-use.py"
BEGIN = "# >>> enmaas guard (managed by enmaas-cli install.sh)"
END = "# <<< enmaas guard"


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def hook_command():
    try:
        guard_script.encode("utf-8")
    except UnicodeEncodeError:
        fail(
            f"The guard path {guard_script!r} is not valid UTF-8. Set XDG_DATA_HOME "
            "to another path, or run this again with --no-hook."
        )
    return "python3 -S " + shlex.quote(guard_script)


def same(a, b):
    """Compare parsed documents by type and value. NaN equals NaN here."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[key], b[key]) for key in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and a != a:
        return b != b
    return a == b


def read_text():
    if os.path.islink(path) and not os.path.exists(path):
        fail(
            f"{path} is a symbolic link to {os.readlink(path)}, which does not exist "
            "or cannot be followed. Fix or remove the link, then run this again."
        )
    if os.path.lexists(path) and not os.path.isfile(path):
        fail(f"{path} is not a regular file. Move it, then run this again.")
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        fail(f"{path} is not valid UTF-8 text. Fix or move it, then run this again.")
    except OSError as exc:
        fail(f"Cannot read {path} ({exc.strerror}). Fix its permissions, then run this again.")


def check_writable():
    # Write through a symbolic link to its target, so the link stays a link.
    real = os.path.realpath(path)
    directory = os.path.dirname(real)
    if not os.access(directory, os.W_OK):
        fail(
            f"Cannot write to {directory}, which holds {path}. "
            "Fix its permissions, then run this again."
        )
    if os.path.exists(real) and not os.access(real, os.W_OK):
        fail(f"{path} is read-only. Make it writable, then run this again.")


def write_text(old, new, done):
    if old == new:
        if mode != "check":
            print(f"  {path}: already up to date")
        return
    check_writable()
    if mode == "check":
        return
    if mode == "1":
        print(f"  would write {path}:")
        for line in new.splitlines():
            print(f"    {line}")
        return
    real = os.path.realpath(path)
    perms = os.stat(real).st_mode & 0o7777 if old is not None else 0o644
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=os.path.dirname(real),
        prefix=f".{os.path.basename(real)}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(new)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, perms)
        # Something else may have saved the file since it was read.
        if read_text() != old:
            fail(f"{path} changed while install.sh was running. Nothing was written. Run this again.")
        os.replace(handle.name, real)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(handle.name)
        raise
    print(f"  {path}: {done}")


def is_ours(handler):
    return (
        isinstance(handler, dict)
        and isinstance(handler.get("command"), str)
        and GUARD_ID in handler["command"]
    )


def holds_ours(group):
    return (
        isinstance(group, dict)
        and isinstance(group.get("hooks"), list)
        and any(is_ours(handler) for handler in group["hooks"])
    )


# --- settings.json ---


def strip_ours(groups):
    """Drop our handlers from every group, and any group that only held ours.

    Returns the groups that remain and the index, among them, where the first
    group that held one of our handlers was. The index is None if no group did.
    """
    kept = []
    first = None
    for group in groups:
        if not holds_ours(group):
            kept.append(group)
            continue
        if first is None:
            first = len(kept)
        others = [handler for handler in group["hooks"] if not is_ours(handler)]
        if others:
            group = dict(group)
            group["hooks"] = others
            kept.append(group)
    return kept, first


def load_json(text):
    def no_duplicates(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"duplicate key {key!r}")
            seen.add(key)
        return dict(pairs)

    return json.loads(text, object_pairs_hook=no_duplicates)


def dump_json(value):
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError:
        fail(
            f"{path} holds NaN or Infinity, which standard JSON does not allow. "
            "Fix it, then run this again."
        )


def claude(add):
    old = read_text()
    if old is None and not add:
        if mode != "check":
            print(f"  {path}: not present, nothing to remove")
        return
    try:
        settings = load_json(old) if old is not None else {}
    except (ValueError, RecursionError) as exc:
        fail(f"{path} is not valid JSON ({exc}). Fix or move it, then run this again.")
    if not isinstance(settings, dict):
        fail(f"{path} does not hold a JSON object. Fix or move it, then run this again.")
    original = copy.deepcopy(settings)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        fail(f"{path}: \"hooks\" is not an object. Fix it, then run this again.")
    groups = hooks.get("PreToolUse", [])
    if not isinstance(groups, list):
        fail(f"{path}: \"hooks.PreToolUse\" is not a list. Fix it, then run this again.")

    kept, first = strip_ours(groups)
    if add:
        entry = {
            "matcher": matcher,
            "hooks": [{"type": "command", "command": hook_command(), "timeout": 5}],
        }
        # Keep the guard where it was if it is already registered.
        kept.insert(len(kept) if first is None else first, entry)
        hooks["PreToolUse"] = kept
        settings["hooks"] = hooks
        done = "registered the enmaas guard"
    else:
        if first is None:
            if mode != "check":
                print(f"  {path}: no enmaas guard registered")
            return
        # The hooks object and the PreToolUse list stay, even when empty.
        hooks["PreToolUse"] = kept
        done = "removed the enmaas guard entry"
    new = old if same(settings, original) else dump_json(settings)
    write_text(old, new, done)


# --- config.toml ---


def toml_string(value):
    # A JSON string is a TOML basic string, except that TOML wants DEL escaped.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def pre_tool_use(doc):
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    if isinstance(hooks, dict) and isinstance(hooks.get("PreToolUse"), list):
        return hooks["PreToolUse"]
    return []


def tidy(doc):
    hooks = doc.get("hooks")
    if isinstance(hooks, dict):
        if hooks.get("PreToolUse") == []:
            del hooks["PreToolUse"]
        if not hooks:
            del doc["hooks"]
    return doc


def is_rest_plus(full, entry, rest):
    """True if full is rest plus one hooks.PreToolUse entry equal to entry."""
    rest = tidy(copy.deepcopy(rest))
    for index, candidate in enumerate(pre_tool_use(full)):
        if not same(candidate, entry):
            continue
        smaller = copy.deepcopy(full)
        del smaller["hooks"]["PreToolUse"][index]
        if same(tidy(smaller), rest):
            return True
    return False


def parse_toml(text, problem):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        fail(problem.replace("{exc}", str(exc)))


def codex(add):
    old = read_text()
    if old is None and not add:
        if mode != "check":
            print(f"  {path}: not present, nothing to remove")
        return
    text = old or ""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if len(starts) > 1 or len(ends) != len(starts) or (starts and ends[0] < starts[0]):
        fail(f"{path}: the enmaas guard markers are broken. Remove the block by hand.")
    if not starts and not add:
        if mode != "check":
            print(f"  {path}: no enmaas guard registered")
        return
    if tomllib is None:
        fail(
            f"Changing {path} safely needs Python 3.11 or newer. Put a newer python3 "
            "first on PATH, or remove the block between the enmaas guard markers by hand."
        )
    old_doc = parse_toml(text, f"{path} is not valid TOML ({{exc}}). Fix or move it, then run this again.")

    if starts:
        begin, end = starts[0], ends[0]
        head = "".join(lines[:begin])
        tail = "".join(lines[end + 1 :])
        if not any(holds_ours(entry) for entry in pre_tool_use(old_doc)):
            fail(
                f"{path}: the enmaas guard marker lines appear inside other content, "
                "such as a multi-line string, and no guard hook is registered. "
                "Fix the file by hand, then run this again."
            )
        try:
            block_doc = tomllib.loads("".join(lines[begin : end + 1]))
        except tomllib.TOMLDecodeError:
            block_doc = None
        entries = pre_tool_use(block_doc)
        if (
            not isinstance(block_doc, dict)
            or list(block_doc) != ["hooks"]
            or list(block_doc["hooks"]) != ["PreToolUse"]
            or len(entries) != 1
            or not holds_ours(entries[0])
        ):
            fail(
                f"{path}: the text between the enmaas guard markers is not the block "
                "this installer writes. Remove the block by hand, then run this again."
            )
        rest = head + tail
        rest_doc = parse_toml(
            rest,
            f"{path}: removing the enmaas guard block would leave invalid TOML ({{exc}}). "
            "Remove the block by hand, then run this again.",
        )
        if not is_rest_plus(old_doc, entries[0], rest_doc):
            fail(
                f"{path}: removing the enmaas guard block would also change other settings. "
                f"Lines below the \"{END}\" line belong to the guard's table. Move them "
                "above the block or under their own table header, then run this again."
            )
    else:
        head, tail, rest, rest_doc = text, "", text, old_doc

    if add:
        block = (
            f"{BEGIN}\n"
            "[[hooks.PreToolUse]]\n"
            f"matcher = {toml_string(matcher)}\n"
            f"hooks = [{{ type = \"command\", command = {toml_string(hook_command())}"
            ", timeout = 5 }]\n"
            f"{END}\n"
        )
        if starts:
            new = head + block + tail
        else:
            new = text + ("" if text == "" or text.endswith("\n") else "\n") + block
        new_doc = parse_toml(
            new,
            f"{path}: adding the guard would make the file invalid ({{exc}}). "
            "It may already define hooks.PreToolUse another way. Merge it by hand.",
        )
        entry = pre_tool_use(tomllib.loads(block))[0]
        if not is_rest_plus(new_doc, entry, rest_doc):
            fail(
                f"{path}: adding the guard block would also change other settings. "
                "Merge it by hand."
            )
        done = "registered the enmaas guard"
    else:
        new = rest
        done = "removed the enmaas guard block"
    write_text(old, new, done)


if action == "claude-add":
    claude(True)
elif action == "claude-remove":
    claude(False)
elif action == "codex-add":
    codex(True)
elif action == "codex-remove":
    codex(False)
else:
    fail(f"unknown action: {action}")
EOF
}

require_python() {
  command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH. Install Python 3.11 or newer."
  if ! python3 -I -B -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    die "The guard runs under the python3 on PATH, which is $(python3 -V 2>&1).
It needs Python 3.11 or newer. Put a newer python3 first on PATH, or run
this again with --no-hook to skip the guard."
  fi
}

hook_matcher() {
  python3 -I -B -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["hooks"]["PreToolUse"][0]["matcher"])
' "$HOOKS_JSON"
}

# Validate both host files before the first change on disk.
check_host_files() {
  local verb="$1" matcher="$2" script="$3"
  if [[ -d "$CLAUDE_DIR" ]]; then
    edit_config "claude-$verb" "$CLAUDE_DIR/settings.json" check "$matcher" "$script"
  fi
  if [[ -d "$CODEX_DIR" ]]; then
    edit_config "codex-$verb" "$CODEX_DIR/config.toml" check "$matcher" "$script"
  fi
}

# --- Write access ------------------------------------------------------------

# Print the nearest path at or above $1 that exists.
nearest_existing() {
  local dir="$1"
  while [[ ! -e "$dir" && "$dir" != "/" && "$dir" != "." ]]; do
    dir="$(dirname "$dir")"
  done
  printf '%s\n' "$dir"
}

# Refuse unless directory $2 exists and is writable, or can be created.
need_writable() {
  local label="$1" near
  near="$(nearest_existing "$2")"
  if [[ ! -d "$near" || ! -w "$near" ]]; then
    die "Cannot write to $near, which is needed for $label.
Fix its permissions, then run this again."
  fi
}

# Refuse unless $2, if present, can be removed: its parent and every
# directory inside it must be writable.
need_removable() {
  local label="$1" target="$2" stuck
  if [[ ! -e "$target" && ! -L "$target" ]]; then
    return 0
  fi
  need_writable "$label" "$(dirname "$target")"
  if [[ -L "$target" || ! -d "$target" ]]; then
    return 0
  fi
  stuck="$(find "$target" -type d -exec sh -c \
    'for d in "$@"; do [ -w "$d" ] || printf "%s\n" "$d"; done' sh {} +)" \
    || die "Cannot read all of $target. Fix its permissions, then run this again."
  if [[ -n "$stuck" ]]; then
    die "Cannot write to $(printf '%s\n' "$stuck" | head -n 1), which is needed for $label.
Fix its permissions, then run this again."
  fi
}

# --- Uninstall ---------------------------------------------------------------

remove_dir() {
  local label="$1" dir="$2"
  if [[ -d "$dir" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "  $label: would remove $dir"
    else
      rm -rf "$dir"
      say "  $label: removed $dir"
    fi
  else
    say "  $label: nothing at $dir"
  fi
}

lists_enmaas() { grep -Eq '^enmaas-cli( |$)' <<<"$1"; }

# Remove the CLI through one tool: $1 is the tool name, the rest is its
# list command. A tool that cannot list its installs is reported, never taken
# to mean the CLI is absent. A listed CLI that fails to uninstall returns 1.
remove_cli_with() {
  local tool="$1" out
  shift
  if [[ $DRY_RUN -eq 1 ]]; then
    say "  would run: $tool uninstall enmaas-cli, if $tool lists it"
    return 0
  fi
  if ! out="$("$@" 2>&1)"; then
    say "  $tool: could not check for enmaas-cli, because '$*' failed:"
    indent "$out"
    CLI_UNCHECKED=1
    return 0
  fi
  if ! lists_enmaas "$out"; then
    say "  $tool: enmaas-cli not installed"
    return 0
  fi
  if [[ "$tool" == uv ]]; then
    out="$(uv tool uninstall enmaas-cli 2>&1)" || {
      say "  uv: could not remove enmaas-cli:"
      indent "$out"
      return 1
    }
  else
    out="$(pipx uninstall enmaas-cli 2>&1)" || {
      say "  pipx: could not remove enmaas-cli:"
      indent "$out"
      return 1
    }
  fi
  say "  $tool: removed enmaas-cli"
  CLI_REMOVED=1
}

remove_cli() {
  local failed=0
  CLI_REMOVED=0
  CLI_UNCHECKED=0
  if command -v uv >/dev/null 2>&1; then
    remove_cli_with uv uv tool list || failed=1
  fi
  if command -v pipx >/dev/null 2>&1; then
    remove_cli_with pipx pipx list --short || failed=1
  fi
  if [[ $failed -eq 1 ]]; then
    die "enmaas-cli could not be removed. Fix the problem shown above, then run this again."
  fi
  if [[ $DRY_RUN -eq 0 && $CLI_REMOVED -eq 0 && $CLI_UNCHECKED -eq 0 ]]; then
    say "  no enmaas-cli install found through uv or pipx"
  fi
}

if [[ $UNINSTALL -eq 1 ]]; then
  if [[ $DO_HOOK -eq 1 ]]; then
    command -v python3 >/dev/null 2>&1 || die "python3 is needed to edit host settings."
    check_host_files remove "" ""
    need_removable "removing the guard copy" "$GUARD_DIR"
  fi
  if [[ $DO_SKILL -eq 1 ]]; then
    need_removable "removing the Claude Code skill" "$CLAUDE_DIR/skills/enmaas"
    need_removable "removing the Codex skill" "$CODEX_DIR/skills/enmaas"
  fi
  if [[ $DO_HOOK -eq 1 ]]; then
    say "Removing the output guard"
    if [[ -d "$CLAUDE_DIR" ]]; then
      edit_config claude-remove "$CLAUDE_DIR/settings.json" "$DRY_RUN" "" ""
    fi
    if [[ -d "$CODEX_DIR" ]]; then
      edit_config codex-remove "$CODEX_DIR/config.toml" "$DRY_RUN" "" ""
    fi
    remove_dir "guard copy" "$GUARD_DIR"
    if [[ $DRY_RUN -eq 0 && -d "$GUARD_PARENT" ]]; then
      rmdir "$GUARD_PARENT" 2>/dev/null || true
    fi
  fi
  if [[ $DO_SKILL -eq 1 ]]; then
    say "Removing the agent skill"
    remove_dir "Claude Code" "$CLAUDE_DIR/skills/enmaas"
    remove_dir "Codex" "$CODEX_DIR/skills/enmaas"
  fi
  if [[ $DO_CLI -eq 1 ]]; then
    say "Removing the enmaas CLI"
    remove_cli
  fi
  say ""
  say "Your key at ~/.secrets/enmaas, if any, is left in place."
  exit 0
fi

# --- Preflight ---------------------------------------------------------------

if [[ $DO_HOOK -eq 1 ]]; then
  require_python
  for relative in "${GUARD_FILES[@]}"; do
    [[ -f "$HERE/$relative" ]] || die "missing $HERE/$relative"
  done
  MATCHER="$(hook_matcher)"
  check_host_files add "$MATCHER" "$GUARD_SCRIPT"
fi
if [[ $DO_SKILL -eq 1 ]]; then
  for root in "$CLAUDE_DIR" "$CODEX_DIR"; do
    if [[ -d "$root" ]]; then
      need_writable "the skill" "$root/skills"
      need_removable "the skill" "$root/skills/enmaas"
    fi
  done
fi
if [[ $DO_HOOK -eq 1 ]]; then
  need_writable "the guard copy" "$GUARD_PARENT"
  need_removable "the guard copy" "$GUARD_DIR"
  if [[ $DRY_RUN -eq 0 ]]; then
    mkdir -p "$GUARD_PARENT" || die "Cannot create $GUARD_PARENT."
  fi
fi

# --- CLI ---------------------------------------------------------------------

if [[ $DO_CLI -eq 1 ]]; then
  say "Installing the enmaas CLI"
  if command -v uv >/dev/null 2>&1; then
    run uv tool install --force "$HERE"
  elif command -v pipx >/dev/null 2>&1; then
    run pipx install --force "$HERE"
  else
    say "  neither uv nor pipx found."
    say "  Install one of them and run this again. Or install the CLI alone by hand:"
    say "    python3 -m venv .venv && . .venv/bin/activate && pip install '$HERE'"
    say "  then run ./install.sh --skill-only for the skill and the guard."
    exit 1
  fi
  say "  done"
fi

# --- Skill -------------------------------------------------------------------

install_skill() {
  local agent="$1" root="$2"
  local dest="$root/skills/enmaas"

  if [[ ! -d "$root" ]]; then
    say "  $agent: not installed here, skipping"
    return
  fi

  if [[ -e "$dest" ]] && ! diff -rq "$SKILL_SRC" "$dest" >/dev/null 2>&1; then
    say "  $agent: replacing an existing and different skill at $dest"
  fi

  run rm -rf "$dest"
  run mkdir -p "$(dirname "$dest")"
  run cp -R "$SKILL_SRC" "$dest"
  say "  $agent: $dest"
}

if [[ $DO_SKILL -eq 1 ]]; then
  say "Installing the agent skill"
  [[ -f "$SKILL_SRC/SKILL.md" ]] || die "missing $SKILL_SRC/SKILL.md"
  install_skill "Claude Code" "$CLAUDE_DIR"
  install_skill "Codex" "$CODEX_DIR"
fi

# --- Output guard ------------------------------------------------------------

check_guard() {
  local script="$1" out
  out="$(printf '{"tool_name":"Read","cwd":"/","tool_input":{"file_path":"/dev/null"}}' \
    | python3 -S -B "$script")" \
    || die "The new guard copy failed to run. The installed copy, if any, is unchanged."
  [[ -z "$out" ]] \
    || die "The new guard copy denied an ordinary read: $out
The installed copy, if any, is unchanged."
}

# Build the new copy beside the old one, check it, then swap the two, so a
# copy that fails its check never replaces a working one.
install_guard() {
  if [[ $DRY_RUN -eq 1 ]]; then
    say "  would copy the guard into a new directory under $GUARD_PARENT,"
    say "  check it, then swap it in at $GUARD_DIR"
    return
  fi
  STAGE="$(mktemp -d "$GUARD_PARENT/.guard-new.XXXXXX")"
  chmod "$(umask -S)" "$STAGE"
  for relative in "${GUARD_FILES[@]}"; do
    mkdir -p "$(dirname "$STAGE/$relative")"
    cp "$HERE/$relative" "$STAGE/$relative"
  done
  check_guard "$STAGE/hooks/pre-tool-use.py"
  if [[ -e "$GUARD_DIR" || -L "$GUARD_DIR" ]]; then
    ASIDE="$(mktemp -d "$GUARD_PARENT/.guard-old.XXXXXX")"
    mv "$GUARD_DIR" "$ASIDE/guard"
  fi
  mv "$STAGE" "$GUARD_DIR"
  STAGE=""
  if [[ -n "$ASIDE" ]]; then
    rm -rf "$ASIDE"
    ASIDE=""
  fi
}

if [[ $DO_HOOK -eq 1 ]]; then
  say "Installing the output guard"
  install_guard
  say "  guard copy: $GUARD_DIR"

  if [[ -d "$CLAUDE_DIR" ]]; then
    say "  Claude Code:"
    edit_config claude-add "$CLAUDE_DIR/settings.json" "$DRY_RUN" "$MATCHER" "$GUARD_SCRIPT"
    say "  Next: restart Claude Code. The guard is listed under PreToolUse in /hooks."
  else
    say "  Claude Code: not installed here, skipping"
  fi

  if [[ -d "$CODEX_DIR" ]]; then
    say "  Codex:"
    edit_config codex-add "$CODEX_DIR/config.toml" "$DRY_RUN" "$MATCHER" "$GUARD_SCRIPT"
    say "  Next: start Codex. It asks you to review and trust the new hook; approve it."
  else
    say "  Codex: not installed here, skipping"
  fi
else
  say "Skipping the output guard (--no-hook or --cli-only)"
fi

# --- Credentials -------------------------------------------------------------

say ""
if [[ -f "$HOME/.secrets/enmaas" ]]; then
  say "A key is already present at ~/.secrets/enmaas."
else
  say "No key file at ~/.secrets/enmaas. Request a key through the EnMaaS page on Source, then:"
  say "  mkdir -p -m 700 ~/.secrets && [ -f ~/.secrets/enmaas ] || install -m 600 /dev/null ~/.secrets/enmaas"
  say "  # paste the key into that file as a single line, no quotes"
fi
say ""
say "Check it works:  enmaas models"
