"""
services/urls.py — validate, canonicalise and de-duplicate submitted URLs.

The service fetches whatever URLs a caller supplies, which makes ``POST /runs``
a server-side request forgery primitive unless the targets are constrained.
Everything that decides "may we fetch this?" lives here so there is one place
to audit.

Scope note: this rejects hostnames and IP literals that obviously point inside
the perimeter. It cannot defeat DNS rebinding on its own — a public name can
resolve to 10.x at connect time. The real fix belongs in the HTTP client
(resolve first, check the resolved address, pin it for the connection, and
re-check every redirect hop); see ``services/stubs.py`` for where that hook
goes.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import config

ALLOWED_SCHEMES = ("http", "https")

# Hostnames that resolve inside the perimeter regardless of DNS.
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".corp", ".home.arpa")

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Hostnames that could be a numeric IPv4 in one of inet_aton's legacy spellings:
# "127.1", "0177.0.0.1", "0x7f.1", "2130706433". The ipaddress module rejects
# all of these as malformed, but resolvers and HTTP clients happily accept them,
# so treating them as ordinary DNS names is an SSRF bypass.
_MAYBE_NUMERIC_HOST = re.compile(r"^[0-9a-fA-FxX.]+$")


class UrlRejected(ValueError):
    """A submitted URL is not fetchable. The message is caller-safe."""


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Resolve inet_aton's shorthand/octal/hex/decimal IPv4 spellings, or None.

    ``127.1`` and ``0x7f000001`` both reach 127.0.0.1 through a normal HTTP
    client while ``ipaddress.ip_address`` calls them invalid.
    """
    if not _MAYBE_NUMERIC_HOST.match(host):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ipaddress.AddressValueError, UnicodeEncodeError):
        return None


def _check_host_is_public(host: str) -> None:
    """Raise UrlRejected if the host is loopback/private/link-local/reserved."""
    if config.settings.allow_private_network_urls:
        return

    bare = host.strip("[]").lower()

    if bare in _BLOCKED_HOSTNAMES or bare.endswith(_BLOCKED_SUFFIXES):
        raise UrlRejected(f"host '{host}' points at a private network")

    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        legacy = _legacy_ipv4(bare)
        if legacy is None:
            return  # a DNS name; resolved-address checking happens in the client
        ip = legacy

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local        # includes 169.254.169.254, the cloud metadata endpoint
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise UrlRejected(f"address '{host}' points at a private network")

    # ::ffff:10.0.0.1 and friends smuggle a private v4 address through a v6 literal.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        _check_host_is_public(str(mapped))


def canonicalise(raw: str) -> str:
    """Validate one URL and return its canonical form.

    Raises UrlRejected with a message that is safe to return to the caller.
    """
    if not isinstance(raw, str):
        raise UrlRejected("URL must be a string")

    url = raw.strip()
    if not url:
        raise UrlRejected("URL must not be blank")
    if len(url) > config.settings.max_url_chars:
        raise UrlRejected(f"URL exceeds {config.settings.max_url_chars} characters")
    # A newline or tab inside a URL is a request-splitting vector in some clients.
    if any(ch in url for ch in "\r\n\t") or any(ord(ch) < 0x20 for ch in url):
        raise UrlRejected("URL contains control characters")

    parts = urlsplit(url)

    if not parts.scheme:
        raise UrlRejected("URL must start with http:// or https://")
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlRejected(f"scheme '{parts.scheme}' is not allowed (use http or https)")

    if parts.username or parts.password:
        raise UrlRejected("URL must not contain embedded credentials")

    try:
        hostname = parts.hostname
    except ValueError as exc:            # malformed IPv6 literal, bad port, etc.
        raise UrlRejected(f"malformed URL: {exc}") from exc
    if not hostname:
        raise UrlRejected("URL must include a host")

    try:
        port = parts.port
    except ValueError as exc:
        raise UrlRejected(f"invalid port: {exc}") from exc

    _check_host_is_public(hostname)

    # Canonical form: lower-case scheme and host, default port dropped, empty
    # path normalised to "/", fragment removed (never sent to a server anyway).
    host = hostname.lower()
    if ":" in host:                       # IPv6 literal needs its brackets back
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"

    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def normalise_many(raw_urls: list[str]) -> list[str]:
    """Canonicalise a batch, drop duplicates, keep submission order.

    Raises UrlRejected naming the offending entry so the caller can fix it,
    rather than silently dropping input.
    """
    max_urls = config.settings.max_urls_per_run
    if len(raw_urls) > max_urls:
        raise UrlRejected(f"at most {max_urls} URLs per run (got {len(raw_urls)})")

    seen: set[str] = set()
    out: list[str] = []
    for index, raw in enumerate(raw_urls):
        try:
            url = canonicalise(raw)
        except UrlRejected as exc:
            raise UrlRejected(f"urls[{index}]: {exc}") from exc
        if url not in seen:
            seen.add(url)
            out.append(url)

    if not out:
        raise UrlRejected("at least one URL is required")
    return out
