#!/usr/bin/env bash
# Install the enmaas CLI, its agent skill and its output guard.
#
# The CLI is the tool. The skill tells an agent when to use it. The guard is a
# hook that stops an agent from reading a protected answer file directly.
# Claude Code and Codex use the same skill format and the same hook data, so
# one copy of each serves both. Each part goes to every host found here.
#
# Usage:
#   ./install.sh                  install the CLI, the skill and the guard
#   ./install.sh --cli-only       install just the CLI
#   ./install.sh --skill-only     install just the skill and the guard
#   ./install.sh --no-hook        skip the guard
#   ./install.sh --gateway URL    save URL in ~/.config/enmaas/config.toml
#   ./install.sh --key-file PATH  key file to use (default ~/.secrets/enmaas)
#   ./install.sh --uninstall      remove what this script installs
#   ./install.sh --dry-run        print what would happen and change nothing
#   ./install.sh -h, --help       show this help
#
# Options can be combined. Without --gateway, an install that includes the CLI
# uses $ENMAAS_BASE_URL or the base_url already in config.toml. If neither is
# set, it asks for the URL when run from a terminal and stops otherwise.

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
CONFIG_FILE="$HOME/.config/enmaas/config.toml"
DEFAULT_KEY="$HOME/.secrets/enmaas"

DO_CLI=1
DO_SKILL=1
DO_HOOK=1
DRY_RUN=0
UNINSTALL=0
GATEWAY=""
GATEWAY_GIVEN=0
KEY_FILE=""
KEY_FILE_GIVEN=0

usage_error() { printf '%s\n' "$*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cli-only)   DO_SKILL=0; DO_HOOK=0 ;;
    --skill-only) DO_CLI=0 ;;
    --no-hook)    DO_HOOK=0 ;;
    --dry-run)    DRY_RUN=1 ;;
    --uninstall)  UNINSTALL=1 ;;
    --gateway)
      [[ $# -ge 2 ]] || usage_error "--gateway needs a URL"
      GATEWAY="$2"; GATEWAY_GIVEN=1; shift ;;
    --gateway=*)  GATEWAY="${1#*=}"; GATEWAY_GIVEN=1 ;;
    --key-file)
      [[ $# -ge 2 ]] || usage_error "--key-file needs a path"
      KEY_FILE="$2"; KEY_FILE_GIVEN=1; shift ;;
    --key-file=*) KEY_FILE="${1#*=}"; KEY_FILE_GIVEN=1 ;;
    -h|--help)
      # Print the header comment, from line 2 to the first blank line.
      awk 'NR == 1 { next } /^$/ { exit } { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *)            usage_error "unknown option: $1" ;;
  esac
  shift
done

if [[ $UNINSTALL -eq 1 && ( $GATEWAY_GIVEN -eq 1 || $KEY_FILE_GIVEN -eq 1 ) ]]; then
  usage_error "--gateway and --key-file do not apply to --uninstall."
fi
if [[ $KEY_FILE_GIVEN -eq 1 && -z "$KEY_FILE" ]]; then
  usage_error "--key-file needs a path"
fi

say() { printf '%s\n' "$*"; }
run() { if [[ $DRY_RUN -eq 1 ]]; then say "  would run: $*"; else "$@"; fi; }
die() { printf '%s\n' "$*" >&2; exit 1; }
indent() { printf '%s\n' "$1" | sed 's/^/    /'; }

# A new skill or guard copy is built in STAGE. The old copy at TARGET waits in
# ASIDE until the new one is in place. If the script stops part way, put the
# old copy back. A copy that cannot be put back is kept, and the message says
# where it is. TARGET_NOTE adds a sentence to that message.
STAGE=""
ASIDE=""
TARGET=""
TARGET_NOTE=""
cleanup() {
  local backup
  if [[ -n "$STAGE" ]]; then
    rm -rf "$STAGE"
  fi
  [[ -n "$ASIDE" ]] || return 0
  backup="$ASIDE/$(basename "$TARGET")"
  # The old copy may be a relative symbolic link, which points nowhere while
  # it sits in ASIDE, so test for a link as well.
  if [[ -e "$backup" || -L "$backup" ]]; then
    if [[ -e "$TARGET" || -L "$TARGET" ]] || ! mv "$backup" "$TARGET"; then
      printf '%s\n' "Could not put the previous copy back at $TARGET." \
        "It is kept at $backup. Move it to $TARGET by hand.$TARGET_NOTE" >&2
      return 0
    fi
    printf '%s\n' "Put the previous copy back at $TARGET." >&2
  fi
  rm -rf "$ASIDE"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# --- Host configuration edits ------------------------------------------------
#
# One Python helper does every settings.json and config.toml edit, so the
# install and uninstall paths share the same parsing and the same rules.
# The first three arguments are the action, the target file and the mode.
# Mode 0 writes, 1 prints what it would write, and check only validates, so a
# broken file stops the run before anything has changed. It prints one status
# line.
#
# claude-add, claude-remove, codex-add and codex-remove take a matcher and the
# guard script path. Our hook is any handler whose command contains GUARD_ID.
# Only those handlers are added or removed; other handlers, groups and keys
# stay as they are.
#
# config-set-gateway and config-set-key-file take a value and set base_url or
# key_file at the top of the enmaas config.toml. config-get takes a key name and
# prints its string value, or nothing. Every other line stays as it is.
#
# config-check-url takes a URL and prints it as the CLI should use it. Its
# second argument is a label for the URL in messages, in place of a file.

edit_config() {
  python3 -I -B - "$@" <<'EOF'
import contextlib
import copy
import json
import os
import re
import shlex
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

try:
    import tomllib
except ImportError:
    # Adding the guard checks the Python version first. Changing config.toml
    # without this module is refused below.
    tomllib = None

action, path, mode = sys.argv[1:4]
if action.startswith("config-"):
    value = sys.argv[4]
    matcher = guard_script = ""
else:
    matcher, guard_script = sys.argv[4:6]
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
        # Something else may have saved the file since it was read. A save
        # between this check and os.replace can still be lost; closing that
        # gap needs a lock that every other writer would honour.
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
        text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError:
        fail(
            f"{path} holds NaN or Infinity, which standard JSON does not allow. "
            "Fix it, then run this again."
        )
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        fail(
            f"{path} holds an unpaired surrogate escape (a \\ud800 to \\udfff code "
            "with no partner), which cannot be saved as UTF-8. Fix it, then run this again."
        )
    return text


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


def is_exactly_ours(entry):
    """True if entry is one PreToolUse table: a matcher and our one handler."""
    return (
        isinstance(entry, dict)
        and set(entry) <= {"matcher", "hooks"}
        and isinstance(entry.get("matcher"), str)
        and isinstance(entry.get("hooks"), list)
        and len(entry["hooks"]) == 1
        and is_ours(entry["hooks"][0])
        and set(entry["hooks"][0]) <= {"type", "command", "timeout"}
        and entry["hooks"][0].get("type", "command") == "command"
    )


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
    # Without markers, a file that never names the guard holds no registration.
    if not starts and not add and GUARD_ID not in text:
        if mode != "check":
            print(f"  {path}: no enmaas guard registered")
        return
    if tomllib is None:
        fail(
            f"Changing {path} safely needs Python 3.11 or newer. Put a newer python3 "
            "first on PATH, or remove the block between the enmaas guard markers by hand."
        )
    old_doc = parse_toml(text, f"{path} is not valid TOML ({{exc}}). Fix or move it, then run this again.")
    unmarked = (
        f"{path}: the enmaas guard is registered in config.toml without the enmaas "
        "markers. Remove that [[hooks.PreToolUse]] table by hand, then run this again."
    )

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
        if not any(holds_ours(entry) for entry in entries):
            fail(
                f"{path}: the text between the enmaas guard markers is not the block "
                "this installer writes. Remove the block by hand, then run this again."
            )
        # The block is ours. Rewriting or removing it would drop anything else
        # added inside it, so refuse instead.
        if (
            list(block_doc) != ["hooks"]
            or list(block_doc["hooks"]) != ["PreToolUse"]
            or len(entries) != 1
            or not is_exactly_ours(entries[0])
        ):
            fail(
                f"{path}: the enmaas guard block was edited by hand. Restore it to the two "
                "marker lines with only the guard's [[hooks.PreToolUse]] table between them, "
                "or remove the block and its marker lines. Put any other hook in its own "
                "table outside the markers, then run this again."
            )
        rest = head + tail
        rest_doc = parse_toml(
            rest,
            f"{path}: removing the enmaas guard block would leave invalid TOML ({{exc}}). "
            "Remove the block by hand, then run this again.",
        )
        if any(holds_ours(entry) for entry in pre_tool_use(rest_doc)):
            fail(unmarked)
        if not is_rest_plus(old_doc, entries[0], rest_doc):
            fail(
                f"{path}: removing the enmaas guard block would also change other settings. "
                f"Lines below the \"{END}\" line belong to the guard's table. Move them "
                "above the block or under their own table header, then run this again."
            )
    else:
        if any(holds_ours(entry) for entry in pre_tool_use(old_doc)):
            fail(unmarked)
        if not add:
            if mode != "check":
                print(f"  {path}: no enmaas guard registered")
            return
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


# --- enmaas config.toml ---

SETTINGS = {"config-set-gateway": "base_url", "config-set-key-file": "key_file"}
HEADER = re.compile(r"^[ \t]*\[")


def setting_line(name):
    quoted = re.escape(name)
    return re.compile(rf"""^[ \t]*(?:{quoted}|"{quoted}"|'{quoted}')[ \t]*=""")


def trailing_comment(line):
    """The comment that ends a key = value line, with the spaces before it."""
    body = line.rstrip("\r\n")
    for at, char in enumerate(body):
        if char != "#":
            continue
        # A # inside a string is part of the value. The first # after which
        # the line before it parses on its own starts the comment.
        try:
            tomllib.loads(body[:at])
        except tomllib.TOMLDecodeError:
            continue
        return body[len(body[:at].rstrip(" \t")) :]
    return ""


def check_url(label, url):
    """Return url with a lower-case scheme and no trailing slashes, or stop."""
    url = url.strip()
    if not url:
        fail(f"{label} is empty.")
    if any(char.isspace() for char in url):
        fail(f"{label} must not contain spaces: {url}")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        fail(f"{label} is not a valid URL ({exc}): {url}")
    try:
        port = parts.port
    except ValueError:
        fail(f"{label} has a port that is not a number from 0 to 65535: {url}")
    if parts.scheme not in ("http", "https") or not url.lower().startswith(parts.scheme + "://"):
        fail(f"{label} must start with http:// or https://: {url}")
    if not host:
        fail(f"{label} has no host name: {url}")
    # The key travels in a header. A user:password in the URL would be a
    # second secret that lands in logs, and the CLI refuses it too.
    if "@" in parts.netloc:
        fail(f"{label} must not contain credentials (user@host): {url}")
    if port is None and parts.netloc.endswith(":"):
        fail(f"{label} has no port number after the colon: {url}")
    if "?" in url or "#" in url:
        fail(f"{label} must not have a query (?) or a fragment (#): {url}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def is_blank(line):
    return line.strip() == ""


def is_comment(line):
    return line.lstrip().startswith("#")


def load_settings(text):
    if tomllib is None:
        fail(
            f"Reading {path} safely needs Python 3.11 or newer. Put a newer python3 "
            "first on PATH, or edit the file by hand."
        )
    return parse_toml(text, f"{path} is not valid TOML ({{exc}}). Fix or move it, then run this again.")


def get_setting(name):
    text = read_text()
    if text is None:
        return
    found = load_settings(text).get(name)
    if isinstance(found, str):
        print(found)


def ensure_directory():
    """Make sure the directory for path exists. Returns False if it does not yet."""
    directory = os.path.dirname(os.path.realpath(path))
    if os.path.isdir(directory):
        return True
    near = directory
    while not os.path.lexists(near):
        near = os.path.dirname(near)
    if not os.path.isdir(near) or not os.access(near, os.W_OK):
        fail(
            f"Cannot create {directory}, which holds {path}. "
            f"Fix the permissions of {near}, then run this again."
        )
    if mode != "0":
        return False
    os.makedirs(directory, exist_ok=True)
    return True


def set_setting(name):
    old = read_text()
    text = old or ""
    old_doc = load_settings(text)
    line_end = "\r\n" if "\r\n" in text else "\n"
    entry = f"{name} = {toml_string(value)}"
    lines = text.splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines) if HEADER.match(line)), len(lines))
    pattern = setting_line(name)
    found = [i for i in range(header) if pattern.match(lines[i])]
    if found:
        # Replace that one line and keep its comment and its line ending.
        old_line = lines[found[0]]
        ending = old_line[len(old_line.rstrip("\r\n")) :]
        lines[found[0]] = entry + trailing_comment(old_line) + ending
    elif header < len(lines):
        # Go above the comments and blank lines that lead into the first table.
        at = header
        while at > 0 and is_comment(lines[at - 1]):
            at -= 1
        while at > 0 and is_blank(lines[at - 1]):
            at -= 1
        added = [entry + line_end]
        if not is_blank(lines[at]):
            added.append(line_end)
        lines[at:at] = added
    else:
        if text and not text.endswith("\n"):
            lines.append(line_end)
        lines.append(entry + line_end)
    new = "".join(lines)
    new_doc = parse_toml(
        new,
        f"{path}: setting {name} would make the file invalid ({{exc}}). "
        f"Set {name} by hand, then run this again.",
    )
    expected = copy.deepcopy(old_doc)
    expected[name] = value
    if not same(new_doc, expected):
        fail(
            f"{path}: setting {name} would also change other settings. "
            f"Set {name} by hand, then run this again."
        )
    if old != new and not ensure_directory():
        if mode == "1":
            print(f"  would create {os.path.dirname(os.path.realpath(path))} and write {path}:")
            for line in new.splitlines():
                print(f"    {line}")
        return
    write_text(old, new, f"set {name}")


if action == "claude-add":
    claude(True)
elif action == "claude-remove":
    claude(False)
elif action == "codex-add":
    codex(True)
elif action == "codex-remove":
    codex(False)
elif action in SETTINGS:
    set_setting(SETTINGS[action])
elif action == "config-get":
    get_setting(value)
elif action == "config-check-url":
    print(check_url(path, value))
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

# --- Gateway URL and key file ----------------------------------------------

GATEWAY_PROMPT="Gateway URL (your organization gives you this): "
NO_GATEWAY="No gateway URL. Run this again with --gateway URL, or set base_url in ~/.config/enmaas/config.toml."

trim() {
  local text="$1"
  text="${text#"${text%%[![:space:]]*}"}"
  text="${text%"${text##*[![:space:]]}"}"
  printf '%s\n' "$text"
}

# Print $2 as a gateway URL with a lower-case scheme and no trailing slashes.
# Refuse anything that is not an http or https URL with a host name, and no
# query or fragment. $1 names the value in the message.
clean_gateway() {
  edit_config config-check-url "$1" check "$(trim "$2")"
}

# Ask for the gateway URL on the terminal and print the answer. Return 1 if
# the terminal cannot be opened or read.
ask_gateway() {
  local answer=""
  { exec 3<>/dev/tty; } 2>/dev/null || return 1
  printf '%s' "$GATEWAY_PROMPT" >&3
  IFS= read -r answer <&3 || [[ -n "$answer" ]] || return 1
  exec 3>&-
  printf '%s\n' "$answer"
}

# Print $1 as an absolute path, with a leading ~ expanded.
# shellcheck disable=SC2088 # matches a literal ~ the shell did not expand
absolute_path() {
  local file="$1"
  case "$file" in
    "~") file="$HOME" ;;
    "~/"*) file="$HOME/${file#"~/"}" ;;
  esac
  [[ "$file" == /* ]] || file="$PWD/$file"
  printf '%s\n' "$file"
}

# Print $1 with $HOME shortened to ~, for messages.
# shellcheck disable=SC2088 # the ~ is printed as is
shown_path() {
  case "$1" in
    "$HOME"/*) printf '~/%s\n' "${1#"$HOME"/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

need_config_python() {
  command -v python3 >/dev/null 2>&1 \
    || die "python3 is not on PATH. It is needed to read and edit $CONFIG_FILE.
Install Python 3.11 or newer, or set base_url in that file by hand."
}

# Read base_url and key_file from config.toml into CONFIG_URL and
# CONFIG_KEY_FILE. A file that cannot be read stops the run when the CLI or a
# config option is part of it. Otherwise it only means no key_file is known.
read_config() {
  CONFIG_URL=""
  CONFIG_KEY_FILE=""
  [[ -e "$CONFIG_FILE" || -L "$CONFIG_FILE" ]] || return 0
  if [[ $DO_CLI -eq 1 || $GATEWAY_GIVEN -eq 1 || $KEY_FILE_GIVEN -eq 1 ]]; then
    need_config_python
    CONFIG_URL="$(edit_config config-get "$CONFIG_FILE" check base_url)" || exit 1
    CONFIG_KEY_FILE="$(edit_config config-get "$CONFIG_FILE" check key_file)" || exit 1
  elif command -v python3 >/dev/null 2>&1; then
    CONFIG_KEY_FILE="$(edit_config config-get "$CONFIG_FILE" check key_file 2>/dev/null)" \
      || CONFIG_KEY_FILE=""
  fi
}

# Print how to create the key file $1.
key_file_steps() {
  local file="$1" dir
  if [[ "$file" == "$DEFAULT_KEY" ]]; then
    say "  mkdir -p -m 700 ~/.secrets && [ -f ~/.secrets/enmaas ] || install -m 600 /dev/null ~/.secrets/enmaas"
  else
    dir="$(dirname "$file")"
    say "  mkdir -p -m 700 $(printf '%q' "$dir") && [ -f $(printf '%q' "$file") ] || install -m 600 /dev/null $(printf '%q' "$file")"
  fi
  say "  # paste the key into that file as a single line, no quotes"
}

# Report on the key file $1: whether it exists, holds one line and is private.
report_key_file() {
  local file="$1" shown lines perms
  shown="$(shown_path "$file")"
  if [[ ! -e "$file" ]]; then
    say "No key file at $shown."
    say "Your organization gives you the gateway URL and an API key."
    say "To create the key file:"
    key_file_steps "$file"
    return 0
  fi
  if [[ ! -f "$file" ]]; then
    say "Warning: $shown is not a regular file, so the CLI cannot read a key from it."
    return 0
  fi
  if [[ ! -r "$file" ]]; then
    say "Warning: cannot read $shown. Fix its permissions so you can read it."
    return 0
  fi
  lines="$(grep -c '[^[:space:]]' "$file" || true)"
  if [[ "$lines" == 0 ]]; then
    say "The key file $shown is empty."
    say "Your organization gives you the gateway URL and an API key."
    say "Paste the key into that file as a single line, no quotes."
  elif [[ "$lines" != 1 ]]; then
    say "Warning: $shown holds $lines lines. It must hold only the key, on a single line."
  else
    say "A key is already present at $shown."
  fi
  perms="$(stat -L -c %a "$file" 2>/dev/null || stat -L -f %Lp "$file" 2>/dev/null || true)"
  if [[ "$perms" =~ ^[0-7]+$ ]] && (( 8#$perms & 8#177 )); then
    say "Warning: $shown is mode $perms, which is wider than 600. Run:"
    say "  chmod 600 $(printf '%q' "$file")"
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
# CLI_CONFIRMED is set once a tool shows the CLI removed or absent.
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
    CLI_UNCHECKED="${CLI_UNCHECKED:+$CLI_UNCHECKED and }$tool"
    return 0
  fi
  if ! lists_enmaas "$out"; then
    say "  $tool: enmaas-cli not installed"
    CLI_CONFIRMED=1
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
  CLI_CONFIRMED=1
}

remove_cli() {
  local failed=0
  CLI_REMOVED=0
  CLI_CONFIRMED=0
  CLI_UNCHECKED=""
  if command -v uv >/dev/null 2>&1; then
    remove_cli_with uv uv tool list || failed=1
  fi
  if command -v pipx >/dev/null 2>&1; then
    remove_cli_with pipx pipx list --short || failed=1
  fi
  if [[ $failed -eq 1 ]]; then
    die "enmaas-cli could not be removed. Fix the problem shown above, then run this again."
  fi
  if [[ $DRY_RUN -eq 0 && -n "$CLI_UNCHECKED" ]]; then
    if [[ $CLI_CONFIRMED -eq 0 ]]; then
      die "Could not confirm that enmaas-cli is removed, because $CLI_UNCHECKED could not list its installs.
Fix the problem shown above, then run this again."
    fi
    say "  note: $CLI_UNCHECKED could not be checked, so enmaas-cli may still be installed through it."
  fi
  if [[ $DRY_RUN -eq 0 && $CLI_REMOVED -eq 0 && -z "$CLI_UNCHECKED" ]]; then
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
  say "Your settings in ~/.config/enmaas/config.toml are left in place."
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
fi

# The gateway URL comes from --gateway, $ENMAAS_BASE_URL, base_url in
# config.toml, or a prompt, in that order. Only an install that includes the
# CLI needs one; an explicit --gateway is saved whatever else is installed.
# A URL the installer keeps is checked like one it writes. A base_url that is
# blank counts as missing.
read_config
ENV_GATEWAY="$(trim "${ENMAAS_BASE_URL:-}")"
CONFIG_URL="$(trim "$CONFIG_URL")"
CONFIG_SHOWN="$(shown_path "$CONFIG_FILE")"
GATEWAY_NOTE=""
WRITE_GATEWAY=0
ASK_GATEWAY=0
if [[ $GATEWAY_GIVEN -eq 1 || $DO_CLI -eq 1 ]]; then
  command -v python3 >/dev/null 2>&1 \
    || die "python3 is not on PATH. It is needed to check the gateway URL.
Install Python 3.11 or newer."
fi
if [[ $GATEWAY_GIVEN -eq 1 ]]; then
  GATEWAY="$(clean_gateway "Gateway URL" "$GATEWAY")" || exit 1
  WRITE_GATEWAY=1
elif [[ $DO_CLI -eq 1 ]]; then
  if [[ -n "$ENV_GATEWAY" ]]; then
    clean_gateway "\$ENMAAS_BASE_URL" "$ENV_GATEWAY" >/dev/null \
      || die "Fix or unset \$ENMAAS_BASE_URL, then run this again."
    GATEWAY_NOTE="  gateway URL: \$ENMAAS_BASE_URL is set, so the CLI will use $ENV_GATEWAY. Nothing is written to config.toml."
  elif [[ -n "$CONFIG_URL" ]]; then
    if clean_gateway "base_url in $CONFIG_SHOWN" "$CONFIG_URL" >/dev/null; then
      GATEWAY_NOTE="  gateway URL: the CLI will use base_url from $CONFIG_SHOWN: $CONFIG_URL"
    elif [[ $DRY_RUN -eq 0 && ! -t 0 ]]; then
      die "Run this again with --gateway URL, or fix base_url in $CONFIG_SHOWN."
    else
      if [[ $DRY_RUN -eq 0 ]]; then
        say "Warning: the base_url in $CONFIG_SHOWN cannot be used. Enter a gateway URL to replace it."
      fi
      ASK_GATEWAY=1
    fi
  else
    ASK_GATEWAY=1
  fi
fi
if [[ $ASK_GATEWAY -eq 1 ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    if [[ -n "$CONFIG_URL" ]]; then
      GATEWAY_NOTE="  gateway URL: the base_url in $CONFIG_SHOWN cannot be used."
    else
      GATEWAY_NOTE="  gateway URL: none is set."
    fi
    GATEWAY_NOTE="$GATEWAY_NOTE A real run would ask \"$GATEWAY_PROMPT\"
  and write the answer as base_url in $CONFIG_SHOWN."
  elif [[ -t 0 ]]; then
    answer="$(ask_gateway)" || die "$NO_GATEWAY"
    [[ -n "$(trim "$answer")" ]] || die "$NO_GATEWAY"
    GATEWAY="$(clean_gateway "Gateway URL" "$answer")" || exit 1
    WRITE_GATEWAY=1
  else
    die "$NO_GATEWAY"
  fi
fi
if [[ $WRITE_GATEWAY -eq 1 ]]; then
  need_config_python
  edit_config config-set-gateway "$CONFIG_FILE" check "$GATEWAY"
fi

# The key file the CLI will read, and whether config.toml must name it.
WRITE_KEY_FILE=0
if [[ $KEY_FILE_GIVEN -eq 1 ]]; then
  KEY_FILE="$(absolute_path "$KEY_FILE")"
  if [[ -e "$KEY_FILE" && ! -f "$KEY_FILE" ]]; then
    die "$KEY_FILE is not a regular file. Pass the path of the file that holds the key."
  fi
  if [[ "$KEY_FILE" != "$DEFAULT_KEY" || -n "$CONFIG_KEY_FILE" ]]; then
    WRITE_KEY_FILE=1
    need_config_python
    edit_config config-set-key-file "$CONFIG_FILE" check "$KEY_FILE"
  fi
elif [[ -n "$CONFIG_KEY_FILE" ]]; then
  KEY_FILE="$(absolute_path "$CONFIG_KEY_FILE")"
else
  KEY_FILE="$DEFAULT_KEY"
fi

if [[ $DO_HOOK -eq 1 && $DRY_RUN -eq 0 ]]; then
  mkdir -p "$GUARD_PARENT" || die "Cannot create $GUARD_PARENT."
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

# --- Settings ----------------------------------------------------------------

if [[ $WRITE_GATEWAY -eq 1 || $WRITE_KEY_FILE -eq 1 || -n "$GATEWAY_NOTE" ]]; then
  say "Configuring the enmaas CLI"
  if [[ -n "$GATEWAY_NOTE" ]]; then
    say "$GATEWAY_NOTE"
  fi
  if [[ $WRITE_GATEWAY -eq 1 ]]; then
    say "  gateway URL: $GATEWAY"
    edit_config config-set-gateway "$CONFIG_FILE" "$DRY_RUN" "$GATEWAY"
    if [[ -n "$ENV_GATEWAY" && "$ENV_GATEWAY" != "$GATEWAY" ]]; then
      say "  note: \$ENMAAS_BASE_URL is set to $ENV_GATEWAY. The CLI uses it before config.toml."
    fi
  fi
  if [[ $WRITE_KEY_FILE -eq 1 ]]; then
    say "  key file: $KEY_FILE"
    edit_config config-set-key-file "$CONFIG_FILE" "$DRY_RUN" "$KEY_FILE"
  fi
fi

# --- Skill -------------------------------------------------------------------

# Put the copy staged in STAGE in place at $1. The old copy, if any, moves into
# ASIDE first and is deleted only once the new copy is in place. $2 is
# TARGET_NOTE for the message shown if the old copy cannot be put back.
swap_in() {
  local target="$1" aside
  TARGET="$target"
  TARGET_NOTE="$2"
  if [[ -e "$target" || -L "$target" ]]; then
    ASIDE="$(mktemp -d "$(dirname "$target")/.$(basename "$target")-old.XXXXXX")"
    mv "$target" "$ASIDE/$(basename "$target")"
  fi
  mv "$STAGE" "$target"
  STAGE=""
  aside="$ASIDE"
  ASIDE=""
  if [[ -n "$aside" ]]; then
    rm -rf "$aside"
  fi
}

# Copy the skill beside the old one, then swap the two, so a copy that fails
# part way never replaces a working skill.
install_skill() {
  local agent="$1" root="$2"
  local parent="$root/skills"
  local dest="$parent/enmaas"

  if [[ ! -d "$root" ]]; then
    say "  $agent: not installed here, skipping"
    return
  fi

  if [[ -e "$dest" ]] && ! diff -rq "$SKILL_SRC" "$dest" >/dev/null 2>&1; then
    say "  $agent: replacing an existing and different skill at $dest"
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    say "  would copy the skill into a new directory under $parent,"
    say "  then swap it in at $dest"
    return
  fi
  mkdir -p "$parent"
  STAGE="$(mktemp -d "$parent/.enmaas-new.XXXXXX")"
  chmod "$(umask -S)" "$STAGE"
  cp -R "$SKILL_SRC/." "$STAGE/" \
    || die "Could not copy the skill into $parent. The installed skill at $dest, if any, is unchanged."
  swap_in "$dest" ""
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
  swap_in "$GUARD_DIR" " The hosts run the guard from $GUARD_SCRIPT, so it cannot run until then."
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
if [[ -n "${ENMAAS_KEY:-}" ]]; then
  say "\$ENMAAS_KEY is set. The CLI uses it before any key file."
fi
report_key_file "$KEY_FILE"
say ""
say "Check it works:  enmaas models"
