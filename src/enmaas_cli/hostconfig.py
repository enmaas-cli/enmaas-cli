import contextlib
import copy
import ipaddress
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
    return "python3 -I -S " + shlex.quote(guard_script)


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


def write_text(old, new, done, planned):
    """Write new in place of old. A dry run prints the path and planned instead.

    The file can hold tokens and provider headers that are not ours, so no
    message prints any of its content, in any mode.
    """
    if old == new:
        if mode != "check":
            print(f"  {path}: already up to date")
        return
    check_writable()
    if mode == "check":
        return
    if mode == "1":
        print(f"  {path}: would {planned}")
        return
    real = os.path.realpath(path)
    perms = os.stat(real).st_mode & 0o7777 if old is not None else 0o644
    # The context below closes the handle before cleanup can remove its file.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
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
            fail(
                f"{path} changed while install.sh was running. Nothing was written. Run this again."
            )
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


class DuplicateKey(ValueError):
    """An object names one key twice. The key is not kept, so no message can show it."""


def load_json(text):
    def no_duplicates(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise DuplicateKey()
            seen.add(key)
        return dict(pairs)

    return json.loads(text, object_pairs_hook=no_duplicates)


def json_problem(exc):
    """Say where a JSON file stops parsing, without quoting any of it."""
    if isinstance(exc, json.JSONDecodeError):
        return f" ({exc.msg} at line {exc.lineno} column {exc.colno})"
    if isinstance(exc, DuplicateKey):
        return " (duplicate key)"
    return ""


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
        fail(f"{path} is not valid JSON{json_problem(exc)}. Fix or move it, then run this again.")
    if not isinstance(settings, dict):
        fail(f"{path} does not hold a JSON object. Fix or move it, then run this again.")
    original = copy.deepcopy(settings)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        fail(f'{path}: "hooks" is not an object. Fix it, then run this again.')
    groups = hooks.get("PreToolUse", [])
    if not isinstance(groups, list):
        fail(f'{path}: "hooks.PreToolUse" is not a list. Fix it, then run this again.')

    kept, first = strip_ours(groups)
    if add:
        # Claude Code kills a hook at its timeout and, without onFailure, then
        # runs the tool call anyway. "block" denies the call instead. Versions
        # before 2.1.295 ignore the field. An older entry without it differs
        # from this one, so it is rewritten in place.
        entry = {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command(),
                    "timeout": 5,
                    "onFailure": "block",
                }
            ],
        }
        # Keep the guard where it was if it is already registered.
        kept.insert(len(kept) if first is None else first, entry)
        hooks["PreToolUse"] = kept
        settings["hooks"] = hooks
        done = "registered the enmaas guard"
        planned = ("add" if first is None else "replace") + " the enmaas guard registration"
    else:
        if first is None:
            if mode != "check":
                print(f"  {path}: no enmaas guard registered")
            return
        # The hooks object and the PreToolUse list stay, even when empty.
        hooks["PreToolUse"] = kept
        done = "removed the enmaas guard entry"
        planned = "remove the enmaas guard registration"
    new = old if same(settings, original) else dump_json(settings)
    write_text(old, new, done, planned)


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
        fail(problem.replace("{exc}", toml_position(exc)))


def toml_position(exc):
    """Say where TOML stops parsing. The parser's own message can quote a key."""
    found = re.search(r"\((at line \d+, column \d+|at end of document)\)$", str(exc))
    return found[1] if found else "at an unknown position"


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
    old_doc = parse_toml(
        text, f"{path} is not valid TOML ({{exc}}). Fix or move it, then run this again."
    )
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
                f'Lines below the "{END}" line belong to the guard\'s table. Move them '
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
            f'hooks = [{{ type = "command", command = {toml_string(hook_command())}'
            ", timeout = 5 }]\n"
            f"{END}\n"
        )
        planned = ("replace" if starts else "add") + " the enmaas guard registration"
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
        planned = "remove the enmaas guard registration"
    write_text(old, new, done, planned)


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


def is_loopback(host):
    """True for localhost, 127.0.0.0/8 and ::1. urlsplit has removed any brackets."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
    # Over plain http, the key and every prompt and answer cross the network
    # in clear text. That is only safe for a gateway on this machine.
    if parts.scheme == "http" and not is_loopback(host):
        fail(f"{label} must use https:// for a host other than localhost: {url}")
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
    return parse_toml(
        text, f"{path} is not valid TOML ({{exc}}). Fix or move it, then run this again."
    )


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
            directory = os.path.dirname(os.path.realpath(path))
            print(f"  {path}: would create {directory} and set {name}")
        return
    write_text(old, new, f"set {name}", f"set {name}")


def main(argv):
    global action, path, mode, value, matcher, guard_script
    action, path, mode = argv[1:4]
    if action.startswith("config-"):
        value = argv[4]
        matcher = guard_script = ""
    else:
        matcher, guard_script = argv[4:6]

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
