"""Host settings edits preserve complete files and refuse unsafe rewrites."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import enmaas_cli.hostconfig as hostconfig

GUARD = "/tmp/enmaas/guard/hooks/pre-tool-use.py"
MATCHER = "Read|Bash"
BEGIN = "# >>> enmaas guard (managed by enmaas-cli install.sh)"
END = "# <<< enmaas guard"
BLOCK = (
    f"{BEGIN}\n"
    "[[hooks.PreToolUse]]\n"
    'matcher = "Read|Bash"\n'
    'hooks = [{ type = "command", command = "python3 -I -S '
    '/tmp/enmaas/guard/hooks/pre-tool-use.py", timeout = 5 }]\n'
    f"{END}\n"
)
ENTRY = {
    "matcher": MATCHER,
    "hooks": [
        {
            "type": "command",
            "command": f"python3 -I -S {GUARD}",
            "timeout": 5,
            "onFailure": "block",
        }
    ],
}


def invoke(action: str, target: Path, mode: str = "0", value: str = "") -> None:
    args = [value] if action.startswith("config-") else [MATCHER, GUARD]
    hostconfig.main(["install_helper.py", action, str(target), mode, *args])


def json_text(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def unchanged(target: Path, before: bytes, metadata: os.stat_result) -> None:
    assert target.read_bytes() == before
    assert target.stat().st_ino == metadata.st_ino
    assert target.stat().st_mtime_ns == metadata.st_mtime_ns
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(metadata.st_mode)
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_add_repeat_remove_preserves_the_complete_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    target = tmp_path / "settings"
    if host == "claude":
        seed = '{"model":"keep","hooks":{"SessionStart":[],"PreToolUse":[]}}'
        installed = json_text(
            {"model": "keep", "hooks": {"SessionStart": [], "PreToolUse": [ENTRY]}}
        )
        removed = json_text({"model": "keep", "hooks": {"SessionStart": [], "PreToolUse": []}})
    else:
        seed = '# keep this comment\nmodel = "keep"\n\n[tui]\nnotifications = true\n'
        installed = seed + BLOCK
        removed = seed
    target.write_text(seed)
    target.chmod(0o640)
    invoke(f"{host}-add", target)
    assert target.read_text() == installed
    assert capsys.readouterr().out == f"  {target}: registered the enmaas guard\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    before = target.stat()
    invoke(f"{host}-add", target)
    unchanged(target, installed.encode(), before)
    assert capsys.readouterr().out == f"  {target}: already up to date\n"
    invoke(f"{host}-remove", target)
    assert target.read_text() == removed
    capsys.readouterr()
    before = target.stat()
    invoke(f"{host}-remove", target)
    unchanged(target, removed.encode(), before)
    assert capsys.readouterr().out == f"  {target}: no enmaas guard registered\n"


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["0", "1", "check"])
def test_absent_removal_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str, mode: str
) -> None:
    target = tmp_path / "settings"
    invoke(f"{host}-remove", target, mode)
    assert not target.exists()
    expected = "" if mode == "check" else f"  {target}: not present, nothing to remove\n"
    assert capsys.readouterr().out == expected
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["1", "check"])
def test_dry_run_and_check_leave_host_files_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str, mode: str
) -> None:
    target = tmp_path / "settings"
    for present in (False, True):
        for operation in ("add", "remove"):
            if present:
                target.write_text("{}" if host == "claude" else "")
                invoke(f"{host}-add", target)
            before = target.read_bytes() if target.exists() else None
            metadata = target.stat() if target.exists() else None
            capsys.readouterr()
            invoke(f"{host}-{operation}", target, mode)
            if before is None:
                assert not target.exists()
            else:
                assert metadata is not None
                unchanged(target, before, metadata)
            output = capsys.readouterr()
            assert output.err == ""
            if mode == "check":
                assert output.out == ""
            elif not present and operation == "add":
                assert output.out == f"  {target}: would add the enmaas guard registration\n"
            if target.exists():
                target.unlink()


def test_claude_replaces_its_entries_without_removing_shared_handlers(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    other = {"type": "command", "command": "echo other"}
    old = {"command": f"python3 -S {GUARD}", "timeout": 3}
    before: list[Any] = ["opaque", {"matcher": "Read", "hooks": [old, other]}]
    before += [{"matcher": "Bash", "hooks": [old]}]
    target.write_text(json_text({"hooks": {"PreToolUse": before}, "note": "keep"}))
    invoke("claude-add", target)
    assert target.read_text() == json_text(
        {
            "hooks": {"PreToolUse": ["opaque", ENTRY, {"matcher": "Read", "hooks": [other]}]},
            "note": "keep",
        }
    )
    invoke("claude-remove", target)
    assert target.read_text() == json_text(
        {"hooks": {"PreToolUse": ["opaque", {"matcher": "Read", "hooks": [other]}]}, "note": "keep"}
    )


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_writes_through_a_symlink_and_keeps_the_link(tmp_path: Path, host: str) -> None:
    actual = tmp_path / "actual"
    actual.write_text("{}" if host == "claude" else "")
    target = tmp_path / "settings"
    target.symlink_to(actual.name)
    invoke(f"{host}-add", target)
    expected = json_text({"hooks": {"PreToolUse": [ENTRY]}}) if host == "claude" else BLOCK
    assert actual.read_text() == expected
    assert target.is_symlink()
    assert os.readlink(target) == actual.name


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        ('{"hidden": 1, "hidden": 2}', "duplicate key"),
        ('{"hooks":{"x":1,"x":2}}', "duplicate key"),
        ('{"hidden":', "not valid JSON"),
        ('["hidden"]', "does not hold a JSON object"),
        ('{"hooks":[]}', '"hooks" is not an object'),
        ('{"hooks":{"PreToolUse":{}}}', '"hooks.PreToolUse" is not a list'),
        ('{"hidden":NaN}', "holds NaN or Infinity"),
        ('{"hidden":Infinity}', "holds NaN or Infinity"),
        ('{"hidden":"\\ud800"}', "holds an unpaired surrogate escape"),
    ],
)
@pytest.mark.parametrize("mode", ["0", "1", "check"])
def test_json_refusals_preserve_the_complete_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], seed: str, message: str, mode: str
) -> None:
    target = tmp_path / "settings.json"
    target.write_text(seed)
    before = target.stat()
    with pytest.raises(SystemExit) as exc:
        invoke("claude-add", target, mode)
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert message in output.err
    assert "hidden" not in output.err
    unchanged(target, seed.encode(), before)


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        ('hidden = "unfinished', "is not valid TOML"),
        ("hidden = 1\nhidden = 2\n", "is not valid TOML"),
        (BEGIN + "\n", "markers are broken"),
        (END + "\n", "markers are broken"),
        (END + "\n" + BEGIN + "\n", "markers are broken"),
        (BLOCK + BLOCK, "markers are broken"),
        ('note = """\n' + BLOCK + '"""\n', "appear inside other content"),
        (BLOCK.replace(BEGIN + "\n", "").replace(END + "\n", ""), "without the enmaas markers"),
        (BLOCK + '\nmodel = "keep"\n', "would also change other settings"),
        (BLOCK.replace("timeout = 5", "timeout = 5, extra = 1"), "was edited by hand"),
        (
            BLOCK.replace("[[hooks.PreToolUse]]", 'other = "keep"\n[[hooks.PreToolUse]]'),
            "was edited by hand",
        ),
        (BLOCK.replace(END, "[hooks.other]\nx = 1\n" + END), "was edited by hand"),
        (
            BLOCK.replace(END, '[[hooks.PreToolUse]]\nmatcher = "Other"\n' + END),
            "was edited by hand",
        ),
        (BLOCK.replace('matcher = "Read|Bash"', "matcher = 1"), "was edited by hand"),
        (BLOCK.replace('type = "command"', 'type = "other"'), "was edited by hand"),
        (
            BLOCK.replace("timeout = 5 }]", 'timeout = 5 }, { command = "other" }]'),
            "was edited by hand",
        ),
        (BLOCK.replace(END, 'other = "keep"\n' + END), "was edited by hand"),
        ("hooks.PreToolUse = []\n", "adding the guard would make the file invalid"),
    ],
)
def test_codex_refusals_preserve_the_complete_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], seed: str, message: str
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(seed)
    before = target.stat()
    with pytest.raises(SystemExit) as exc:
        invoke("codex-add", target)
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert message in output.err
    assert "hidden" not in output.err
    unchanged(target, seed.encode(), before)


@pytest.mark.parametrize("mode", ["0", "1", "check"])
def test_unmarked_guard_is_also_refused_on_removal(tmp_path: Path, mode: str) -> None:
    target = tmp_path / "config.toml"
    seed = BLOCK.replace(BEGIN + "\n", "").replace(END + "\n", "")
    target.write_text(seed)
    before = target.stat()
    with pytest.raises(SystemExit):
        invoke("codex-remove", target, mode)
    unchanged(target, seed.encode(), before)


@pytest.mark.parametrize("seed", ['# keep\nmodel = "x"', '# keep\nmodel = "x"\n'])
def test_codex_preserves_the_tail_and_unrelated_hooks(tmp_path: Path, seed: str) -> None:
    target = tmp_path / "config.toml"
    other = '[[hooks.PreToolUse]]\nmatcher = "Other"\nhooks = [{command = "other"}]\n'
    seed += "\n" + other
    target.write_text(seed)
    invoke("codex-add", target)
    assert target.read_text() == seed + BLOCK
    tail = "\n[tui]\nnotifications = true\n"
    target.write_text(seed + BLOCK + tail)
    invoke("codex-add", target)
    assert target.read_text() == seed + BLOCK + tail
    invoke("codex-remove", target)
    assert target.read_text() == seed + tail


@pytest.mark.parametrize("seed", ["", 'model = "x"', "# enmaas/guard/hooks/pre-tool-use.py\n"])
def test_codex_adds_to_empty_or_unterminated_text(tmp_path: Path, seed: str) -> None:
    target = tmp_path / "config.toml"
    target.write_text(seed)
    invoke("codex-add", target)
    separator = "\n" if seed and not seed.endswith("\n") else ""
    assert target.read_text() == seed + separator + BLOCK


@pytest.mark.parametrize("mode", ["0", "check"])
def test_codex_comment_naming_guard_is_not_a_registration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    target = tmp_path / "config.toml"
    seed = '# enmaas/guard/hooks/pre-tool-use.py\n[hooks]\nother = "keep"\n'
    target.write_text(seed)
    before = target.stat()
    invoke("codex-remove", target, mode)
    unchanged(target, seed.encode(), before)
    assert capsys.readouterr().out == (
        "" if mode == "check" else f"  {target}: no enmaas guard registered\n"
    )


@pytest.mark.parametrize(
    ("action", "name", "value"),
    [
        ("config-set-gateway", "base_url", "https://gateway.test"),
        ("config-set-key-file", "key_file", '/tmp/key "name"\x7f'),
    ],
)
@pytest.mark.parametrize(
    ("seed", "expected"),
    [
        ("", "{entry}\n"),
        ('note = "keep"', 'note = "keep"\n{entry}\n'),
        ('note = "keep"\n', 'note = "keep"\n{entry}\n'),
        ('{name} = "old#value"  # keep\n', "{entry}  # keep\n"),
        ("'{name}' = 'old'", "{entry}"),
        ('"{name}" = "old"\r\n', "{entry}\r\n"),
        ("# table\n[tui]\nx = 1\n", "{entry}\n\n# table\n[tui]\nx = 1\n"),
        (
            'note = "keep"\n\n# table\n[tui]\nx = 1\n',
            'note = "keep"\n{entry}\n\n# table\n[tui]\nx = 1\n',
        ),
        (
            'note = "keep"\r\n\r\n[tui]\r\nx = 1\r\n',
            'note = "keep"\r\n{entry}\r\n\r\n[tui]\r\nx = 1\r\n',
        ),
        ("note = nan\n[tui]\nx = [1, 2]\n", "note = nan\n{entry}\n\n[tui]\nx = [1, 2]\n"),
    ],
)
def test_each_config_setting_preserves_all_other_bytes_and_repeats_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    action: str,
    name: str,
    value: str,
    seed: str,
    expected: str,
) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(seed.format(name=name).encode())
    entry = name + " = " + json.dumps(value).replace("\x7f", "\\u007f")
    wanted = expected.format(entry=entry).encode()
    invoke(action, target, value=value)
    assert target.read_bytes() == wanted
    assert capsys.readouterr().out == f"  {target}: set {name}\n"
    before = target.stat()
    invoke(action, target, value=value)
    unchanged(target, wanted, before)
    assert capsys.readouterr().out == f"  {target}: already up to date\n"
    invoke("config-get", target, value=name)
    assert capsys.readouterr().out == value + "\n"


@pytest.mark.parametrize("mode", ["0", "1", "check"])
def test_config_parent_creation_obeys_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    target = tmp_path / "new" / "enmaas" / "config.toml"
    invoke("config-set-gateway", target, mode, "https://gateway.test")
    output = capsys.readouterr()
    if mode == "0":
        assert target.read_text() == 'base_url = "https://gateway.test"\n'
        assert output.out == f"  {target}: set base_url\n"
    else:
        assert not target.parent.parent.exists()
        assert output.out == (
            f"  {target}: would create {target.parent} and set base_url\n" if mode == "1" else ""
        )
    assert output.err == ""


@pytest.mark.parametrize("action", ["config-set-gateway", "config-set-key-file"])
@pytest.mark.parametrize("mode", ["1", "check"])
def test_config_dry_run_and_check_do_not_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str, mode: str
) -> None:
    target = tmp_path / "config.toml"
    seed = 'secret = "not-for-output"\n'
    target.write_text(seed)
    before = target.stat()
    invoke(action, target, mode, "new")
    unchanged(target, seed.encode(), before)
    output = capsys.readouterr()
    name = "base_url" if action == "config-set-gateway" else "key_file"
    assert output.out == (f"  {target}: would set {name}\n" if mode == "1" else "")
    assert output.err == ""


@pytest.mark.parametrize("seed", [None, "", "base_url = 4\n", 'key_file = "elsewhere"\n'])
def test_config_get_missing_or_nonstring_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], seed: str | None
) -> None:
    target = tmp_path / "config.toml"
    if seed is not None:
        target.write_text(seed)
    invoke("config-get", target, value="base_url")
    assert capsys.readouterr() == ("", "")
    if seed is None:
        assert not target.exists()
    else:
        assert target.read_text() == seed


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        ('hidden = "unfinished', "is not valid TOML"),
        ('base_url = "a"\nbase_url = "b"\n', "is not valid TOML"),
        ('base_url = """line one\nline two"""\n', "would make the file invalid"),
        ('base_url.extra = "keep"\n', "would make the file invalid"),
        ('note = """\nbase_url = \\"inside\\"\n"""\n', "would also change other settings"),
    ],
)
def test_config_refusals_preserve_the_complete_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], seed: str, message: str
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(seed)
    before = target.stat()
    with pytest.raises(SystemExit) as exc:
        invoke("config-set-gateway", target, value="https://new.test")
    assert exc.value.code == 1
    assert message in capsys.readouterr().err
    unchanged(target, seed.encode(), before)


@pytest.mark.parametrize("action", ["claude-add", "codex-add", "config-set-gateway", "config-get"])
@pytest.mark.parametrize("kind", ["directory", "fifo", "dangling", "invalid-utf8"])
def test_nonregular_or_invalid_text_is_refused_without_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str, kind: str
) -> None:
    target = tmp_path / "settings"
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "dangling":
        target.symlink_to("absent")
    else:
        target.write_bytes(b"\xff")
        os.utime(target, ns=(1, target.stat().st_mtime_ns))
    before = target.lstat()
    with pytest.raises(SystemExit) as exc:
        invoke(action, target, value="base_url")
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    expected = {
        "directory": "is not a regular file",
        "fifo": "is not a regular file",
        "dangling": "is a symbolic link to absent",
        "invalid-utf8": "is not valid UTF-8 text",
    }
    assert expected[kind] in output.err
    after = target.lstat()
    # Reading invalid text can update access time without changing the file.
    for field in (
        "st_mode",
        "st_ino",
        "st_dev",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    ):
        assert getattr(after, field) == getattr(before, field), field
    assert list(tmp_path.iterdir()) == [target]
    if kind == "invalid-utf8":
        assert target.read_bytes() == b"\xff"
    elif kind == "dangling":
        assert os.readlink(target) == "absent"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write read-only files")
@pytest.mark.parametrize("kind", ["file", "directory", "unreadable", "new-parent"])
def test_real_permissions_refuse_the_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    directory = tmp_path / "config"
    directory.mkdir()
    target = directory / "settings.json"
    target.write_text("{}")
    metadata = target.stat()
    if kind == "file":
        target.chmod(0o400)
    elif kind == "unreadable":
        target.chmod(0o200)
    else:
        directory.chmod(0o500)
    try:
        with pytest.raises(SystemExit) as exc:
            if kind == "new-parent":
                invoke("config-set-gateway", directory / "new" / "config.toml", value="new")
            else:
                invoke("claude-add", target)
        assert exc.value.code == 1
        messages = {
            "file": "is read-only",
            "directory": "Cannot write to",
            "unreadable": "Cannot read",
            "new-parent": "Cannot create",
        }
        assert messages[kind] in capsys.readouterr().err
    finally:
        target.chmod(0o644)
        directory.chmod(0o755)
    unchanged(target, b"{}", metadata)
    assert list(directory.iterdir()) == [target]


def test_new_parent_cannot_be_a_file(tmp_path: Path) -> None:
    parent = tmp_path / "file"
    parent.write_text("keep")
    target = parent / "config.toml"
    with pytest.raises(SystemExit):
        invoke("config-set-gateway", target, value="new")
    assert parent.read_text() == "keep"
    assert list(tmp_path.iterdir()) == [parent]


def test_a_concurrent_writer_is_kept_and_temporary_file_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{}")
    original = os.fsync

    def concurrent_write(descriptor: int) -> None:
        target.write_text('{"other":"writer"}')
        original(descriptor)

    monkeypatch.setattr(os, "fsync", concurrent_write)
    with pytest.raises(SystemExit) as exc:
        invoke("claude-add", target)
    assert exc.value.code == 1
    assert capsys.readouterr().err == (
        f"{target} changed while install.sh was running. Nothing was written. Run this again.\n"
    )
    assert target.read_text() == '{"other":"writer"}'
    assert list(tmp_path.iterdir()) == [target]


def test_failed_replace_keeps_original_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{}")
    metadata = target.stat()

    def fail_replace(source: str, destination: str) -> None:
        raise OSError(errno.EIO, "simulated write failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated write failure"):
        invoke("claude-add", target)
    unchanged(target, b"{}", metadata)


@pytest.mark.parametrize("action", ["codex-add", "config-set-gateway", "config-get"])
def test_missing_tomllib_refuses_toml_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    target = tmp_path / "config.toml"
    target.write_text('base_url = "keep"\n')
    before = target.stat()
    monkeypatch.setattr(hostconfig, "tomllib", None)
    with pytest.raises(SystemExit) as exc:
        invoke(action, target, value="base_url")
    assert exc.value.code == 1
    assert "needs Python 3.11 or newer" in capsys.readouterr().err
    unchanged(target, b'base_url = "keep"\n', before)


def test_invalid_guard_path_is_refused_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{}")
    before = target.stat()
    with pytest.raises(SystemExit):
        hostconfig.main(["install_helper.py", "claude-add", str(target), "0", MATCHER, "bad\udcff"])
    unchanged(target, b"{}", before)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", "is empty."),
        ("https://g.test/a b", "must not contain spaces"),
        ("ftp://g.test", "must start with http:// or https://"),
        ("https:g.test", "must start with http:// or https://"),
        ("https://", "has no host name"),
        ("https://[::1", "is not a valid URL"),
        ("https://g.test:bad", "has a port that is not a number from 0 to 65535"),
        ("https://g.test:", "has no port number after the colon"),
        ("http://g.test", "must use https:// for a host other than localhost"),
        ("https://user@g.test", "must not contain credentials (user@host)"),
        ("https://g.test?", "must not have a query (?) or a fragment (#)"),
        ("https://g.test#", "must not have a query (?) or a fragment (#)"),
    ],
)
def test_url_refusal_preserves_the_installer_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], url: str, reason: str
) -> None:
    label = tmp_path / "Gateway URL"
    with pytest.raises(SystemExit) as exc:
        invoke("config-check-url", label, value=url)
    assert exc.value.code == 1
    message = capsys.readouterr().err
    assert message == f"{label} {reason}\n"
    if url:
        assert url not in message
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("  HTTPS://Example.test/path///  ", "https://Example.test/path"),
        ("http://localhost/", "http://localhost"),
        ("http://127.8.0.1:8080/", "http://127.8.0.1:8080"),
        ("http://[::1]", "http://[::1]"),
    ],
)
def test_url_normalisation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], url: str, expected: str
) -> None:
    invoke("config-check-url", tmp_path / "Gateway URL", value=url)
    assert capsys.readouterr() == (expected + "\n", "")
    assert list(tmp_path.iterdir()) == []


def test_unknown_action_has_the_existing_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        invoke("unknown", tmp_path / "settings")
    assert exc.value.code == 1
    assert capsys.readouterr() == ("", "unknown action: unknown\n")
    assert list(tmp_path.iterdir()) == []


def test_launcher_loads_only_bundled_code_from_another_working_directory(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    for name in ("json", "hostconfig", "enmaas_cli"):
        (tmp_path / f"{name}.py").write_text('raise AssertionError("workspace module imported")\n')
    env = dict(os.environ)
    for key, directory in {
        "HOME": "home",
        "CLAUDE_CONFIG_DIR": "claude",
        "CODEX_HOME": "codex",
        "XDG_CONFIG_HOME": "config",
        "XDG_STATE_HOME": "state",
        "XDG_DATA_HOME": "data",
        "XDG_CACHE_HOME": "cache",
    }.items():
        env[key] = str(tmp_path / directory)
    env["PYTHONPATH"] = str(tmp_path)
    target = tmp_path / "settings.json"
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(repo / "scripts/install_helper.py"),
            "claude-add",
            str(target),
            "0",
            MATCHER,
            GUARD,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"  {target}: registered the enmaas guard\n"
    assert target.read_text() == json_text({"hooks": {"PreToolUse": [ENTRY]}})
    assert not list(tmp_path.rglob("__pycache__"))


def test_parser_recursion_refusal_keeps_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "settings.json"
    target.write_text('{"keep":true}')
    before = target.stat()

    def exhausted_parser(text: str) -> object:
        raise RecursionError("simulated parser depth limit")

    monkeypatch.setattr(hostconfig, "load_json", exhausted_parser)
    with pytest.raises(SystemExit) as exc:
        invoke("claude-add", target)
    assert exc.value.code == 1
    assert capsys.readouterr().err == (
        f"{target} is not valid JSON. Fix or move it, then run this again.\n"
    )
    unchanged(target, b'{"keep":true}', before)


@pytest.mark.parametrize("mode", ["0", "1", "check"])
@pytest.mark.parametrize("host", ["claude", "codex"])
def test_removal_without_a_guard_preserves_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str, host: str
) -> None:
    target = tmp_path / "settings"
    seed = '{"model":"keep"}' if host == "claude" else 'model = "keep"\n'
    target.write_text(seed)
    metadata = target.stat()
    invoke(f"{host}-remove", target, mode)
    unchanged(target, seed.encode(), metadata)
    assert capsys.readouterr() == (
        "" if mode == "check" else f"  {target}: no enmaas guard registered\n",
        "",
    )


@pytest.mark.parametrize(
    ("marked", "reason"),
    [
        (f"{BEGIN}\n{END}\n", "is not the block this installer writes"),
        (
            'note = """\n' + BEGIN + "\nnot toml\n" + END + '\n"""\n',
            "is not the block this installer writes",
        ),
        (BLOCK, "without the enmaas markers"),
    ],
)
def test_an_outside_guard_does_not_make_the_marked_block_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], marked: str, reason: str
) -> None:
    target = tmp_path / "config.toml"
    unmarked = BLOCK.replace(BEGIN + "\n", "").replace(END + "\n", "")
    seed = marked + unmarked
    target.write_text(seed)
    metadata = target.stat()
    with pytest.raises(SystemExit) as exc:
        invoke("codex-add", target)
    assert exc.value.code == 1
    assert reason in capsys.readouterr().err
    unchanged(target, seed.encode(), metadata)


def test_an_unexpected_parsed_setting_change_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import tomllib

    target = tmp_path / "config.toml"
    seed = 'model = "keep"\n'
    target.write_text(seed)
    metadata = target.stat()
    parse = tomllib.loads

    def altered_parse(text: str) -> dict[str, Any]:
        doc = parse(text)
        if text == seed + BLOCK:
            doc["model"] = "unexpected change"
        return doc

    monkeypatch.setattr(tomllib, "loads", altered_parse)
    with pytest.raises(SystemExit) as exc:
        invoke("codex-add", target)
    assert exc.value.code == 1
    assert capsys.readouterr().err == (
        f"{target}: adding the guard block would also change other settings. Merge it by hand.\n"
    )
    unchanged(target, seed.encode(), metadata)


def test_claude_replaces_a_guard_with_a_different_matcher_type(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    old_entry = {**ENTRY, "matcher": True}
    target.write_text(json_text({"hooks": {"PreToolUse": [old_entry]}}))
    invoke("claude-add", target)
    assert target.read_text() == json_text({"hooks": {"PreToolUse": [ENTRY]}})


@pytest.mark.parametrize("action", ["codex-add", "codex-remove"])
def test_removing_markers_cannot_leave_an_unterminated_toml_string(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    target = tmp_path / "config.toml"
    seed = 'note = """\n' + BLOCK.replace(BEGIN + "\n", BEGIN + '\n# """\n')
    target.write_text(seed)
    metadata = target.stat()
    with pytest.raises(SystemExit) as exc:
        invoke(action, target)
    assert exc.value.code == 1
    assert "removing the enmaas guard block would leave invalid TOML" in capsys.readouterr().err
    unchanged(target, seed.encode(), metadata)


@pytest.mark.parametrize(
    ("url", "cli_problem", "installer_problem", "cli_reason", "installer_reason", "detail"),
    [
        (
            "https://g.test/a b",
            "whitespace",
            "whitespace",
            "whitespace inside the URL",
            "must not contain spaces",
            None,
        ),
        (
            "ftp://g.test",
            "scheme",
            "scheme",
            "scheme must be http or https",
            "must start with http:// or https://",
            None,
        ),
        (
            "http://g.test",
            "cleartext",
            "cleartext",
            "http is only allowed for localhost, use https",
            "must use https:// for a host other than localhost",
            None,
        ),
        (
            "https://g.test?",
            "query",
            "query",
            "query and fragment are not allowed",
            "must not have a query (?) or a fragment (#)",
            None,
        ),
        (
            "https://user@g.test",
            "credentials",
            "credentials",
            "credentials in the URL are not allowed",
            "must not contain credentials (user@host)",
            None,
        ),
        ("https://", "host", "host", "missing host", "has no host name", None),
        (
            "https://g.test:bad",
            "port",
            "port",
            "invalid port",
            "has a port that is not a number from 0 to 65535",
            None,
        ),
        (
            "https://g.test:",
            "empty_port",
            "empty_port",
            "empty port",
            "has no port number after the colon",
            None,
        ),
        (
            "http://g.test:bad",
            "cleartext",
            "port",
            "http is only allowed for localhost, use https",
            "has a port that is not a number from 0 to 65535",
            None,
        ),
        (
            "ftp://g.test:bad",
            "scheme",
            "port",
            "scheme must be http or https",
            "has a port that is not a number from 0 to 65535",
            None,
        ),
        (
            "https:user@g.test",
            "host",
            "scheme",
            "missing host",
            "must start with http:// or https://",
            None,
        ),
        (
            "https://user@g.test?",
            "query",
            "credentials",
            "query and fragment are not allowed",
            "must not contain credentials (user@host)",
            None,
        ),
        (
            "https://user@:bad",
            "credentials",
            "port",
            "credentials in the URL are not allowed",
            "has a port that is not a number from 0 to 65535",
            None,
        ),
        (
            "https://:bad",
            "host",
            "port",
            "missing host",
            "has a port that is not a number from 0 to 65535",
            None,
        ),
        (
            "https://g.test:?",
            "query",
            "empty_port",
            "query and fragment are not allowed",
            "has no port number after the colon",
            None,
        ),
        (
            "https://[::1",
            "host",
            "host",
            "missing host",
            "is not a valid URL",
            "Invalid IPv6 URL",
        ),
    ],
)
def test_gateway_callers_keep_their_messages_and_problem_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    url: str,
    cli_problem: str,
    installer_problem: str,
    cli_reason: str,
    installer_reason: str,
    detail: str | None,
) -> None:
    from enmaas_cli import gateway
    from enmaas_cli.cli import resolve_base_url

    assert gateway.check(url) == gateway.Checked(url, cli_problem)
    with pytest.raises(ValueError) as cli_exc:
        resolve_base_url(None, {"ENMAAS_BASE_URL": url}, tmp_path / "config.toml")
    assert str(cli_exc.value) == f"gateway URL is not valid ({cli_reason})"
    with pytest.raises(SystemExit) as installer_exc:
        invoke("config-check-url", Path("Gateway URL"), value=url)
    assert installer_exc.value.code == 1
    captured = capsys.readouterr()
    assert captured == ("", f"Gateway URL {installer_reason}\n")
    assert url not in str(cli_exc.value)
    assert url not in captured.err
    assert list(tmp_path.iterdir()) == []
    assert gateway.check_installer(url) == gateway.InstallerChecked(url, installer_problem, detail)


def test_installer_url_check_preserves_empty_input_and_parser_detail() -> None:
    from urllib.parse import urlsplit

    from enmaas_cli import gateway

    assert gateway.check("") == gateway.Checked("", gateway.SCHEME)
    assert gateway.check_installer(" \t ") == gateway.InstallerChecked("", gateway.EMPTY)
    url = "https://[not-ip]"
    with pytest.raises(ValueError) as parser_exc:
        urlsplit(url)
    assert gateway.check_installer(url) == gateway.InstallerChecked(
        url, gateway.HOST, str(parser_exc.value)
    )


def test_isolated_installer_url_check_does_not_import_the_package(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    copy_root = tmp_path / "copy"
    for relative in (
        "scripts/install_helper.py",
        "src/enmaas_cli/hostconfig.py",
        "src/enmaas_cli/gateway.py",
    ):
        target = copy_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((repo / relative).read_bytes())
    (copy_root / "src/enmaas_cli/__init__.py").write_text(
        'raise AssertionError("the installer imported the CLI package")\n'
    )
    env = dict(os.environ)
    for name in (
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        env[name] = str(tmp_path / name.lower())
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(copy_root / "scripts/install_helper.py"),
            "config-check-url",
            "Gateway URL",
            "0",
            " HTTP://LOCALHOST:8080/ ",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "http://LOCALHOST:8080\n"
    assert not list(copy_root.rglob("__pycache__"))


@pytest.mark.parametrize(
    ("url", "cli_reason", "installer_reason"),
    [
        (
            "https://UserSecret:PassSecret@g.test",
            "credentials in the URL are not allowed",
            "must not contain credentials (user@host)",
        ),
        (
            "https://User%53ecret:Pass%53ecret@g.test",
            "credentials in the URL are not allowed",
            "must not contain credentials (user@host)",
        ),
        ("https://UserSecret:PassSecret@[broken", "missing host", "is not a valid URL"),
        (
            "https://UserSecret:PassSecret@g.test:bad",
            "credentials in the URL are not allowed",
            "has a port that is not a number from 0 to 65535",
        ),
        (
            "ftp://UserSecret:PassSecret@g.test",
            "scheme must be http or https",
            "must start with http:// or https://",
        ),
        (
            "http://UserSecret:PassSecret@g.test",
            "http is only allowed for localhost, use https",
            "must use https:// for a host other than localhost",
        ),
        (
            "https://UserSecret:PassSecret@g.test?x=1",
            "query and fragment are not allowed",
            "must not contain credentials (user@host)",
        ),
        (
            "https://UserSecret:PassSecret@g.test#part",
            "query and fragment are not allowed",
            "must not contain credentials (user@host)",
        ),
        (
            "https://UserSecret:PassSecret@g.test/a b",
            "whitespace inside the URL",
            "must not contain spaces",
        ),
        (
            "https://UserSecret:PassSecret@",
            "credentials in the URL are not allowed",
            "has no host name",
        ),
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_url_refusals_never_echo_credentials_before_key_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
    cli_reason: str,
    installer_reason: str,
    as_json: bool,
) -> None:
    import os

    from enmaas_cli import cli

    for name in os.environ:
        if name.startswith("ENMAAS_"):
            monkeypatch.delenv(name, raising=False)
    for name in (
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("ENMAAS_BASE_URL", url)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("URL refusal must precede keys, clients and writes")

    monkeypatch.setattr(cli, "resolve_key", forbidden)
    monkeypatch.setattr(cli, "make_client", forbidden)
    argv = ["--config", str(tmp_path / "config.toml"), "models"]
    if as_json:
        argv += ["--json"]
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    reason = f"gateway URL is not valid ({cli_reason})"
    if as_json:
        assert json.loads(captured.out) == {"status": "error", "error": reason}
        assert captured.err == ""
    else:
        assert captured == ("", f"error: {reason}\n")
    with pytest.raises(SystemExit) as caught:
        invoke("config-check-url", Path("Gateway URL"), value=url)
    assert caught.value.code == 1
    helper = capsys.readouterr()
    assert helper == ("", f"Gateway URL {installer_reason}\n")
    diagnostics = captured.out + captured.err + helper.err
    for secret in (
        url,
        "UserSecret",
        "PassSecret",
        "User%53ecret",
        "Pass%53ecret",
        "Invalid IPv6 URL",
    ):
        assert secret not in diagnostics
    assert list(tmp_path.iterdir()) == []
