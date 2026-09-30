"""LinkedIn job-posting URL rule (an assumption to verify against real provider output).

Accepts /jobs/view/<id> and /jobs/view/<slug>-<id> on linkedin.com and its
subdomains. Nothing here makes a network request.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_JOB_PATH = re.compile(r"^/jobs/view/(?:[^/?#]*-)?(\d{6,})/?$")


def linkedin_job_id(url: str) -> str | None:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    m = _JOB_PATH.match(parts.path)
    return m.group(1) if m else None


def canonical_job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}"


def path_shape(url: str) -> str:
    """Coarse shape of a URL for reporting what kinds of pages come back."""
    parts = urlsplit(url)
    path = re.sub(r"\d{4,}", "<n>", parts.path)
    path = re.sub(r"/[^/]*-<n>", "/<slug>-<n>", path)
    segs = path.strip("/").split("/")[:3]
    return f"{(parts.hostname or '?').removeprefix('www.')}/" + "/".join(segs)
