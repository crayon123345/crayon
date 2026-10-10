"""Shared canonical URL identity for the find ports.

Single source of truth for URL identity across ports/find and
ports/find_track (Pratham round-1: one corrected canonicalizer, not
copies). Identity preserves scheme and non-default port - they can name
different resources; it does NOT collapse them. It strips www prefixes,
host trailing dots, fragments, tracking parameters and trailing path
slashes; query parameters are order-insensitive (sorted); a bare flag
and an empty-valued flag both fold away ("?id" == "?id=");
percent-encodings of UNRESERVED characters decode ("%7E" == "~").
STATED EQUIVALENCE LIMITS: encoded RESERVED characters stay encoded
("%2F" is not "/"), and interior duplicate slashes stay distinct -
both can name different resources. A redirect or published canonical
relationship is the integration's to resolve BEFORE feeding urls here;
this module asserts identity only from the URL string.
"""

import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-._~")


def _decode_unreserved(s):
    """Decode percent-encodings of unreserved chars only (%7E -> ~);
    reserved-char encodings (%2F) stay put - they can be load-bearing."""
    def sub(m):
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else m.group(0).upper()
    return re.sub(r"%([0-9A-Fa-f]{2})", sub, s)

MAX_URL = 500

_TRACKING_PARAMS = re.compile(r'(?i)^(utm_|fbclid|gclid|mc_|ref$|ref_)')
_DEFAULT_PORTS = {"http": 80, "https": 443}


class UrlCanonError(Exception):
    pass


def canonical_url(url, max_url=MAX_URL, error=UrlCanonError):
    if not isinstance(url, str) or not url or len(url) > max_url:
        raise error("url missing or over %d chars" % max_url)
    try:
        p = urlparse(url.strip())
        host = p.hostname  # ValueError on malformed brackets
        port = p.port      # ValueError on malformed port
    except ValueError:
        raise error("malformed url")
    if p.scheme not in ("http", "https") or not host:
        raise error("url must be absolute http(s)")
    scheme = p.scheme.lower()
    host = host.lower()
    ipv6 = ":" in host
    if not ipv6:
        host = host.rstrip(".")  # "example.com." == "example.com"
        if host.startswith("www."):
            host = host[4:]
    if not host:
        raise error("empty host")
    if ipv6:
        host = "[%s]" % host  # brackets are required for a valid URL
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = "%s:%d" % (host, port)
    q = urlencode(sorted((k, v) for k, v in parse_qsl(p.query)
                         if not _TRACKING_PARAMS.match(k)))
    return urlunparse((scheme, host,
                       _decode_unreserved(p.path).rstrip("/"), "", q, ""))
