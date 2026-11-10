"""The rules a gateway URL must follow.

Standard library only, and no import from the rest of the package, so a
helper can import this module without site-packages. A rule that fails
returns a problem code, never a message: each caller words the problem in
its own way.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import NamedTuple
from urllib.parse import urlsplit

BASE_URL_ENV = "ENMAAS_BASE_URL"

# Problem codes, one per rule.
WHITESPACE = "whitespace"
SCHEME = "scheme"
CLEARTEXT = "cleartext"
QUERY = "query"
CREDENTIALS = "credentials"
HOST = "host"
PORT = "port"


class Checked(NamedTuple):
    """The result of a check.

    `url` is the normalised URL when `problem` is None. Otherwise it is the
    URL as given.
    """

    url: str
    problem: str | None


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
    """Check a gateway URL and normalise it.

    The URL is the gateway root. Requests append /v1/... to it, so a path in
    it is kept as typed. The scheme is lowered, the host keeps its case, and
    trailing slashes are removed.
    """

    def refuse(problem: str) -> Checked:
        return Checked(url, problem)

    if any(ch.isspace() for ch in url):
        return refuse(WHITESPACE)
    try:
        parts = urlsplit(url)
    except ValueError:
        # urlsplit only raises here for a malformed bracketed IPv6 host.
        return refuse(HOST)
    if parts.scheme.lower() not in {"http", "https"}:
        return refuse(SCHEME)
    # The key travels in a header, so cleartext http would send it across
    # the network readable to anyone on the path. A local gateway or
    # tunnel is the only place where that is safe.
    if parts.scheme.lower() == "http" and not is_loopback(parts.hostname):
        return refuse(CLEARTEXT)
    # Checked on the raw text: "https://h?" has an empty query, yet the
    # appended request path would still land after the "?".
    if "?" in url or "#" in url:
        return refuse(QUERY)
    # The key travels in a header. Credentials in the URL would be a
    # second secret that ends up in logs and error messages.
    if "@" in parts.netloc:
        return refuse(CREDENTIALS)
    # Without a host, "https://" plus "/v1/models" would send the key to a
    # host named "v1".
    if not parts.hostname:
        return refuse(HOST)
    try:
        _ = parts.port
    except ValueError:
        # httpx would raise InvalidURL later, outside any handler.
        return refuse(PORT)
    # Request paths start with a slash, so a trailing one here would double it.
    return Checked(f"{parts.scheme.lower()}://{parts.netloc}{parts.path.rstrip('/')}", None)
