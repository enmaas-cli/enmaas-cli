from __future__ import annotations

from pathlib import Path

import pytest

from enmaas_cli.auth import REDACTION, AuthError, Key, cut_redacted, printable, redact, resolve_key

KEY = "sk-oai-" + "a" * 60


def _write_key(home: Path, mode: int = 0o600) -> Path:
    secrets = home / ".secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    path = secrets / "enmaas"
    path.write_text(KEY + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_explicit_key_file_wins(tmp_path: Path) -> None:
    _write_key(tmp_path)
    other = tmp_path / "other"
    other.write_text("sk-oai-other\n", encoding="utf-8")
    other.chmod(0o600)
    key = resolve_key(key_file=other, env={"ENMAAS_KEY": "sk-oai-env"}, home=tmp_path)
    assert key.value == "sk-oai-other"
    assert key.source == str(other)


def test_env_beats_default_file(tmp_path: Path) -> None:
    _write_key(tmp_path)
    key = resolve_key(key_file=None, env={"ENMAAS_KEY": "sk-oai-env"}, home=tmp_path)
    assert key.value == "sk-oai-env"
    assert key.source == "ENMAAS_KEY"


def test_falls_back_to_default_file(tmp_path: Path) -> None:
    path = _write_key(tmp_path)
    key = resolve_key(key_file=None, env={}, home=tmp_path)
    assert key.value == KEY
    assert key.source == str(path)
    assert key.warning is None


def test_trailing_whitespace_stripped(tmp_path: Path) -> None:
    path = _write_key(tmp_path)
    path.write_text(f"  {KEY}  \n\n", encoding="utf-8")
    assert resolve_key(key_file=None, env={}, home=tmp_path).value == KEY


def test_world_readable_file_warns(tmp_path: Path) -> None:
    _write_key(tmp_path, mode=0o644)
    key = resolve_key(key_file=None, env={}, home=tmp_path)
    assert key.warning is not None
    assert "644" in key.warning


def test_missing_key_raises(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="no EnMaaS key"):
        resolve_key(key_file=None, env={}, home=tmp_path)


def test_empty_key_file_raises(tmp_path: Path) -> None:
    path = _write_key(tmp_path)
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(AuthError, match="empty"):
        resolve_key(key_file=None, env={}, home=tmp_path)


def test_missing_explicit_key_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="does not exist"):
        resolve_key(key_file=tmp_path / "nope", env={}, home=tmp_path)


def _write_other(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_flag_beats_env_and_configured_file(tmp_path: Path) -> None:
    flag = _write_other(tmp_path, "flag", "sk-oai-flag")
    configured = _write_other(tmp_path, "configured", "sk-oai-configured")
    key = resolve_key(
        key_file=flag,
        env={"ENMAAS_KEY": "sk-oai-env"},
        home=tmp_path,
        config_key_file=configured,
    )
    assert key.value == "sk-oai-flag"
    assert key.source == str(flag)


def test_env_beats_configured_file(tmp_path: Path) -> None:
    configured = _write_other(tmp_path, "configured", "sk-oai-configured")
    key = resolve_key(
        key_file=None,
        env={"ENMAAS_KEY": "sk-oai-env"},
        home=tmp_path,
        config_key_file=configured,
    )
    assert key.value == "sk-oai-env"
    assert key.source == "ENMAAS_KEY"


def test_configured_file_beats_default_file(tmp_path: Path) -> None:
    _write_key(tmp_path)
    configured = _write_other(tmp_path, "configured", "sk-oai-configured")
    key = resolve_key(key_file=None, env={}, home=tmp_path, config_key_file=configured)
    assert key.value == "sk-oai-configured"
    assert key.source == str(configured)


def test_default_file_used_when_nothing_is_configured(tmp_path: Path) -> None:
    path = _write_key(tmp_path)
    key = resolve_key(key_file=None, env={}, home=tmp_path, config_key_file=None)
    assert key.value == KEY
    assert key.source == str(path)


def test_missing_configured_key_file_names_it(tmp_path: Path) -> None:
    # The default file exists, but the configured one wins and is missing.
    _write_key(tmp_path)
    missing = tmp_path / "keys" / "enmaas"
    with pytest.raises(AuthError, match="does not exist") as exc_info:
        resolve_key(key_file=None, env={}, home=tmp_path, config_key_file=missing)
    assert str(missing) in str(exc_info.value)


def test_configured_key_file_permission_warning(tmp_path: Path) -> None:
    configured = _write_other(tmp_path, "configured", "sk-oai-configured")
    configured.chmod(0o644)
    key = resolve_key(key_file=None, env={}, home=tmp_path, config_key_file=configured)
    assert key.warning is not None
    assert f"chmod 600 {configured}" in key.warning


def test_redact_replaces_every_occurrence() -> None:
    text = f"auth failed for {KEY} using {KEY}"
    out = redact(text, KEY)
    assert KEY not in out
    assert out.count("***REDACTED***") == 2


def test_redact_handles_empty_key() -> None:
    assert redact("nothing to do", "") == "nothing to do"


def test_key_repr_does_not_leak() -> None:
    key = Key(value=KEY, source="test", warning=None)
    assert KEY not in repr(key)


BAD_KEYS = {
    "non-ascii": "sk-oai-" + "Q" * 20 + "é" + "Z" * 20,
    "inner-space": "sk-oai-" + "Q" * 20 + " " + "Z" * 20,
    "control": "sk-oai-" + "Q" * 20 + "\x07" + "Z" * 20,
}


def _resolve_from(source: str, tmp_path: Path, value: str) -> Key:
    path = tmp_path / "keyfile"
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    if source == "env":
        return resolve_key(key_file=None, env={"ENMAAS_KEY": value}, home=tmp_path)
    if source == "flag":
        return resolve_key(key_file=path, env={}, home=tmp_path)
    if source == "config":
        return resolve_key(key_file=None, env={}, home=tmp_path, config_key_file=path)
    default = tmp_path / ".secrets" / "enmaas"
    default.parent.mkdir(parents=True, exist_ok=True)
    path.rename(default)
    return resolve_key(key_file=None, env={}, home=tmp_path)


SOURCES = ["env", "flag", "config", "default"]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", list(BAD_KEYS))
def test_a_key_that_cannot_be_sent_in_a_header_is_refused(
    tmp_path: Path, source: str, kind: str
) -> None:
    value = BAD_KEYS[kind]
    with pytest.raises(AuthError) as exc:
        _resolve_from(source, tmp_path, value)
    message = str(exc.value)
    assert "holds a character that cannot be sent in a header" in message
    assert message.startswith("the key from ")
    for start in range(len(value) - 3):
        assert value[start : start + 4] not in message
    if kind != "inner-space":
        assert value[27] not in message


@pytest.mark.parametrize("source", SOURCES)
def test_a_sendable_key_passes_from_every_source(tmp_path: Path, source: str) -> None:
    assert _resolve_from(source, tmp_path, KEY).value == KEY


# Gateway text is redacted, then made printable, then cut.
#
# `cut_redacted` cuts text that is already redacted without splitting the
# placeholder. `printable` makes text safe to print on one terminal line.


def test_cut_keeps_a_placeholder_that_straddles_the_limit() -> None:
    text = "a" * 10 + REDACTION + "b" * 10
    cut = cut_redacted(text, 15)
    assert cut == "a" * 10 + REDACTION


def test_cut_keeps_text_before_the_placeholder_at_the_limit() -> None:
    text = "a" * 10 + REDACTION + "b" * 10
    assert cut_redacted(text, 5) == "a" * 5
    assert cut_redacted(text, 10) == "a" * 10


def test_cut_at_a_placeholder_that_ends_before_the_limit() -> None:
    text = REDACTION + "b" * 20
    assert cut_redacted(text, len(REDACTION)) == REDACTION
    assert cut_redacted(text, len(REDACTION) + 3) == REDACTION + "bbb"


def test_cut_ignores_a_placeholder_after_the_limit() -> None:
    text = "a" * 20 + REDACTION
    assert cut_redacted(text, 20) == "a" * 20
    assert cut_redacted(text, 5) == "a" * 5


def test_cut_of_short_text_is_unchanged() -> None:
    assert cut_redacted("abc", 80) == "abc"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\nb", "a b"),
        ("a\rb", "a b"),
        ("a\tb", "a b"),
        ("a\x1bb", "a?b"),
        ("a\x07b", "a?b"),
        ("a\x7fb", "a?b"),
        ("a\x9bb", "a?b"),
        ("a‮b", "a?b"),
        ("a b", "a?b"),
        ("a b", "a?b"),
        ("a\x00b", "a?b"),
    ],
)
def test_printable_replaces_characters_that_are_not_printable(raw: str, expected: str) -> None:
    assert printable(raw) == expected


def test_printable_keeps_ordinary_non_ascii_letters() -> None:
    text = "héllo ñandú Ωmega 日本語 Привет çà"
    assert printable(text) == text


def test_printable_output_holds_only_printable_characters() -> None:
    text = "".join(chr(n) for n in range(0x00, 0x300)) + "  ‮​﻿"
    assert printable(text).isprintable()
