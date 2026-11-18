"""The rules a gateway URL must follow.

Standard library only, and no import from the rest of the package, so a
helper can import this module without site-packages. A rule that fails
returns a problem code, never a message: each caller words the problem in
its own way.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from functools import partial
from typing import NamedTuple
from urllib.parse import SplitResult, urlsplit

BASE_URL_ENV = "ENMAAS_BASE_URL"

# Problem codes, one per rule.
WHITESPACE = "whitespace"
SCHEME = "scheme"
CLEARTEXT = "cleartext"
QUERY = "query"
CREDENTIALS = "credentials"
HOST = "host"
PORT = "port"
EMPTY_PORT = "empty_port"
EMPTY = "empty"


class Checked(NamedTuple):
    """The result of a check.

    `url` is the normalised URL when `problem` is None. Otherwise it is the
    URL as given.
    """

    url: str
    problem: str | None


class InstallerChecked(NamedTuple):
    """A checked URL with parser detail for the installer's host error."""

    url: str
    problem: str | None
    detail: str | None = None


def choose(configured: str | None, env: Mapping[str, str]) -> str:
    """The URL to use: $ENMAAS_BASE_URL first, then the configured one.

    Surrounding whitespace is removed. An empty result means no URL is set.
    """
    return env.get(BASE_URL_ENV, "").strip() or (configured or "").strip()


def is_loopback(host: str | None) -> bool:
    """Whether a host names this machine: localhost or a loopback address."""
    if host is None:
        # A missing host is reported by the URL check itself.
        return True
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check(url: str) -> Checked:
    """Check and normalise a gateway URL in the CLI's error order."""
    result = _check(url, _CLI_RULES)
    return Checked(result.url, result.problem)


def check_installer(url: str) -> InstallerChecked:
    """Use the installer's error order, whitespace trimming and parser detail."""
    url = url.strip()
    if not url:
        return InstallerChecked(url, EMPTY)
    return _check(url, _INSTALLER_RULES)


def _check(
    url: str, rules: tuple[Callable[[str, SplitResult], str | None], ...]
) -> InstallerChecked:
    if any(ch.isspace() for ch in url):
        return InstallerChecked(url, WHITESPACE)
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return InstallerChecked(url, HOST, str(exc))
    for rule in rules:
        problem = rule(url, parts)
        if problem is not None:
            return InstallerChecked(url, problem)
    # Requests append /v1/... to the root. Keep the path and host case,
    # lower the scheme, and remove slashes that would double the separator.
    normal = f"{parts.scheme.lower()}://{parts.netloc}{parts.path.rstrip('/')}"
    return InstallerChecked(normal, None)


def _scheme_problem(url: str, parts: SplitResult, *, require_separator: bool = False) -> str | None:
    if parts.scheme.lower() not in {"http", "https"}:
        return SCHEME
    if require_separator and not url.lower().startswith(parts.scheme + "://"):
        return SCHEME
    return None


def _cleartext_problem(url: str, parts: SplitResult) -> str | None:
    # The key, prompts and answers would cross the network in clear text.
    # Only a gateway on this machine may use plain HTTP.
    if parts.scheme.lower() == "http" and not is_loopback(parts.hostname):
        return CLEARTEXT
    return None


def _query_problem(url: str, parts: SplitResult) -> str | None:
    # Even an empty query would put an appended request path after the '?'.
    if "?" in url or "#" in url:
        return QUERY
    return None


def _credentials_problem(url: str, parts: SplitResult) -> str | None:
    # The key travels in a header. URL credentials would add another secret
    # that could reach logs and error messages.
    if "@" in parts.netloc:
        return CREDENTIALS
    return None


def _host_problem(url: str, parts: SplitResult) -> str | None:
    # Appending /v1/models to a URL without a host could send the key to 'v1'.
    if not parts.hostname:
        return HOST
    return None


def _numeric_port_problem(url: str, parts: SplitResult) -> str | None:
    try:
        _ = parts.port
    except ValueError:
        # Reject it before the HTTP client can raise outside its handler.
        return PORT
    return None


def _empty_port_problem(url: str, parts: SplitResult) -> str | None:
    # urlsplit treats a final colon as no port. Refuse that spelling too.
    if parts.port is None and parts.netloc.endswith(":"):
        return EMPTY_PORT
    return None


# Both callers use the same predicates. Their first reported problem is part
# of the interface, so a URL with several faults keeps each caller's order.
_CLI_RULES: tuple[Callable[[str, SplitResult], str | None], ...] = (
    _scheme_problem,
    _cleartext_problem,
    _query_problem,
    _credentials_problem,
    _host_problem,
    _numeric_port_problem,
    _empty_port_problem,
)
_INSTALLER_RULES: tuple[Callable[[str, SplitResult], str | None], ...] = (
    _numeric_port_problem,
    partial(_scheme_problem, require_separator=True),
    _host_problem,
    _cleartext_problem,
    _credentials_problem,
    _empty_port_problem,
    _query_problem,
)
