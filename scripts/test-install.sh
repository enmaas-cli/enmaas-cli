#!/usr/bin/env bash
# Check install.sh end to end for a brand-new user.
#
# Everything happens under a temporary HOME: the CLI install through uv, the
# skill copies, the guard copy and the edits to the Claude Code and Codex
# settings. The registered guard is then run the way a host runs it. Fixtures
# after the main run cover settings files the installer must keep intact or
# refuse to touch. Nothing outside the temporary directory is changed. Run it
# with: make check-install

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
cleanup() { chmod -R u+w "$TMP" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

# --- Isolated environment --------------------------------------------------

# Drop settings that point at the real user's files or at an active virtualenv.
unset CLAUDE_CONFIG_DIR CODEX_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_STATE_HOME \
  PIPX_HOME PIPX_BIN_DIR PIPX_MAN_DIR UV_CACHE_DIR UV_CONFIG_FILE \
  UV_PYTHON_INSTALL_DIR UV_PYTHON_BIN_DIR PYTHONPATH PYTHONHOME
# The checks below are plain Python; keep them from being optimized away.
export PYTHONOPTIMIZE=
VENV_BIN="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin}"
unset VIRTUAL_ENV

UV_BIN="$(command -v uv || true)"
[[ -n "$UV_BIN" ]] || { echo "FAIL: uv is required for this test"; exit 1; }
REAL_PYTHON="$(command -v python3 || true)"
[[ -n "$REAL_PYTHON" ]] || { echo "FAIL: python3 is required for this test"; exit 1; }

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
GUARD="$XDG_DATA_HOME/enmaas/guard"
WORK="$TMP/work"
mkdir -p "$CLAUDE" "$CODEX" "$WORK"

GUARD_ID="enmaas/guard/hooks/pre-tool-use.py"
BEGIN_MARK="# >>> enmaas guard (managed by enmaas-cli install.sh)"
END_MARK="# <<< enmaas guard"

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
  if (cd "$REPO" && ./install.sh "$@") >"$log" 2>&1; then
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

# --- First install ---------------------------------------------------------

install_run first

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

cp "$CLAUDE/settings.json" "$TMP/settings.after-first"
cp "$CODEX/config.toml" "$TMP/config.after-first"

# --- Second install --------------------------------------------------------

install_run second

check "settings.json unchanged by a second install" cmp -s "$CLAUDE/settings.json" \
  "$TMP/settings.after-first"
check "config.toml unchanged by a second install" cmp -s "$CODEX/config.toml" \
  "$TMP/config.after-first"
check "guard copy still has exactly four files" file_count_is "$GUARD" 4
check "no staged or old guard copies left behind" \
  test "$(find "$XDG_DATA_HOME/enmaas" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1

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

# --- Fixtures --------------------------------------------------------------
#
# Each fixture has its own Claude Code dir, Codex dir and data dir under
# $FIXTURES/NAME, and runs with --skill-only, so the CLI is not reinstalled.

FIXTURES="$HOME/fixtures"
FX_RUNS=0
FX_LOG=""

# fx_run NAME EXPECTED_STATUS ARGS...
# Set FX_DATA to override the data dir, FX_PATH to put a directory first on PATH.
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
    PATH="${FX_PATH:+$FX_PATH:}$PATH"
    ./install.sh "$@"
  ) >"$FX_LOG" 2>&1 || status=$?
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

echo
if [[ $FAILS -eq 0 ]]; then
  echo "install test: all checks passed"
else
  echo "install test: $FAILS check(s) failed"
  exit 1
fi
