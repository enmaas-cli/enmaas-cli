from pathlib import Path

import pytest

from enmaas_cli.auth import AuthError, Key, redact, resolve_key

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
