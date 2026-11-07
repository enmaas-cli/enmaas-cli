#!/usr/bin/env bash
# Check install.sh end to end for a brand-new user.
#
# Everything happens under a temporary HOME: the CLI install through uv, the
# skill copies, the guard copy and the edits to the Claude Code and Codex
# settings. The registered guard is then run the way a host runs it. Fixtures
# after the main run cover settings files the installer must keep intact or
# refuse to touch, and the ways the gateway URL and the key file reach
# ~/.config/enmaas/config.toml. Nothing outside the temporary directory is
# changed. Run it with: make check-install

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
cleanup() { chmod -R u+w "$TMP" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

# --- Isolated environment --------------------------------------------------

# Drop settings that point at the real user's files or at an active virtualenv.
# PYTHONPYCACHEPREFIX would send Python bytecode outside the temporary HOME.
unset CLAUDE_CONFIG_DIR CODEX_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_STATE_HOME \
  PIPX_HOME PIPX_BIN_DIR PIPX_MAN_DIR UV_CACHE_DIR UV_CONFIG_FILE \
  UV_PYTHON_INSTALL_DIR UV_PYTHON_BIN_DIR PYTHONPATH PYTHONHOME PYTHONPYCACHEPREFIX \
  ENMAAS_BASE_URL ENMAAS_KEY
# The checks below are plain Python; keep them from being optimized away.
export PYTHONOPTIMIZE=
VENV_BIN="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin}"
unset VIRTUAL_ENV

UV_BIN="$(command -v uv || true)"
[[ -n "$UV_BIN" ]] || { echo "FAIL: uv is required for this test"; exit 1; }
REAL_PYTHON="$(command -v python3 || true)"
[[ -n "$REAL_PYTHON" ]] || { echo "FAIL: python3 is required for this test"; exit 1; }
REAL_CP="$(command -v cp)"
REAL_MV="$(command -v mv)"

# Keep only PATH entries without an enmaas of their own, so the test sees just
# the copy it installs.
clean_path=""
IFS=: read -ra path_parts <<<"$PATH"
for dir in "${path_parts[@]}"; do
  [[ -n "$dir" && "$dir" != "$VENV_BIN" && ! -x "$dir/enmaas" ]] || continue
  clean_path="${clean_path:+$clean_path:}$dir"
done

export HOME="$TMP/home"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_STATE_HOME="$HOME/.local/state"
export UV_TOOL_DIR="$HOME/.local/share/uv/tools"
export UV_TOOL_BIN_DIR="$HOME/.local/bin"
export PATH="$UV_TOOL_BIN_DIR:$clean_path"
if ! command -v uv >/dev/null 2>&1; then
  mkdir -p "$TMP/shims"
  ln -s "$UV_BIN" "$TMP/shims/uv"
  PATH="$PATH:$TMP/shims"
fi
mkdir -p "$HOME"

CLAUDE="$HOME/.claude"
CODEX="$HOME/.codex"
ENMAAS_CONFIG="$HOME/.config/enmaas/config.toml"
GUARD="$XDG_DATA_HOME/enmaas/guard"
WORK="$TMP/work"
mkdir -p "$CLAUDE" "$CODEX" "$WORK"

GUARD_ID="enmaas/guard/hooks/pre-tool-use.py"
BEGIN_MARK="# >>> enmaas guard (managed by enmaas-cli install.sh)"
END_MARK="# <<< enmaas guard"
GATEWAY_LINE='base_url = "https://gw.example.test"'
KEY_SENTENCE="Your organization gives you the gateway URL and an API key."
NO_GATEWAY="No gateway URL. Run this again with --gateway URL, or set base_url in ~/.config/enmaas/config.toml."

# --- Helpers ---------------------------------------------------------------

FAILS=0
check() {
  local name="$1"
  shift
  if "$@"; then
    echo "PASS: $name"
  else
    echo "FAIL: $name"
    FAILS=$((FAILS + 1))
  fi
}

install_run() {
  local log="$TMP/install-$1.log"
  shift
  if (cd "$REPO" && ./install.sh "$@") </dev/null >"$log" 2>&1; then
    echo "PASS: install.sh${*:+ $*} exits 0"
  else
    echo "FAIL: install.sh${*:+ $*} exits 0. Output:"
    sed 's/^/    /' "$log"
    FAILS=$((FAILS + 1))
    echo "FAIL: stopping early"
    exit 1
  fi
}

# Every Python check starts with need(), which fails the check with a message.
PRELUDE='
import sys
def need(condition, message="check failed"):
    if not condition:
        raise SystemExit(message)
'
py() {
  local code="$1"
  shift
  python3 -B -c "$PRELUDE$code" "$@"
}

# List every path under $1 with its type, mode, size, mtime, link target and
# content hash. Uses only Python, so it also runs on stock macOS.
snapshot() {
  py '
import hashlib, os, stat
root = sys.argv[1]
paths = [root]
for top, dirs, files in os.walk(root):
    paths.extend(os.path.join(top, name) for name in dirs + files)
for full in sorted(paths):
    info = os.lstat(full)
    row = [os.path.relpath(full, root), oct(info.st_mode), str(info.st_mtime_ns)]
    if stat.S_ISLNK(info.st_mode):
        row.append("link " + os.readlink(full))
    elif stat.S_ISREG(info.st_mode):
        with open(full, "rb") as handle:
            row.append(str(info.st_size) + " " + hashlib.sha256(handle.read()).hexdigest())
    print(" ".join(row))
' "$1"
}

guard_entries() {
  py '
import json
settings = json.load(open(sys.argv[1], encoding="utf-8"))
groups = settings.get("hooks", {}).get("PreToolUse", [])
print(sum(1 for g in groups for h in g["hooks"] if sys.argv[2] in h.get("command", "")))
' "$1" "$GUARD_ID"
}

install_log_says() { grep -qF -- "$2" "$TMP/install-$1.log"; }

# True if the file $1 holds exactly the text $2 plus a newline.
holds_exactly() {
  py '
text = open(sys.argv[1], encoding="utf-8", newline="").read()
need(text == sys.argv[2] + "\n", repr(text))
' "$1" "$2"
}

on_path() {
  hash -r
  [[ "$(command -v enmaas || true)" == "$UV_TOOL_BIN_DIR/enmaas" ]]
}

not_on_path() {
  hash -r
  ! command -v enmaas >/dev/null 2>&1
}

runs_help() { enmaas --help >/dev/null 2>&1; }

file_count_is() { [[ "$(find "$1" -type f | wc -l)" -eq "$2" ]]; }

same_json() {
  py '
import json
need(json.load(open(sys.argv[1], encoding="utf-8")) == json.loads(sys.argv[2]),
     open(sys.argv[1], encoding="utf-8").read())
' "$1" "$2"
}

# The command registered for the guard in settings.json $1.
registered_command() {
  py '
import json
s = json.load(open(sys.argv[1], encoding="utf-8"))
for group in s["hooks"]["PreToolUse"]:
    for hook in group["hooks"]:
        if sys.argv[2] in hook["command"]:
            print(hook["command"])
' "$1" "$GUARD_ID"
}

# --- Seed existing host settings -------------------------------------------

SEED_JSON='{"model":"opus","hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo hi"}]}],"PreToolUse":[{"matcher":"Write","hooks":[{"type":"command","command":"echo other"}]}]}}'
printf '%s' "$SEED_JSON" >"$CLAUDE/settings.json"
printf '%s' "$SEED_JSON" >"$TMP/seed.json"
# Another tool's PreToolUse table comes first; it must survive untouched.
printf '%s\n' 'model = "gpt-5"' 'approval_policy = "on-request"' '' \
  '[[hooks.PreToolUse]]' 'matcher = "Bash"' \
  'hooks = [{ type = "command", command = "other-tool check", timeout = 10 }]' '' \
  '[tui]' >"$CODEX/config.toml"
printf 'notifications = true' >>"$CODEX/config.toml"
cp "$CODEX/config.toml" "$TMP/seed.toml"

[[ "$HOME" == "$TMP"/* ]] || { echo "FAIL: HOME is not inside the temporary directory"; exit 1; }

# --- Dry runs on a fresh HOME change nothing -------------------------------
#
# These run before any step that executes the guard, so a dry run that ran
# the guard or wrote Python bytecode into HOME would show up here.
# PYTHONPYCACHEPREFIX points outside that HOME and catches bytecode written
# anywhere else.

DRY_HOME="$TMP/dry-home"
DRY_PYCACHE="$TMP/dry-pycache"
mkdir -p "$DRY_HOME/.claude" "$DRY_HOME/.codex"
cp "$TMP/seed.json" "$DRY_HOME/.claude/settings.json"
cp "$TMP/seed.toml" "$DRY_HOME/.codex/config.toml"
fresh_dry_run() {
  local log="$TMP/install-$1.log"
  shift
  if (cd "$REPO" && HOME="$DRY_HOME" XDG_DATA_HOME="$DRY_HOME/.local/share" \
    PYTHONPYCACHEPREFIX="$DRY_PYCACHE" ./install.sh "$@") </dev/null >"$log" 2>&1; then
    echo "PASS: fresh HOME: install.sh $* exits 0"
  else
    echo "FAIL: fresh HOME: install.sh $* exits 0. Output:"
    sed 's/^/    /' "$log"
    FAILS=$((FAILS + 1))
  fi
}
snapshot "$DRY_HOME" >"$TMP/dry-home.before"
fresh_dry_run fresh-dry-run --dry-run --gateway https://gw.example.test/
fresh_dry_run fresh-dry-uninstall --dry-run --uninstall
snapshot "$DRY_HOME" >"$TMP/dry-home.after"
check "fresh HOME: --dry-run and --dry-run --uninstall change nothing" \
  cmp -s "$TMP/dry-home.before" "$TMP/dry-home.after"
check "fresh HOME: the dry runs write no Python bytecode" test ! -e "$DRY_PYCACHE"

# --- First install ---------------------------------------------------------

install_run first --gateway https://gw.example.test/

check "enmaas resolves to the test install on PATH" on_path
check "enmaas --help runs" runs_help
for host in "$CLAUDE" "$CODEX"; do
  check "skill in $host matches the source byte for byte" \
    diff -r "$REPO/skills/enmaas" "$host/skills/enmaas"
done
for relative in hooks/pre-tool-use.py src/enmaas_cli/__init__.py \
  src/enmaas_cli/outputs.py src/enmaas_cli/containment.py; do
  check "guard copy has $relative" cmp -s "$REPO/$relative" "$GUARD/$relative"
done
check "guard copy has exactly four files" file_count_is "$GUARD" 4

check "settings.json keeps model, SessionStart and the Write hook" py '
import json
s = json.load(open(sys.argv[1], encoding="utf-8"))
pre = s["hooks"]["PreToolUse"]
need(s["model"] == "opus")
need(s["hooks"]["SessionStart"] == [{"hooks": [{"type": "command", "command": "echo hi"}]}])
need(pre[0] == {"matcher": "Write", "hooks": [{"type": "command", "command": "echo other"}]})
' "$CLAUDE/settings.json"
check "settings.json has exactly one enmaas guard entry" \
  test "$(guard_entries "$CLAUDE/settings.json")" = 1
check "settings.json guard entry uses the bundled matcher, timeout 5" py '
import json, shlex
s = json.load(open(sys.argv[1], encoding="utf-8"))
matcher = json.load(open(sys.argv[2], encoding="utf-8"))["hooks"]["PreToolUse"][0]["matcher"]
entry = s["hooks"]["PreToolUse"][-1]
need(entry["matcher"] == matcher)
need(len(entry["hooks"]) == 1)
hook = entry["hooks"][0]
need(hook["type"] == "command" and hook["timeout"] == 5)
need(hook["command"] == "python3 -S " + shlex.quote(sys.argv[3]), hook["command"])
' "$CLAUDE/settings.json" "$REPO/hooks/hooks.json" "$GUARD/hooks/pre-tool-use.py"

check "config.toml keeps the seeded lines" py '
text = open(sys.argv[1], encoding="utf-8").read()
seed = open(sys.argv[2], encoding="utf-8").read()
need(text.startswith(seed + "\n"))
' "$CODEX/config.toml" "$TMP/seed.toml"
check "config.toml has exactly one marker block" py '
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
need(lines.count(sys.argv[2]) == 1)
need(lines.count(sys.argv[3]) == 1)
' "$CODEX/config.toml" "$BEGIN_MARK" "$END_MARK"
check "config.toml keeps the other tool's hook and registers the same hook as settings.json" py '
import json, tomllib
config = tomllib.load(open(sys.argv[1], "rb"))
settings = json.load(open(sys.argv[2], encoding="utf-8"))
need(config["model"] == "gpt-5" and config["tui"] == {"notifications": True})
other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool check", "timeout": 10}]}
need(config["hooks"]["PreToolUse"] == [other, settings["hooks"]["PreToolUse"][-1]])
' "$CODEX/config.toml" "$CLAUDE/settings.json"

check "--gateway creates the enmaas config.toml without the trailing slash" \
  holds_exactly "$ENMAAS_CONFIG" "$GATEWAY_LINE"
check "with no key file, the install says where the key comes from" \
  install_log_says first "$KEY_SENTENCE"
check "with no key file, the install shows how to create one without truncating" \
  install_log_says first "[ -f ~/.secrets/enmaas ] || install -m 600 /dev/null ~/.secrets/enmaas"

cp "$CLAUDE/settings.json" "$TMP/settings.after-first"
cp "$CODEX/config.toml" "$TMP/config.after-first"
cp "$ENMAAS_CONFIG" "$TMP/enmaas-config.after-first"

# --- Second install --------------------------------------------------------

install_run second --gateway https://gw.example.test/

check "settings.json unchanged by a second install" cmp -s "$CLAUDE/settings.json" \
  "$TMP/settings.after-first"
check "config.toml unchanged by a second install" cmp -s "$CODEX/config.toml" \
  "$TMP/config.after-first"
check "the enmaas config.toml is byte-identical after a second --gateway run" \
  cmp -s "$ENMAAS_CONFIG" "$TMP/enmaas-config.after-first"
check "guard copy still has exactly four files" file_count_is "$GUARD" 4
check "no staged or old guard copies left behind" \
  test "$(find "$XDG_DATA_HOME/enmaas" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1
for host in "$CLAUDE" "$CODEX"; do
  check "no staged or old skill copies left in $host/skills" \
    test "$(find "$host/skills" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1
done

# --- A key file in another place -------------------------------------------

OTHER_KEY="$HOME/keys/enmaas key"
mkdir -p "$(dirname "$OTHER_KEY")"
printf 'sk-test-key\n' >"$OTHER_KEY"
chmod 644 "$OTHER_KEY"
install_run key-file --cli-only --key-file "$OTHER_KEY"
check "--key-file adds key_file to the enmaas config.toml and keeps base_url" py '
import tomllib
need(tomllib.load(open(sys.argv[1], "rb")) == {
    "base_url": "https://gw.example.test", "key_file": sys.argv[2]})
' "$ENMAAS_CONFIG" "$OTHER_KEY"
check "a mode 644 key file gets a warning with the chmod to run" \
  install_log_says key-file "chmod 600 $(printf '%q' "$OTHER_KEY")"
check "a key file that is present is not offered for creation" \
  test "$(grep -cF "$KEY_SENTENCE" "$TMP/install-key-file.log")" -eq 0
# shellcheck disable=SC2016 # the inner shell expands $1
check "enmaas usage runs with key_file in the config" \
  bash -c 'cd "$1" && enmaas usage >/dev/null' _ "$TMP"
chmod 600 "$OTHER_KEY"

# --- Run the registered guard as a host would ------------------------------

COMMAND="$(registered_command "$CLAUDE/settings.json")"

ANSWER="$WORK/answer with spaces.md"
ORDINARY="$WORK/source.py"
printf 'print("source")\n' >"$ORDINARY"
"$UV_TOOL_DIR/enmaas-cli/bin/python" -B -c '
import sys
from pathlib import Path
from enmaas_cli.outputs import protect
path = Path(sys.argv[1])
path.write_text("PRIVATE RESPONSE BODY", encoding="utf-8")
protect(path)
' "$ANSWER"

read_event() {
  py '
import json
print(json.dumps({"tool_name": "Read", "cwd": sys.argv[1], "tool_input": {"file_path": sys.argv[2]}}))
' "$WORK" "$1"
}

run_guard() { (cd "$TMP" && printf '%s' "$2" | bash -c "$1"); }

is_deny() {
  printf '%s' "$1" | py '
import json
decision = json.load(sys.stdin)["hookSpecificOutput"]
need(decision["hookEventName"] == "PreToolUse")
need(decision["permissionDecision"] == "deny")
need(decision["permissionDecisionReason"].startswith(sys.argv[1]), decision)
' "${2:-}"
}

out="$(run_guard "$COMMAND" "$(read_event "$ANSWER")")" || out="guard exited non-zero"
check "guard denies a Read of a protected answer" is_deny "$out"
out="$(run_guard "$COMMAND" "$(read_event "$ORDINARY")")" || out="guard exited non-zero"
check "guard allows a Read of an ordinary file (empty stdout)" test -z "$out"
out="$(run_guard "$COMMAND" '{invalid')" || out="guard exited non-zero"
check "guard denies invalid JSON with an enmaas containment reason" \
  is_deny "$out" "enmaas containment:"

# --- Dry runs change nothing -----------------------------------------------

snapshot "$HOME" >"$TMP/before"
install_run dry-run --dry-run
snapshot "$HOME" >"$TMP/after"
check "--dry-run changes nothing under HOME" cmp -s "$TMP/before" "$TMP/after"

install_run dry-uninstall --dry-run --uninstall
snapshot "$HOME" >"$TMP/after"
check "--dry-run --uninstall changes nothing under HOME" cmp -s "$TMP/before" "$TMP/after"

# --- Uninstall -------------------------------------------------------------

cp "$ENMAAS_CONFIG" "$TMP/enmaas-config.before-uninstall"
install_run uninstall --uninstall

check "guard copy removed" test ! -e "$GUARD"
check "Claude Code skill removed" test ! -e "$CLAUDE/skills/enmaas"
check "Codex skill removed" test ! -e "$CODEX/skills/enmaas"
check "settings.json has no enmaas guard entry" test "$(guard_entries "$CLAUDE/settings.json")" = 0
check "settings.json equals the seeded settings" py '
import json
need(json.load(open(sys.argv[1], encoding="utf-8")) == json.load(open(sys.argv[2], encoding="utf-8")))
' "$CLAUDE/settings.json" "$TMP/seed.json"
check "config.toml equals the seed plus at most one newline" py '
text = open(sys.argv[1], encoding="utf-8", newline="").read()
seed = open(sys.argv[2], encoding="utf-8", newline="").read()
need(text in (seed, seed + "\n"), repr(text))
' "$CODEX/config.toml" "$TMP/seed.toml"
check "enmaas no longer on PATH" not_on_path
check "uninstall leaves the enmaas config.toml alone" \
  cmp -s "$ENMAAS_CONFIG" "$TMP/enmaas-config.before-uninstall"

# --- Fixtures --------------------------------------------------------------
#
# Each fixture has its own Claude Code dir, Codex dir and data dir under
# $FIXTURES/NAME, and runs with --skill-only, so the CLI is not reinstalled.

FIXTURES="$HOME/fixtures"
FX_RUNS=0
FX_LOG=""

# fx_run NAME EXPECTED_STATUS ARGS...
# Set FX_DATA to override the data dir, FX_PATH to put a directory first on PATH,
# FX_PATH_ONLY to replace PATH, FX_HOME to run with another HOME. Stdin is never
# a terminal.
fx_run() {
  local name="$1" expected="$2" status=0
  shift 2
  local fx="$FIXTURES/$name"
  FX_RUNS=$((FX_RUNS + 1))
  FX_LOG="$TMP/fixture-$FX_RUNS-$name.log"
  (
    cd "$REPO"
    export CLAUDE_CONFIG_DIR="$fx/claude" CODEX_HOME="$fx/codex"
    export XDG_DATA_HOME="${FX_DATA:-$fx/data}"
    HOME="${FX_HOME:-$HOME}" PATH="${FX_PATH_ONLY:-${FX_PATH:+$FX_PATH:}$PATH}" ./install.sh "$@"
  ) </dev/null >"$FX_LOG" 2>&1 || status=$?
  if [[ $status -eq $expected ]]; then
    echo "PASS: fixture $name: install.sh $* exits $expected"
  else
    echo "FAIL: fixture $name: install.sh $* exits $expected (got $status). Output:"
    sed 's/^/    /' "$FX_LOG"
    FAILS=$((FAILS + 1))
  fi
}

log_says() { grep -qF -- "$1" "$FX_LOG"; }

# A user handler that shares a group with ours survives every step.
fx="$FIXTURES/shared-group"
mkdir -p "$fx/claude"
printf '%s' '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"python3 -S /old/place/enmaas/guard/hooks/pre-tool-use.py"},{"type":"command","command":"echo audit"}]}]}}' \
  >"$fx/claude/settings.json"
AUDIT_GROUP='{"matcher":"Bash","hooks":[{"type":"command","command":"echo audit"}]}'
fx_run shared-group 0 --skill-only
check "shared group: audit handler kept, guard in a group of its own" py '
import json
s = json.load(open(sys.argv[1], encoding="utf-8"))
pre = s["hooks"]["PreToolUse"]
audit = json.loads(sys.argv[3])
need(pre.count(audit) == 1, pre)
ours = [g for g in pre if any(sys.argv[2] in h["command"] for h in g["hooks"])]
need(len(ours) == 1 and len(ours[0]["hooks"]) == 1, pre)
need("/old/place/" not in json.dumps(s), pre)
need(len(pre) == 2, pre)
' "$fx/claude/settings.json" "$GUARD_ID" "$AUDIT_GROUP"
cp "$fx/claude/settings.json" "$TMP/shared-group.after-first"
fx_run shared-group 0 --skill-only
check "shared group: settings.json unchanged by a second install" \
  cmp -s "$fx/claude/settings.json" "$TMP/shared-group.after-first"
fx_run shared-group 0 --skill-only --uninstall
check "shared group: uninstall leaves only the audit handler" \
  same_json "$fx/claude/settings.json" "{\"hooks\":{\"PreToolUse\":[$AUDIT_GROUP]}}"

# An empty hooks object comes back with an empty PreToolUse list. The
# installer never deletes the hooks object or the list, so this is the
# accepted outcome of an install followed by an uninstall.
fx="$FIXTURES/empty-hooks"
mkdir -p "$fx/claude"
printf '%s' '{"hooks": {}}' >"$fx/claude/settings.json"
fx_run empty-hooks 0 --skill-only
check "empty hooks: one guard entry after install" \
  test "$(guard_entries "$fx/claude/settings.json")" = 1
fx_run empty-hooks 0 --skill-only --uninstall
check "empty hooks: uninstall leaves an empty PreToolUse list" \
  same_json "$fx/claude/settings.json" '{"hooks": {"PreToolUse": []}}'

# A symlinked settings.json stays a symlink and its target is edited.
fx="$FIXTURES/symlink"
mkdir -p "$fx/claude" "$HOME/elsewhere"
printf '%s' "$SEED_JSON" >"$HOME/elsewhere/settings.json"
ln -s "../../../elsewhere/settings.json" "$fx/claude/settings.json"
fx_run symlink 0 --skill-only
check "symlink: settings.json is still a symlink after install" test -L "$fx/claude/settings.json"
check "symlink: the link target holds the guard entry" \
  test "$(guard_entries "$HOME/elsewhere/settings.json")" = 1
fx_run symlink 0 --skill-only --uninstall
check "symlink: settings.json is still a symlink after uninstall" test -L "$fx/claude/settings.json"
check "symlink: the link target equals the seed after uninstall" \
  same_json "$HOME/elsewhere/settings.json" "$SEED_JSON"

# Files the installer must refuse to edit. Each refusal changes nothing.
refuse_fixture() {
  local name="$1" settings="$2" toml="$3" message="$4"
  local fx="$FIXTURES/$name"
  mkdir -p "$fx/claude" "$fx/codex"
  if [[ -n "$settings" ]]; then printf '%s' "$settings" >"$fx/claude/settings.json"; fi
  if [[ -n "$toml" ]]; then printf '%s' "$toml" >"$fx/codex/config.toml"; fi
  snapshot "$fx" >"$TMP/$name.before"
  fx_run "$name" 1 --skill-only
  check "$name: the refusal names the problem" log_says "$message"
  snapshot "$fx" >"$TMP/$name.after"
  check "$name: nothing changed" cmp -s "$TMP/$name.before" "$TMP/$name.after"
}

# Marker lines inside a multi-line string are not our block.
refuse_fixture markers-in-string "" "$(printf '%s\n' \
  'developer_instructions = """' 'Keep this text.' "$BEGIN_MARK" \
  '[[hooks.PreToolUse]]' "$END_MARK" '"""')" "appear inside other content"
# NaN cannot be written back as standard JSON.
refuse_fixture nan-number '{"x": NaN}' "" "holds NaN or Infinity"
refuse_fixture duplicate-key '{"a": 1, "a": 2}' "" "duplicate key"
# An unpaired surrogate parses but cannot be written back as UTF-8.
refuse_fixture lone-surrogate '{"note":"\ud800"}' "" \
  "holds an unpaired surrogate escape"
# A dangling symlink is an error.
fx="$FIXTURES/dangling-link"
mkdir -p "$fx/claude"
ln -s "$HOME/nowhere/settings.json" "$fx/claude/settings.json"
refuse_fixture dangling-link "" "" "which does not exist"

# Lines appended after the closing marker belong to our table, so
# removing the block would change them. Uninstall refuses.
fx="$FIXTURES/appended"
mkdir -p "$fx/codex"
printf '%s\n' 'model = "gpt-5"' '' '[tui]' 'notifications = true' >"$fx/codex/config.toml"
fx_run appended 0 --skill-only
printf 'model = "x"\n' >>"$fx/codex/config.toml"
snapshot "$fx" >"$TMP/appended.before"
fx_run appended 1 --skill-only --uninstall
check "appended: the refusal says other settings would change" \
  log_says "would also change other settings"
snapshot "$fx" >"$TMP/appended.after"
check "appended: uninstall changed nothing" cmp -s "$TMP/appended.before" "$TMP/appended.after"

# A second handler added inside the managed block would be lost when the block
# is rewritten or removed, so install and uninstall both refuse.
fx="$FIXTURES/edited-block"
mkdir -p "$fx/codex"
fx_run edited-block 0 --skill-only
sed 's/timeout = 5 }]$/timeout = 5 }, { type = "command", command = "echo audit" }]/' \
  "$fx/codex/config.toml" >"$TMP/edited-block.toml"
cp "$TMP/edited-block.toml" "$fx/codex/config.toml"
check "edited block: the block holds a second handler" \
  grep -qF '{ type = "command", command = "echo audit" }' "$fx/codex/config.toml"
snapshot "$fx" >"$TMP/edited-block.before"
EDITED_BLOCK="the enmaas guard block was edited by hand"
fx_run edited-block 1 --skill-only
check "edited block: install says the block was edited by hand" log_says "$EDITED_BLOCK"
fx_run edited-block 1 --skill-only --uninstall
check "edited block: uninstall says the block was edited by hand" log_says "$EDITED_BLOCK"
snapshot "$fx" >"$TMP/edited-block.after"
check "edited block: nothing changed" \
  cmp -s "$TMP/edited-block.before" "$TMP/edited-block.after"

# A guard table left without its marker lines is still a registration.
# Uninstall refuses and keeps the guard copy the table points at.
fx="$FIXTURES/unmarked"
mkdir -p "$fx/codex"
fx_run unmarked 0 --skill-only
grep -vxF -e "$BEGIN_MARK" -e "$END_MARK" "$fx/codex/config.toml" >"$TMP/unmarked.toml"
cp "$TMP/unmarked.toml" "$fx/codex/config.toml"
snapshot "$fx" >"$TMP/unmarked.before"
UNMARKED="the enmaas guard is registered in config.toml without the enmaas markers"
fx_run unmarked 1 --skill-only --uninstall
check "unmarked: uninstall says the guard is registered without markers" log_says "$UNMARKED"
check "unmarked: the guard copy is still there" \
  test -f "$fx/data/enmaas/guard/hooks/pre-tool-use.py"
fx_run unmarked 1 --skill-only
check "unmarked: install says the guard is registered without markers" log_says "$UNMARKED"
snapshot "$fx" >"$TMP/unmarked.after"
check "unmarked: nothing changed" cmp -s "$TMP/unmarked.before" "$TMP/unmarked.after"

# A skill copy that fails part way leaves the installed skill in place.
CP_SHIM="$TMP/failing-cp"
mkdir -p "$CP_SHIM"
# shellcheck disable=SC2016 # the shim expands these itself
printf '#!/bin/sh\ncase "$*" in *skills/enmaas*) echo "simulated copy failure" >&2; exit 1 ;; esac\nexec "%s" "$@"\n' \
  "$REAL_CP" >"$CP_SHIM/cp"
chmod +x "$CP_SHIM/cp"
fx="$FIXTURES/skill-copy"
mkdir -p "$fx/claude"
fx_run skill-copy 0 --skill-only
printf 'kept\n' >"$fx/claude/skills/enmaas/user-note.txt"
snapshot "$fx/claude/skills/enmaas" >"$TMP/skill-copy.before"
FX_PATH="$CP_SHIM" fx_run skill-copy 1 --skill-only
check "skill copy: the copy error is shown" log_says "simulated copy failure"
check "skill copy: the failure says the installed skill is unchanged" \
  log_says "The installed skill at $fx/claude/skills/enmaas, if any, is unchanged."
# A missing skill makes the snapshot fail; the comparison below reports it.
snapshot "$fx/claude/skills/enmaas" >"$TMP/skill-copy.after" 2>&1 || true
check "skill copy: the installed skill is unchanged" \
  cmp -s "$TMP/skill-copy.before" "$TMP/skill-copy.after"
check "skill copy: no staged skill copies left behind" \
  test "$(find "$fx/claude/skills" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1

# A guard copy that is a relative symbolic link survives a failed swap. The
# link points nowhere while it waits in the old-copy directory.
MV_SHIM="$TMP/failing-mv"
mkdir -p "$MV_SHIM"
fx="$FIXTURES/guard-link"
mkdir -p "$fx/claude"
fx_run guard-link 0 --skill-only
LINK_PARENT="$fx/data/enmaas"
mv "$LINK_PARENT/guard" "$LINK_PARENT/guard-real"
ln -s guard-real "$LINK_PARENT/guard"
# Only the rename of the new copy fails. The link is put back.
# shellcheck disable=SC2016 # the shim expands these itself
printf '#!/bin/sh\ncase "$1" in */.guard-new.*) echo "simulated rename failure" >&2; exit 1 ;; esac\nexec "%s" "$@"\n' \
  "$REAL_MV" >"$MV_SHIM/mv"
chmod +x "$MV_SHIM/mv"
FX_PATH="$MV_SHIM" fx_run guard-link 1 --skill-only
check "guard link: the relative link is back in place" \
  test "$(readlink "$LINK_PARENT/guard")" = guard-real
check "guard link: the failure says the previous copy is back" \
  log_says "Put the previous copy back at $LINK_PARENT/guard."
check "guard link: no staged or old guard copies left behind" \
  test "$(find "$LINK_PARENT" -mindepth 1 -maxdepth 1 | wc -l)" -eq 2
# Every move into the guard path fails, so the link cannot be put back either.
# The backup is kept and the message names it.
# shellcheck disable=SC2016 # the shim expands these itself
printf '#!/bin/sh\nfor last; do :; done\nif [ "$last" = "%s" ]; then echo "simulated rename failure" >&2; exit 1; fi\nexec "%s" "$@"\n' \
  "$LINK_PARENT/guard" "$REAL_MV" >"$MV_SHIM/mv"
FX_PATH="$MV_SHIM" fx_run guard-link 1 --skill-only
LINK_BACKUP="$(find "$LINK_PARENT" -mindepth 2 -maxdepth 2 -path '*/.guard-old.*/guard')"
check "guard link: the backup link still exists" test -L "$LINK_BACKUP"
check "guard link: the backup still points at the sibling directory" \
  test "$(readlink "$LINK_BACKUP")" = guard-real
check "guard link: the failure names the backup" log_says "It is kept at $LINK_BACKUP."
check "guard link: the failure names the registered guard" \
  log_says "The hosts run the guard from $LINK_PARENT/guard/hooks/pre-tool-use.py"

# A data dir with a space, quotes, a dollar sign, a backslash and a
# backtick. The registered command must still run the guard.
fx="$FIXTURES/odd-path"
mkdir -p "$fx/claude" "$fx/codex"
# shellcheck disable=SC2016 # the name is meant to hold a literal $HOME
ODD_DATA="$fx/"'odd dir $HOME "q" \z `t`'
FX_DATA="$ODD_DATA" fx_run odd-path 0 --skill-only
ODD_COMMAND="$(registered_command "$fx/claude/settings.json")" || ODD_COMMAND="none registered"
out="$(run_guard "$ODD_COMMAND" "$(read_event "$ANSWER")")" || out="guard exited non-zero"
check "odd path: the registered command denies a protected read" is_deny "$out"
out="$(run_guard "$ODD_COMMAND" "$(read_event "$ORDINARY")")" || out="guard exited non-zero"
check "odd path: the registered command allows an ordinary read" test -z "$out"
check "odd path: config.toml registers the same command" py '
import tomllib
config = tomllib.load(open(sys.argv[1], "rb"))
need(config["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == sys.argv[2])
' "$fx/codex/config.toml" "$ODD_COMMAND"
FX_DATA="$ODD_DATA" fx_run odd-path 0 --skill-only --uninstall
check "odd path: guard copy removed" test ! -e "$ODD_DATA/enmaas/guard"
check "odd path: settings.json has no guard entry" \
  test "$(guard_entries "$fx/claude/settings.json")" = 0

# A new guard copy that fails its check leaves the installed one in place.
fx="$FIXTURES/staged"
mkdir -p "$fx/claude" "$TMP/broken-python"
fx_run staged 0 --skill-only
snapshot "$fx/data/enmaas/guard" >"$TMP/staged.before"
# shellcheck disable=SC2016 # the shim expands these itself
printf '#!/bin/sh\ncase "$1 $2 $3" in "-S -B "*/pre-tool-use.py) exit 1 ;; esac\nexec "%s" "$@"\n' \
  "$REAL_PYTHON" >"$TMP/broken-python/python3"
chmod +x "$TMP/broken-python/python3"
FX_PATH="$TMP/broken-python" fx_run staged 1 --skill-only
check "staged: the failure is reported" log_says "The new guard copy failed to run"
snapshot "$fx/data/enmaas/guard" >"$TMP/staged.after"
check "staged: the installed guard copy is unchanged" \
  cmp -s "$TMP/staged.before" "$TMP/staged.after"
check "staged: no staged or old guard copies left behind" \
  test "$(find "$fx/data/enmaas" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1

# No write access stops the run before anything changes. Root can write
# anywhere, so these only run for a normal user.
if [[ "$(id -u)" -ne 0 ]]; then
  fx="$FIXTURES/read-only-data"
  mkdir -p "$fx/claude" "$fx/data"
  chmod 555 "$fx/data"
  snapshot "$fx" >"$TMP/read-only-data.before"
  fx_run read-only-data 1 --skill-only
  check "read-only data dir: the refusal names the directory" log_says "Cannot write to $fx/data"
  snapshot "$fx" >"$TMP/read-only-data.after"
  check "read-only data dir: nothing changed" \
    cmp -s "$TMP/read-only-data.before" "$TMP/read-only-data.after"
  chmod 755 "$fx/data"

  fx="$FIXTURES/read-only-codex"
  mkdir -p "$fx/claude" "$fx/codex"
  chmod 555 "$fx/codex"
  snapshot "$fx" >"$TMP/read-only-codex.before"
  fx_run read-only-codex 1 --skill-only
  check "read-only Codex dir: the refusal names the directory" log_says "Cannot write to $fx/codex"
  snapshot "$fx" >"$TMP/read-only-codex.after"
  check "read-only Codex dir: nothing changed" \
    cmp -s "$TMP/read-only-codex.before" "$TMP/read-only-codex.after"
  chmod 755 "$fx/codex"
fi

# A CLI that is listed but fails to uninstall is an error. It is never reported as absent.
SHIMS="$TMP/cli-shims"
mkdir -p "$SHIMS"
# shellcheck disable=SC2016 # the shim expands these itself
printf '%s\n' '#!/bin/sh' \
  'case "$1 $2" in' \
  '  "tool list") printf "enmaas-cli v0.0.0\n- enmaas\n" ;;' \
  '  "tool uninstall") echo "simulated uninstall failure" >&2; exit 2 ;;' \
  '  *) exit 3 ;;' \
  'esac' >"$SHIMS/uv"
printf '%s\n' '#!/bin/sh' 'exit 0' >"$SHIMS/pipx"
chmod +x "$SHIMS/uv" "$SHIMS/pipx"
FX_PATH="$SHIMS" fx_run cli-fails 1 --uninstall --cli-only
check "CLI uninstall failure: the tool's error is shown" log_says "simulated uninstall failure"
printf '%s\n' '#!/bin/sh' 'exit 0' >"$SHIMS/uv"
FX_PATH="$SHIMS" fx_run cli-absent 0 --uninstall --cli-only
check "CLI not listed: reported as not installed" log_says "uv: enmaas-cli not installed"

# A pipx that lists the CLI but fails to remove it is an error too.
# shellcheck disable=SC2016 # the shim expands these itself
printf '%s\n' '#!/bin/sh' \
  'case "$1" in' \
  '  list) printf "enmaas-cli 0.0.0\n" ;;' \
  '  uninstall) echo "simulated pipx failure" >&2; exit 1 ;;' \
  '  *) exit 3 ;;' \
  'esac' >"$SHIMS/pipx"
FX_PATH="$SHIMS" fx_run pipx-fails 1 --uninstall --cli-only
check "pipx uninstall failure: the tool's error is shown" log_says "simulated pipx failure"
check "pipx uninstall failure: the failure names pipx" log_says "pipx: could not remove enmaas-cli:"

# A uv that cannot list its installs proves nothing. With no other tool to
# check, uninstall fails.
# shellcheck disable=SC2016 # the shim expands these itself
printf '%s\n' '#!/bin/sh' \
  'case "$1 $2" in' \
  '  "tool list") echo "simulated list failure" >&2; exit 2 ;;' \
  '  *) exit 3 ;;' \
  'esac' >"$SHIMS/uv"
NO_PIPX="$TMP/no-pipx"
mkdir -p "$NO_PIPX/shims" "$NO_PIPX/core"
cp "$SHIMS/uv" "$NO_PIPX/shims/uv"
for tool in bash dirname grep sed; do
  ln -s "$(command -v "$tool")" "$NO_PIPX/core/$tool"
done
no_pipx_path="$NO_PIPX/shims:$NO_PIPX/core"
IFS=: read -ra path_parts <<<"$PATH"
for dir in "${path_parts[@]}"; do
  [[ -n "$dir" && ! -e "$dir/pipx" ]] || continue
  no_pipx_path="$no_pipx_path:$dir"
done
# shellcheck disable=SC2016 # the inner shell expands $1
check "no pipx on the PATH for the list-failure fixture" \
  bash -c 'PATH="$1"; ! command -v pipx >/dev/null' _ "$no_pipx_path"
FX_PATH_ONLY="$no_pipx_path" fx_run cli-list-fails 1 --uninstall --cli-only
check "CLI list failure: the tool's error is shown" log_says "simulated list failure"
check "CLI list failure: removal could not be confirmed" \
  log_says "Could not confirm that enmaas-cli is removed, because uv could not list its installs."
# If another tool confirms the CLI is absent, uninstall succeeds and names the
# tool that could not be checked.
printf '%s\n' '#!/bin/sh' 'exit 0' >"$SHIMS/pipx"
FX_PATH="$SHIMS" fx_run cli-list-partial 0 --uninstall --cli-only
check "CLI partial list failure: the unchecked tool is named" \
  log_says "note: uv could not be checked, so enmaas-cli may still be installed through it."

# --- Gateway URL fixtures --------------------------------------------------
#
# Each runs with --cli-only and its own HOME. A uv that does nothing stands in
# for the real one, so the CLI is not reinstalled; only the settings matter.

NOOP_UV="$TMP/noop-uv"
mkdir -p "$NOOP_UV"
printf '%s\n' '#!/bin/sh' 'exit 0' >"$NOOP_UV/uv"
chmod +x "$NOOP_UV/uv"

# An existing config.toml keeps every other line; base_url is replaced in place.
fx="$FIXTURES/gateway-replace"
mkdir -p "$fx/home/.config/enmaas"
printf '%s\n' '# my enmaas settings' 'default_model = "m"' \
  'base_url = "https://old.example.test"' 'concurrency = 2' '' \
  '[budget]' 'window_days = 30' 'tokens = 1000' >"$fx/home/.config/enmaas/config.toml"
sed 's|^base_url = .*|'"$GATEWAY_LINE"'|' "$fx/home/.config/enmaas/config.toml" \
  >"$TMP/gateway-replace.expected"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-replace 0 --cli-only \
  --gateway https://gw.example.test/
check "gateway replace: only the base_url line changed" \
  cmp -s "$fx/home/.config/enmaas/config.toml" "$TMP/gateway-replace.expected"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-replace 0 --cli-only \
  --gateway https://gw.example.test/
check "gateway replace: a second run is byte-identical" \
  cmp -s "$fx/home/.config/enmaas/config.toml" "$TMP/gateway-replace.expected"

# Without a base_url line, it goes above the first table and its comment.
fx="$FIXTURES/gateway-insert"
mkdir -p "$fx/home/.config/enmaas"
printf '%s\n' 'default_model = "m"' '' '# what I pay' '[prices."m"]' 'input = 1.0' \
  'output = 2.0' >"$fx/home/.config/enmaas/config.toml"
printf '%s\n' 'default_model = "m"' "$GATEWAY_LINE" '' '# what I pay' '[prices."m"]' \
  'input = 1.0' 'output = 2.0' >"$TMP/gateway-insert.expected"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-insert 0 --cli-only \
  --gateway https://gw.example.test/
check "gateway insert: base_url added before the first table, nothing else changed" \
  cmp -s "$fx/home/.config/enmaas/config.toml" "$TMP/gateway-insert.expected"

# A URL without a scheme is refused before anything changes.
fx="$FIXTURES/gateway-bad"
mkdir -p "$fx/home/.config/enmaas"
cp "$FIXTURES/gateway-replace/home/.config/enmaas/config.toml" "$fx/home/.config/enmaas/"
snapshot "$fx" >"$TMP/gateway-bad.before"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-bad 1 --cli-only --gateway not-a-url
check "gateway bad: the refusal names the URL" \
  log_says "Gateway URL must start with http:// or https://: not-a-url"
snapshot "$fx" >"$TMP/gateway-bad.after"
check "gateway bad: nothing changed" cmp -s "$TMP/gateway-bad.before" "$TMP/gateway-bad.after"

# No URL anywhere and no terminal to ask on: stop and say how to give one.
fx="$FIXTURES/gateway-none"
mkdir -p "$fx/home"
snapshot "$fx" >"$TMP/gateway-none.before"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-none 1 --cli-only
check "gateway none: the refusal says how to give the URL" log_says "$NO_GATEWAY"
snapshot "$fx" >"$TMP/gateway-none.after"
check "gateway none: nothing changed" cmp -s "$TMP/gateway-none.before" "$TMP/gateway-none.after"

# $ENMAAS_BASE_URL is used as it is and nothing is written.
fx="$FIXTURES/gateway-env"
mkdir -p "$fx/home"
snapshot "$fx" >"$TMP/gateway-env.before"
ENMAAS_BASE_URL=https://env.example.test FX_HOME="$fx/home" FX_PATH="$NOOP_UV" \
  fx_run gateway-env 0 --cli-only
check "gateway env: the install says the environment URL will be used" \
  log_says "\$ENMAAS_BASE_URL is set, so the CLI will use https://env.example.test"
snapshot "$fx" >"$TMP/gateway-env.after"
check "gateway env: nothing written" cmp -s "$TMP/gateway-env.before" "$TMP/gateway-env.after"

# Gateway URLs that cannot be used are refused before anything changes, and
# the refusal says what is wrong.
fx="$FIXTURES/gateway-invalid"
mkdir -p "$fx/home"
snapshot "$fx" >"$TMP/gateway-invalid.before"
BAD_URLS=("https://" "https://g.test:bad" "https://g.test:" "https://g.test/?x=1" \
  "https://g.test/#top" "ftp://g.test" "https://[::1" "https://user:pw@g.test")
BAD_REASONS=("has no host name" "has a port that is not a number from 0 to 65535" \
  "has no port number after the colon" "must not have a query (?) or a fragment (#)" \
  "must not have a query (?) or a fragment (#)" "must start with http:// or https://" \
  "is not a valid URL (Invalid IPv6 URL)" "must not contain credentials (user@host)")
for index in "${!BAD_URLS[@]}"; do
  FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-invalid 1 --cli-only \
    --gateway "${BAD_URLS[$index]}"
  check "gateway invalid: ${BAD_URLS[$index]} is refused with the reason" \
    log_says "Gateway URL ${BAD_REASONS[$index]}: ${BAD_URLS[$index]}"
done
snapshot "$fx" >"$TMP/gateway-invalid.after"
check "gateway invalid: nothing changed" \
  cmp -s "$TMP/gateway-invalid.before" "$TMP/gateway-invalid.after"

# The scheme is saved in lower case, the host as given, and every trailing
# slash is removed.
fx="$FIXTURES/gateway-normal"
mkdir -p "$fx/home"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-normal 0 --cli-only \
  --gateway 'HTTPS://Host.Example/'
check "gateway normal: HTTPS://Host.Example/ is saved as https://Host.Example" \
  holds_exactly "$fx/home/.config/enmaas/config.toml" 'base_url = "https://Host.Example"'
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-normal 0 --cli-only \
  --gateway 'https://host/v1//'
check "gateway normal: https://host/v1// is saved as https://host/v1" \
  holds_exactly "$fx/home/.config/enmaas/config.toml" 'base_url = "https://host/v1"'

# A comment at the end of the base_url line stays when the value is replaced.
# The old value holds a # of its own, which is part of the string.
fx="$FIXTURES/gateway-comment"
mkdir -p "$fx/home/.config/enmaas"
printf '%s\n' 'base_url = "https://old.test/#x"  # keep this note' 'concurrency = 2' \
  >"$fx/home/.config/enmaas/config.toml"
printf '%s\n' "$GATEWAY_LINE  # keep this note" 'concurrency = 2' \
  >"$TMP/gateway-comment.expected"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-comment 0 --cli-only \
  --gateway https://gw.example.test/
check "gateway comment: the comment on the base_url line is kept" \
  cmp -s "$fx/home/.config/enmaas/config.toml" "$TMP/gateway-comment.expected"

# A config.toml with CRLF line endings keeps them, when base_url is replaced
# and when it is added.
fx="$FIXTURES/gateway-crlf"
mkdir -p "$fx/home/.config/enmaas"
printf 'default_model = "m"\r\nbase_url = "https://old.test"\r\n\r\n[budget]\r\ntokens = 1\r\n' \
  >"$fx/home/.config/enmaas/config.toml"
printf 'default_model = "m"\r\n%s\r\n\r\n[budget]\r\ntokens = 1\r\n' "$GATEWAY_LINE" \
  >"$TMP/gateway-crlf.expected"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-crlf 0 --cli-only \
  --gateway https://gw.example.test/
check "gateway CRLF: a replaced base_url keeps the CRLF line endings" \
  cmp -s "$fx/home/.config/enmaas/config.toml" "$TMP/gateway-crlf.expected"
printf 'default_model = "m"\r\n\r\n[budget]\r\ntokens = 1\r\n' \
  >"$fx/home/.config/enmaas/config.toml"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-crlf 0 --cli-only \
  --gateway https://gw.example.test/
check "gateway CRLF: an added base_url uses CRLF too" \
  cmp -s "$fx/home/.config/enmaas/config.toml" "$TMP/gateway-crlf.expected"

# A URL the installer would keep is checked as well.
fx="$FIXTURES/gateway-env-bad"
mkdir -p "$fx/home"
ENMAAS_BASE_URL=https:// FX_HOME="$fx/home" FX_PATH="$NOOP_UV" \
  fx_run gateway-env-bad 1 --cli-only
check "gateway env bad: the refusal names \$ENMAAS_BASE_URL and the problem" \
  log_says "\$ENMAAS_BASE_URL has no host name: https://"
check "gateway env bad: the refusal says to fix or unset it" \
  log_says "Fix or unset \$ENMAAS_BASE_URL, then run this again."
fx="$FIXTURES/gateway-config-bad"
mkdir -p "$fx/home/.config/enmaas"
printf '%s\n' 'base_url = "https://"' >"$fx/home/.config/enmaas/config.toml"
snapshot "$fx" >"$TMP/gateway-config-bad.before"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-config-bad 1 --cli-only
check "gateway config bad: the refusal names base_url and the problem" \
  log_says "base_url in ~/.config/enmaas/config.toml has no host name: https://"
check "gateway config bad: the refusal says how to fix it" \
  log_says "Run this again with --gateway URL, or fix base_url in ~/.config/enmaas/config.toml."
snapshot "$fx" >"$TMP/gateway-config-bad.after"
check "gateway config bad: nothing changed" \
  cmp -s "$TMP/gateway-config-bad.before" "$TMP/gateway-config-bad.after"
# A base_url of only spaces counts as missing.
printf '%s\n' 'base_url = "   "' >"$fx/home/.config/enmaas/config.toml"
FX_HOME="$fx/home" FX_PATH="$NOOP_UV" fx_run gateway-config-bad 1 --cli-only
check "gateway config blank: a blank base_url counts as missing" log_says "$NO_GATEWAY"

# Stdin is a terminal but /dev/tty cannot be opened: the command runs in a
# new session with no controlling terminal. A Python process leads that
# session, so the shell under it cannot take the terminal as its own. The
# install stops with the usual message and no shell error.
PTY_RUN='
import os, subprocess
master, slave = os.openpty()
leader = [sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"]
with open(sys.argv[1], "wb") as log:
    status = subprocess.call(leader + sys.argv[2:], stdin=slave, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
raise SystemExit(status)
'
fx="$FIXTURES/gateway-no-tty"
mkdir -p "$fx/home"
# shellcheck disable=SC2016 # the inner shell expands this
if py "$PTY_RUN" "$TMP/no-tty-probe.log" bash -c \
  '[[ -t 0 ]] && ! { exec 3<>/dev/tty; } 2>/dev/null'; then
  FX_LOG="$TMP/fixture-gateway-no-tty.log"
  status=0
  (cd "$REPO" && env CLAUDE_CONFIG_DIR="$fx/claude" CODEX_HOME="$fx/codex" \
    XDG_DATA_HOME="$fx/data" HOME="$fx/home" PATH="$NOOP_UV:$PATH" \
    python3 -B -c "$PRELUDE$PTY_RUN" "$FX_LOG" ./install.sh --cli-only) || status=$?
  check "gateway no tty: install.sh exits 1" test "$status" -eq 1
  check "gateway no tty: the refusal says how to give the URL" log_says "$NO_GATEWAY"
  check "gateway no tty: no shell error about /dev/tty" bash -c '! grep -qF /dev/tty "$1"' _ "$FX_LOG"
else
  echo "SKIP: fixture gateway-no-tty: this system opens /dev/tty without a controlling terminal"
fi

# With a terminal, the install asks once and saves the answer. script(1)
# provides the pseudo-terminal; util-linux and BSD spell it differently.
fx="$FIXTURES/gateway-prompt"
mkdir -p "$fx/home"
SCRIPT_KIND=""
if command -v script >/dev/null 2>&1; then
  if script -qefc true /dev/null </dev/null >/dev/null 2>&1; then
    SCRIPT_KIND=util-linux
  elif script -q /dev/null true </dev/null >/dev/null 2>&1; then
    SCRIPT_KIND=bsd
  fi
fi
if [[ -z "$SCRIPT_KIND" ]]; then
  echo "SKIP: fixture gateway-prompt: no usable script command, so there is no pseudo-terminal to answer the prompt"
else
  FX_LOG="$TMP/fixture-gateway-prompt.log"
  status=0
  if [[ "$SCRIPT_KIND" == util-linux ]]; then
    pty_run=(script -qefc "./install.sh --cli-only" /dev/null)
  else
    pty_run=(script -q /dev/null ./install.sh --cli-only)
  fi
  (cd "$REPO" && printf 'https://prompted.example.test/\n' \
    | env CLAUDE_CONFIG_DIR="$fx/claude" CODEX_HOME="$fx/codex" XDG_DATA_HOME="$fx/data" \
      HOME="$fx/home" PATH="$NOOP_UV:$PATH" "${pty_run[@]}") >"$FX_LOG" 2>&1 || status=$?
  check "gateway prompt: install.sh exits 0" test "$status" -eq 0
  check "gateway prompt: the question was asked" \
    log_says "Gateway URL (your organization gives you this): "
  check "gateway prompt: the answer is saved without the trailing slash" \
    holds_exactly "$fx/home/.config/enmaas/config.toml" 'base_url = "https://prompted.example.test"'
fi

echo
if [[ $FAILS -eq 0 ]]; then
  echo "install test: all checks passed"
else
  echo "install test: $FAILS check(s) failed"
  exit 1
fi
