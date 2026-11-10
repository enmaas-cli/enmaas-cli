"""The gateway URL rules as problem codes, and the module's import floor."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import enmaas_cli
from enmaas_cli import gateway


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("https://g.test/v 1", gateway.WHITESPACE),
        ("ftp://g.test", gateway.SCHEME),
        ("g.test", gateway.SCHEME),
        ("http://g.test", gateway.CLEARTEXT),
        ("https://g.test?a=b", gateway.QUERY),
        ("https://g.test#top", gateway.QUERY),
        ("https://user@g.test", gateway.CREDENTIALS),
        ("https://", gateway.HOST),
        ("http://[::1", gateway.HOST),
        ("https://g.test:bad", gateway.PORT),
    ],
)
def test_each_rule_returns_its_problem_code(url: str, problem: str) -> None:
    assert gateway.check(url) == gateway.Checked(url, problem)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Host.Example/", "https://Host.Example"),
        ("https://host/v1//", "https://host/v1"),
        ("http://[::1]:8080", "http://[::1]:8080"),
    ],
)
def test_a_good_url_is_normalised(url: str, expected: str) -> None:
    assert gateway.check(url) == gateway.Checked(expected, None)


def test_the_environment_wins_over_the_config() -> None:
    env = {gateway.BASE_URL_ENV: "  https://env.test  "}
    assert gateway.choose("https://config.test", env) == "https://env.test"
    assert gateway.choose(" https://config.test ", {}) == "https://config.test"
    assert gateway.choose(None, {gateway.BASE_URL_ENV: "  "}) == ""


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        (None, True),
        ("LOCALHOST", True),
        ("127.3.4.5", True),
        ("::1", True),
        ("10.0.0.1", False),
        ("localhost.example.test", False),
    ],
)
def test_loopback_hosts(host: str | None, loopback: bool) -> None:
    assert gateway.is_loopback(host) is loopback


def test_the_module_imports_without_site_packages() -> None:
    """A helper imports this module with site-packages disabled."""
    source = str(Path(enmaas_cli.__file__).resolve().parents[1])
    program = (
        "import sys\n"
        f"sys.path.insert(0, {source!r})\n"
        "import enmaas_cli.gateway as gateway\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('enmaas_cli'))\n"
        "assert loaded == ['enmaas_cli', 'enmaas_cli.gateway'], loaded\n"
        "assert gateway.check('https://g.test/').url == 'https://g.test'\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
