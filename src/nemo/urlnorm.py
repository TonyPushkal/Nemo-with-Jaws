"""URL canonicalisation for dedup. Pure and offline."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = {"gclid", "fbclid", "mc_eid", "ref", "referrer", "refid", "trk", "trackingid"}


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_EXACT
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host + port, path, urlencode(query), ""))


def is_linkedin(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")
