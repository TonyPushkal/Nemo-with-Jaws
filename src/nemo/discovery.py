"""Phase 1 discovery: profile + lookback -> broadly related jobs with individual LinkedIn URLs.

Uses the tested SerpApi setup: Google Jobs with an explicit `location` (bare role as query) and
Google Search with `site:linkedin.com/jobs/view`. Candidates are NOT filtered by fit, seniority or
date; only clearly mismatched listing/link pairs are excluded. Dates are best-effort and labelled:
nothing here is a verified posting time.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

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


# ------------------------------------------------------------------ application links and job identity

# Hosts that are job boards / aggregators, not the employer (substring match on the host).
JOB_BOARDS = ("indeed.", "glassdoor.", "simplyhired.", "bebee.", "jooble.", "shine.com", "naukri", "foundit.", "monster",
              "ziprecruiter.", "jobrapido.", "trabajo.", "expertini.", "jobleads.", "olibr.", "levels.fyi", "swooped.",
              "getmereferred.", "jobaaj.", "internshala.", "myinternships.", "remoterush.", "mysmartpros.", "learn4good.",
              "talent.com", "adzuna.", "careerjet.", "hirist.", "instahyre.", "wellfound.", "bharatdigital.", "joindevops.",
              "canarywharfian.", "startup.jobs", "bayt.", "jobtensor.", "jobsora.", "recruit.net", "cutshort.", "peerlist.",
              "airtribe.", "mployee.", "jobsinhyd.", "efinancialcareers.", "carrerlift.", "hirecentric.", "jeevikaindia.",
              "jobringer.", "hackajob.", "kula.", "jobgether.", "flexjobs.", "ambitionbox.", "apna.co", "freshersworld.",
              "geekwall.", "workindia.", "jobsora.", "whatjobs.", "lensa.", "tealhq.", "builtin.")
# Applicant-tracking-system hosts: the page is the employer's own application page.
ATS_HOSTS = ("greenhouse.io", "lever.co", "myworkdayjobs.com", "workdayjobs.com", "smartrecruiters.com", "ashbyhq.com",
             "icims.com", "jobvite.com", "taleo.net", "successfactors.", "bamboohr.com", "recruitee.com", "workable.com",
             "breezy.hr", "zohorecruit.", "freshteam.com", "darwinbox.", "join.com", "personio.", "teamtailor.com",
             "pinpointhq.com", "oraclecloud.com", "keka.com", "kekahire.", "peoplestrong.")
LINK_KINDS = ("employer_ats", "linkedin", "job_board", "unknown")


def _company_tokens(company: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (company or "").casefold())
            if len(t) >= 4 and t not in {"india", "private", "limited", "technologies", "solutions", "software", "global",
                                         "services", "systems", "group", "inc"}]


def _registrable_label(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else host


# Query parameters known to be tracking only. Everything else is kept: it may carry the job id (talent.com ?id=, indeed ?jk=, ...).
_KNOWN_TRACKING = re.compile(r"^(utm_.*|gclid|dclid|fbclid|msclkid|yclid|igshid|mc_cid|mc_eid|_ga|_gl|trk|trackingid|refid)$", re.I)


def normalize_url(url: str) -> str:
    """Identity form of a URL: lower-case host without 'www.', path without trailing '/', and the query string minus KNOWN
    tracking parameters (unknown parameters are retained, sorted). Fragment dropped."""
    p = urlsplit(url.strip())
    query = sorted((k.lower(), v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _KNOWN_TRACKING.match(k))
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}" + (f"?{urlencode(query)}" if query else "")


def _legacy_normalize_url(url: str) -> str:
    """Pre-fix identity form (query string dropped). Kept only to recognise keys stored before the fix."""
    p = urlsplit(url.strip())
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}"


def classify_option(company: str, via: str | None, option: dict) -> str:
    """employer_ats | linkedin | job_board | unknown. 'employer_ats' only when established: an ATS host, or a host whose
    registrable domain is the company's own name. Job boards are recognised by host; anything else is 'unknown'."""
    link = option.get("link") or ""
    host = (urlsplit(link).hostname or "").lower()
    if linkedin_job_id(link) or "linkedin." in host:
        return "linkedin"
    if any(b in host for b in JOB_BOARDS):
        return "job_board"
    if any(a in host for a in ATS_HOSTS):
        return "employer_ats"
    label, squashed = _registrable_label(host), re.sub(r"[^a-z0-9]", "", (company or "").casefold())
    if len(label) >= 2 and squashed and (label == squashed or (len(label) >= 4 and (label in squashed or squashed in label))
                                         or any(t in label for t in _company_tokens(company))):
        return "employer_ats"
    return "unknown"


def choose_application_link(company: str, via: str | None, options: list[dict]) -> dict | None:
    """Employer/ATS link when established; else the LinkedIn link; else the listing source's own link; else the first
    option. `kind` says what the chosen link is (employer_ats, linkedin, job_board, unknown)."""
    opts = [o for o in options if o.get("link")]
    if not opts:
        return None
    kinds = [(classify_option(company, via, o), o) for o in opts]
    pick = (next(((k, o) for k, o in kinds if k == "employer_ats"), None)
            or next(((k, o) for k, o in kinds if k == "linkedin"), None)
            or next(((k, o) for k, o in kinds if via and (o.get("title") or "").casefold() == via.casefold()), None)
            or kinds[0])
    return {"url": pick[1]["link"], "title": pick[1].get("title"), "kind": pick[0]}


_TRACKING_PARAMS = re.compile(r"^(utm_|gclid|fbclid|ref$|src$|source$|trk|campaign|medium)", re.I)


_GENERIC_LAST_SEGMENT = {"view", "jobs", "job", "careers", "career", "search", "apply", "openings", "vacancies", "details",
                         "listing", "job-listing", "jv", "viewjob", "positions", "opportunities"}


def application_url_status(url: str) -> tuple[str, str]:
    """('structurally_valid'|'needs_review', reason): a check of the URL's shape only. Structurally valid means it appears to
    identify one job (an id-like query parameter, or a path ending in a specific id/slug). The destination is NOT fetched, so
    it may still be closed, expired or wrong. Roots and bare '/view', '/jobs', '/careers' pages need review."""
    p = urlsplit((url or "").strip())
    if not p.hostname:
        return "needs_review", "no usable URL"
    if any(len(v) >= 5 for k, v in parse_qsl(p.query) if not _TRACKING_PARAMS.match(k)):
        return "structurally_valid", "identifier in the query string"
    segs = [x for x in p.path.split("/") if x]
    last = segs[-1].lower() if segs else ""
    where = f"{p.hostname}{p.path or '/'}"
    specific = any((len(x) >= 6 and re.search(r"\d", x)) or (len(x) >= 18 and "-" in x) for x in segs[:-1] or segs)
    if not segs or (last in _GENERIC_LAST_SEGMENT and not specific):         # '/…/<id>/apply' is fine, '/view' is not
        return "needs_review", f"generic or incomplete URL: no job identifier in {where}"
    if len(segs) == 1 and not (re.search(r"\d", last) or ("-" in last and len(last) >= 8)):
        return "needs_review", f"generic or incomplete URL: no job identifier in {where}"
    return "structurally_valid", "identifier or slug in the path"


def job_key(linkedin_id: str | None, application: dict | None, google_job_id: str | None, *, legacy: bool = False) -> str | None:
    """Stable identity: LinkedIn job id when a consistency-checked LinkedIn link exists (not independently verified); else a
    hash of the primary application URL (see normalize_url: job-id query parameters are part of the identity); else Google's
    own job id. Never title/company based. `legacy=True` gives the key as generated before the query-string fix."""
    if linkedin_id:
        return linkedin_id
    if application:
        norm = _legacy_normalize_url if legacy else normalize_url
        return "url:" + hashlib.sha1(norm(application["url"]).encode()).hexdigest()[:16]
    if google_job_id:
        return "gj:" + hashlib.sha1(google_job_id.encode()).hexdigest()[:16]
    return None


@dataclass
class Sighting:
    job_id: str                      # job key (see job_key)
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
    linkedin_job_id: str | None = None
    application: dict | None = None
    apply_options: list[dict] = field(default_factory=list)
    legacy_id: str | None = None     # key as generated before the query-string fix (history compatibility)


def parse_google_jobs(raw: dict, window: tuple[datetime, datetime]) -> tuple[list[Sighting], list[dict], int, int]:
    """(kept, rejected_linkedin_links, kept_without_linkedin_count, skipped_without_identity). Jobs are kept with or
    without a LinkedIn link; a LinkedIn link that clearly disagrees with the listing is rejected and recorded. Jobs with
    no application link and no Google job id cannot be identified and are skipped (and counted)."""
    sp, rt = raw.get("search_parameters") or {}, request_time(raw)
    kept, rejected, no_li, unidentifiable = [], [], 0, 0
    for j in raw.get("jobs_results") or []:
        opts = [{"title": o.get("title"), "link": o.get("link")} for o in j.get("apply_options") or [] if o.get("link")]
        li_link = next((o["link"] for o in opts if linkedin_job_id(o["link"])), None)
        flags: list[str] = []
        problems = check(j, li_link) if li_link else []
        if li_link and is_clear_mismatch(problems):
            rejected.append({"linkedin_job_id": linkedin_job_id(li_link), "returned_url": li_link, "title": j.get("title"),
                             "company": j.get("company_name"), "via": j.get("via"), "problems": problems})
            flags.append("LinkedIn link rejected (does not match listing): " + "; ".join(problems))
            opts = [o for o in opts if o["link"] != li_link]
            li_link = None
        elif li_link:
            flags += problems
        li_id = linkedin_job_id(li_link) if li_link else None
        app = choose_application_link(j.get("company_name") or "", j.get("via"), opts)
        key = job_key(li_id, app, j.get("job_id"))
        if not key:
            unidentifiable += 1
            continue
        if not li_id:
            no_li += 1
        de = j.get("detected_extensions") or {}
        age, src = de.get("posted_at"), "google_jobs.detected_extensions.posted_at"
        if not age:
            age = next((e for e in j.get("extensions") or [] if isinstance(e, str) and _AGO_EXT.match(e)), None)
            src = "google_jobs.extensions"
        reports = [date_report(age, src, "aggregator_reported", rt, j.get("via"), window)] if age else []
        returned = li_link or (app["url"] if app else "")
        kept.append(Sighting(key, "google_jobs", sp.get("q", ""), sp.get("location_requested") or sp.get("location"), j.get("title"),
                             j.get("company_name"), j.get("location"), j.get("description") or "", j.get("via"),
                             returned, reports, flags, li_id, app, opts,
                             job_key(li_id, app, j.get("job_id"), legacy=True)))
    return kept, rejected, no_li, unidentifiable


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
        link = r.get("link", "")
        kept.append(Sighting(jid, "google", sp.get("q", ""), None, title, company, None, r.get("snippet") or "",
                             "Google Search (LinkedIn page)", link, reports, [], jid,
                             {"url": link, "title": "LinkedIn", "kind": "linkedin"}, [{"title": "LinkedIn", "link": link}]))
    return kept, other


# ------------------------------------------------------------------ merge by job key

_BASIS_RANK = {"aggregator_reported": 0, "search_engine_date": 1}


def merge(sightings: list[Sighting]) -> dict[str, dict[str, Any]]:
    """One record per job key. Keeps every source, every application option, and the actual description source."""
    jobs: dict[str, dict[str, Any]] = {}
    for s in sightings:
        j = jobs.setdefault(s.job_id, {"job_key": s.job_id, "linkedin_job_id": None, "url": "", "linkedin_url": None,
                                       "application_url": None, "application_kind": None, "application_options": [],
                                       "linkedin_link_status": None, "legacy_key": None,
                                       "title": None, "company": None, "location": None, "description": "",
                                       "description_source": None, "sources": [], "reported_ages": [], "flags": []})
        for k in ("title", "company", "location"):
            if not j[k] and getattr(s, k):
                j[k] = getattr(s, k)
        if len(s.description) > len(j["description"]):
            j["description"], j["description_source"] = s.description, s.via
        if s.legacy_id and s.legacy_id != s.job_id:
            j["legacy_key"] = s.legacy_id
        if s.linkedin_job_id:
            j["linkedin_job_id"], j["linkedin_url"] = s.linkedin_job_id, canonical_job_url(s.linkedin_job_id)
            j["linkedin_link_status"] = "consistency_checked"      # matched the listing; the posting itself was not fetched
        seen = {normalize_url(o["link"]) for o in j["application_options"]}
        j["application_options"] += [o for o in s.apply_options if normalize_url(o["link"]) not in seen]
        j["sources"].append({"engine": s.engine, "query": s.query, "location_param": s.location_param, "via": s.via,
                             "returned_url": s.returned_url})
        j["reported_ages"] += s.date_reports
        j["flags"] += [f for f in s.flags if f not in j["flags"]]
    for j in jobs.values():
        app = choose_application_link(j["company"] or "", j["description_source"], j["application_options"])
        if app:
            j["application_url"], j["application_kind"] = app["url"], app["kind"]
        j["url"] = j["linkedin_url"] or j["application_url"] or ""
    for j in jobs.values():
        # Prefer a Google Jobs time from a LinkedIn listing, then other aggregator times, then Google's date.
        ranked = sorted(j["reported_ages"], key=lambda d: (_BASIS_RANK.get(d["basis"], 9),
                                                           (d.get("via") or "").casefold() != "linkedin"))
        j["best_reported_age"] = ranked[0] if ranked else None
        j["lookback_status"] = ranked[0]["lookback_status"] if ranked else "unknown"
        n = len(j["description"])
        j["description_level"] = "none" if n == 0 else "snippet" if n < 500 else "description"
    return jobs
