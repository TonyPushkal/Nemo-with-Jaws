"""Phase 1 discovery: profile + lookback -> broadly related jobs with individual LinkedIn URLs.

Uses the tested SerpApi setup: Google Jobs with an explicit `location` (bare role as query) and
Google Search with `site:linkedin.com/jobs/view`. Candidates are NOT filtered by fit, seniority or
date; only clearly mismatched listing/link pairs are excluded. Dates are best-effort and labelled:
nothing here is a verified posting time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from nemo.consistency import check, is_clear_mismatch
from nemo.linkedin import canonical_job_url, linkedin_job_id

# ------------------------------------------------------------------ lookback

_LOOKBACK = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.I)
MIN_LOOKBACK, MAX_LOOKBACK = timedelta(hours=1), timedelta(days=30)


def parse_lookback(text: str) -> timedelta:
    m = _LOOKBACK.match(text or "")
    if not m:
        raise ValueError(f"lookback {text!r}: use a number with h, d or w, e.g. 24h, 7d, 2w")
    n, unit = int(m.group(1)), m.group(2).lower()
    td = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    if not MIN_LOOKBACK <= td <= MAX_LOOKBACK:
        raise ValueError(f"lookback {text!r} must be between 1h and 30d")
    return td


def google_freshness(lookback: timedelta) -> str:
    """Google's own freshness filter for Google Search (narrows the search; never posting evidence)."""
    if lookback <= timedelta(days=1):
        return "qdr:d"
    if lookback <= timedelta(weeks=1):
        return "qdr:w"
    return "qdr:m"


# ------------------------------------------------------------------ plan

@dataclass(frozen=True)
class Step:
    engine: str        # google_jobs | google
    role: str
    params: dict[str, str]


def city_name(location: str) -> str:
    return location.split(",")[0].strip()


def plan(roles: tuple[str, ...] | list[str], locations: list[str], lookback: timedelta, gl: str | None) -> list[Step]:
    """Round-robin by tier so a budget stop still covers every role:
    tier 1 Google Jobs @ first location, tier 2 Google Search site:, tier 3+ Google Jobs @ other locations."""
    common = {"hl": "en", **({"gl": gl} if gl else {})}
    cities = [city_name(l) for l in locations]
    tiers: list[list[Step]] = []
    if locations:
        tiers.append([Step("google_jobs", r, {"q": r, "location": locations[0], **common}) for r in roles])
    where = f" ({' OR '.join(cities)})" if cities else ""
    tiers.append([Step("google", r, {"q": f'site:linkedin.com/jobs/view "{r}"{where}', "num": "10",
                                     "tbs": google_freshness(lookback), **common}) for r in roles])
    for loc in locations[1:]:
        tiers.append([Step("google_jobs", r, {"q": r, "location": loc, **common}) for r in roles])
    return [s for t in tiers for s in t]


# ------------------------------------------------------------------ dates (best effort, labelled)

_REL = re.compile(r"^\s*(\d+)(\+)?\s*(second|minute|hour|day|week|month|year)s?\s+ago\s*$", re.I)
_UNIT = {"second": (1, 1), "minute": (60, 60), "hour": (3600, 3600), "day": (86400, 86400),
         "week": (604800, 604800), "month": (28 * 86400, 31 * 86400), "year": (365 * 86400, 366 * 86400)}
_ABS_FORMATS = ("%d %b %Y", "%b %d, %Y", "%d %B %Y", "%B %d, %Y")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def estimate_interval(raw: str, reference: datetime | None) -> tuple[datetime | None, datetime | None, str]:
    """Interval the posting time may lie in, widened for rounding / unknown time zone. (None, None) if unusable."""
    m = _REL.match(raw or "")
    if m:
        if reference is None:
            return None, None, "relative phrase without a reference time"
        n, plus, unit = int(m.group(1)), bool(m.group(2)), m.group(3).lower()
        lo, hi = _UNIT[unit]
        latest = reference - timedelta(seconds=max(n - 1, 0) * lo)
        earliest = None if plus else reference - timedelta(seconds=(n + 1) * hi)
        return earliest, latest, f"relative '{raw}' against request time; ±1 {unit} for rounding"
    for fmt in _ABS_FORMATS:
        try:
            d = datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
            return d - timedelta(hours=14), d + timedelta(days=1, hours=12), "date only; time zone unknown (±UTC offsets)"
        except ValueError:
            continue
    return None, None, "unrecognised date text"


def lookback_status(earliest: datetime | None, latest: datetime | None, window_start: datetime,
                    window_end: datetime) -> str:
    """Best-effort, from UNVERIFIED reported dates: within / outside / ambiguous / unknown."""
    if latest is None:
        return "unknown"
    if latest < window_start:
        return "reported_outside"
    if earliest is not None and earliest >= window_start and latest <= window_end + timedelta(minutes=5):
        return "reported_within"
    return "ambiguous"


_AGO_EXT = re.compile(r"^\s*\d+\+?\s*(?:second|minute|hour|day|week|month|year)s?\s+ago\s*$", re.I)


def date_report(raw: str, source: str, basis: str, request_time: str | None, via: str | None,
                window: tuple[datetime, datetime]) -> dict[str, Any]:
    earliest, latest, note = estimate_interval(raw, parse_time(request_time))
    return {"raw": raw, "source": source, "basis": basis, "via": via, "request_time": request_time,
            "estimated_earliest": earliest.isoformat() if earliest else None,
            "estimated_latest": latest.isoformat() if latest else None, "note": note,
            "lookback_status": lookback_status(earliest, latest, *window), "verified": False}


# ------------------------------------------------------------------ parse responses

def request_time(raw: dict) -> str | None:
    md = raw.get("search_metadata") or {}
    return md.get("processed_at") or md.get("created_at")


@dataclass
class Sighting:
    job_id: str
    engine: str
    query: str
    location_param: str | None
    title: str | None
    company: str | None
    location: str | None
    description: str
    via: str | None
    returned_url: str
    date_reports: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def parse_google_jobs(raw: dict, window: tuple[datetime, datetime]) -> tuple[list[Sighting], list[dict], int]:
    """(kept, excluded_mismatches, not_linkedin_count)."""
    sp, rt = raw.get("search_parameters") or {}, request_time(raw)
    kept, excluded, not_linkedin = [], [], 0
    for j in raw.get("jobs_results") or []:
        link = next((o.get("link") for o in j.get("apply_options") or [] if linkedin_job_id(o.get("link", ""))), None)
        if not link:
            not_linkedin += 1
            continue
        jid = linkedin_job_id(link)
        problems = check(j, link)
        if is_clear_mismatch(problems):
            excluded.append({"linkedin_job_id": jid, "returned_url": link, "title": j.get("title"),
                             "company": j.get("company_name"), "via": j.get("via"), "problems": problems})
            continue
        de = j.get("detected_extensions") or {}
        age, src = de.get("posted_at"), "google_jobs.detected_extensions.posted_at"
        if not age:
            age = next((e for e in j.get("extensions") or [] if isinstance(e, str) and _AGO_EXT.match(e)), None)
            src = "google_jobs.extensions"
        reports = [date_report(age, src, "aggregator_reported", rt, j.get("via"), window)] if age else []
        kept.append(Sighting(jid, "google_jobs", sp.get("q", ""), sp.get("location"), j.get("title"),
                             j.get("company_name"), j.get("location"), j.get("description") or "", j.get("via"),
                             link, reports, problems))
    return kept, excluded, not_linkedin


def _split_google_title(title: str) -> tuple[str | None, str | None]:
    """LinkedIn page titles in Google look like 'Company hiring Title in City' or 'Title at Company …'."""
    t = re.sub(r"\s*(\| LinkedIn|- Jobs|…|\.\.\.)\s*$", "", title or "").strip()
    m = re.match(r"^(.+?) hiring (.+?)(?: in .+)?$", t)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    m = re.match(r"^(.+?) at (.+?)(?: —.*| - .*)?$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return t or None, None


def parse_google(raw: dict, window: tuple[datetime, datetime]) -> tuple[list[Sighting], int]:
    sp, rt = raw.get("search_parameters") or {}, request_time(raw)
    kept, other = [], 0
    for r in raw.get("organic_results") or []:
        jid = linkedin_job_id(r.get("link", ""))
        if not jid:
            other += 1
            continue
        title, company = _split_google_title(r.get("title", ""))
        reports = [date_report(r["date"], "google.organic_results.date", "search_engine_date", rt, "Google Search", window)] \
            if r.get("date") else []
        kept.append(Sighting(jid, "google", sp.get("q", ""), None, title, company, None, r.get("snippet") or "",
                             "Google Search (LinkedIn page)", r.get("link", ""), reports))
    return kept, other


# ------------------------------------------------------------------ merge by LinkedIn job id

_BASIS_RANK = {"aggregator_reported": 0, "search_engine_date": 1}


def merge(sightings: list[Sighting]) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    for s in sightings:
        j = jobs.setdefault(s.job_id, {"linkedin_job_id": s.job_id, "url": canonical_job_url(s.job_id),
                                       "title": None, "company": None, "location": None, "description": "",
                                       "sources": [], "reported_ages": [], "flags": []})
        for k in ("title", "company", "location"):
            if not j[k] and getattr(s, k):
                j[k] = getattr(s, k)
        if len(s.description) > len(j["description"]):
            j["description"] = s.description
        j["sources"].append({"engine": s.engine, "query": s.query, "location_param": s.location_param, "via": s.via,
                             "returned_url": s.returned_url})
        j["reported_ages"] += s.date_reports
        j["flags"] += [f for f in s.flags if f not in j["flags"]]
    for j in jobs.values():
        # Prefer a Google Jobs time from a LinkedIn listing, then other aggregator times, then Google's date.
        ranked = sorted(j["reported_ages"], key=lambda d: (_BASIS_RANK.get(d["basis"], 9),
                                                           (d.get("via") or "").casefold() != "linkedin"))
        j["best_reported_age"] = ranked[0] if ranked else None
        j["lookback_status"] = ranked[0]["lookback_status"] if ranked else "unknown"
        n = len(j["description"])
        j["description_level"] = "none" if n == 0 else "snippet" if n < 500 else "description"
    return jobs
