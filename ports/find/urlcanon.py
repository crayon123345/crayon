"""Shared canonical URL identity for the find ports.

Single source of truth for URL identity across ports/find and
ports/find_track (Pratham round-1: one corrected canonicalizer, not
copies). Identity preserves scheme and non-default port - they can name
different resources; it does NOT collapse them. It strips www prefixes,
fragments, tracking parameters and trailing slashes, which are
presentation differences of the same resource. A redirect or published
canonical relationship is the integration's to resolve BEFORE feeding
urls here; this module asserts identity only from the URL string.
"""

import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

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
    if not ipv6 and host.startswith("www."):
        host = host[4:]
    if ipv6:
        host = "[%s]" % host  # brackets are required for a valid URL
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = "%s:%d" % (host, port)
    q = urlencode([(k, v) for k, v in parse_qsl(p.query)
                   if not _TRACKING_PARAMS.match(k)])
    return urlunparse((scheme, host, p.path.rstrip("/"), "", q, ""))
