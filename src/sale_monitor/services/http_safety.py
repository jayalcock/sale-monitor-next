"""SSRF-safe HTTP fetching for user-supplied URLs.

The web app fetches pages and images from URLs that users type in, so
every request (including each redirect hop) must be restricted to public
hosts.  ``requests`` follows redirects blindly, which would let a public
URL 302 to an internal address; ``safe_get`` follows them manually and
re-validates each hop instead.
"""
import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5

_FORBIDDEN_HOSTS = {"localhost", "localhost.localdomain"}


def is_obviously_non_public_url(url: str) -> bool:
    """Syntax-only screen (no DNS): True for non-http(s) URLs, local
    hostnames, and IP literals in private/loopback/reserved ranges.

    Cheap enough to run at product-add time, where a resolving check
    would make adds network-dependent; ``safe_get`` still performs the
    full resolving check on every fetch and redirect hop.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    if parsed.scheme not in ("http", "https"):
        return True
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not hostname:
        return True
    if hostname.lower() in _FORBIDDEN_HOSTS:
        return True
    # Single-label hostnames (no dot) are intranet names, not public sites
    if "." not in hostname:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False  # a resolvable name; safe_get re-checks what it resolves to
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_public_url(url: str) -> bool:
    """Allow only http(s) URLs whose host resolves exclusively to public IPs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        return False
    # Disallow obvious local names
    if hostname in _FORBIDDEN_HOSTS:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for rec in infos:
        family = rec[0]
        try:
            if family == socket.AF_INET:
                ip = ipaddress.ip_address(rec[4][0])
            elif hasattr(socket, 'AF_INET6') and family == socket.AF_INET6:
                ip = ipaddress.ip_address(rec[4][0])
            else:
                # Unknown family: reject
                return False
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
            ):
                return False
        except (ValueError, OSError):
            return False
    return True


def safe_get(url: str, *, headers=None, timeout=30, stream=False,
             max_redirects: int = MAX_REDIRECTS) -> Optional[requests.Response]:
    """GET *url* following redirects manually, validating every hop.

    Returns the final response, or None if any hop targets a non-public
    host or the redirect chain is too long.  Raises
    ``requests.RequestException`` on network errors like plain ``get``.
    """
    current = url
    for _ in range(max_redirects + 1):
        if not is_public_url(current):
            logger.warning("Blocked fetch of non-public URL: %s", current)
            return None
        resp = requests.get(current, headers=headers, timeout=timeout,
                            stream=stream, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get('Location')
            resp.close()
            if not location:
                return None
            current = urljoin(current, location)
            continue
        return resp
    logger.warning("Too many redirects fetching %s", url)
    return None
