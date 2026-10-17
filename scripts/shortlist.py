#!/usr/bin/env python3
"""SDE/backend shortlist from SAVED runs (rules only; no network, no model, originals untouched).

    .venv/bin/python scripts/shortlist.py [--profile job-profile.md] [--runs runs] [--out runs]

Merges every saved run, screens each job, drops Visakhapatnam-located jobs and writes runs/shortlist-<UTC>.md.
Duplicates: records are merged ONLY when they share a posting identifier (the same normalised application URL or LinkedIn job id);
records that merely share company + title + city stay separate and are labelled "possible duplicate". Every record and link is kept.
Internships are listed apart; employment type is shown explicitly (Google-reported, from the description, or "not stated").
Backend-role fit and required-stack fit are shown separately. Rule-based labels only: nothing here is human-reviewed.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from screen_results import load_saved, provenance  # noqa: E402

from nemo.discovery import _SCHEDULE_EXT, choose_application_link, freshness_at, normalize_url, parse_lookback  # noqa: E402
from nemo.profile import ProfileError, load_profile  # noqa: E402
from nemo.queryplan import load_config  # noqa: E402
from nemo.runreport import reported_age, screen_run  # noqa: E402

_RESP_START = re.compile(r"(design|build|develop|implement|own|maintain|collaborate|write|deploy|work|participate|create|ensure|optimi[sz]e|contribute)\b", re.I)


def vacancy_key(job: dict) -> tuple[str, str, str]:
    """Company + title + city. Evidence of a POSSIBLE duplicate only: never used to merge."""
    norm = lambda t: re.sub(r"[^a-z0-9]+", " ", (t or "").casefold()).strip()
    city = norm((job.get("location") or "").split(",")[0])
    title = re.sub(r"\b(hiring|jobs?|at|in|for)\b", " ", norm(job.get("title")))
    for token in (norm(job.get("company")), city):
        title = title.replace(token, " ") if token else title
    return norm(job.get("company")), " ".join(title.split()), city


def identifiers(job: dict) -> set[str]:
    """Posting identifiers that justify merging: any application URL (job-id query parameters kept) or a LinkedIn job id."""
    ids = {"url:" + normalize_url(o["link"]) for o in job.get("application_options") or [] if o.get("link")}
    if job.get("application_url"):
        ids.add("url:" + normalize_url(job["application_url"]))
    if job.get("linkedin_job_id"):
        ids.add("li:" + job["linkedin_job_id"])
    return ids


def schedule_map(runs: Path) -> dict[str, str]:
    """Employment type as reported by Google Jobs, from the saved raw responses, keyed by normalised application URL."""
    out: dict[str, str] = {}
    for f in glob.glob(str(runs / "*" / "raw" / "*.json")):
        for j in json.loads(Path(f).read_text()).get("jobs_results") or []:
            sched = next((e for e in j.get("extensions") or [] if isinstance(e, str) and _SCHEDULE_EXT.match(e.strip())), None)
            if sched:
                for o in j.get("apply_options") or []:
                    if o.get("link"):
                        out[normalize_url(o["link"])] = sched
    return out


def responsibilities_excerpt(desc: str, limit: int = 170) -> str:
    """Verbatim text describing the work: first responsibility sentences, else text after a 'Responsibilities' header, else the
    sentence that says what the team is hiring someone to do."""
    d = desc or ""
    out = [x.strip()[:limit] for x in re.split(r"(?<=[.!?])\s+|(?<=[a-z0-9][.!?])(?=[A-Z])|\n|•|;", d) if _RESP_START.match(x.strip())]
    if out:
        return " / ".join(out[:2])
    m = re.search(r"(responsibilit(?:y|ies)|what you.?ll (?:do|work on)|you will)\b[^\n:]*:?\s*", d, re.I)
    if m and d[m.end(): m.end() + 40].strip():
        return re.sub(r"\s+", " ", d[m.end(): m.end() + limit * 2]).strip()
    m = re.search(r"[^.\n]*(looking for|responsible for|to develop|to build|will be building)[^.\n]*[.\n]", d, re.I)
    return re.sub(r"\s+", " ", m.group(0)).strip()[:limit * 2] if m else "(no responsibility sentences found)"


def employment_label(s) -> str:
    sched, e = s.job.get("schedule_type"), s.employment
    if e == "internship":
        said = re.search(r"\bintern(ship)?\b", f"{s.job.get('title') or ''} {(s.job.get('description') or '')[:300]}", re.I)
        if said:
            return "internship (stated in the title or description" + (f"; Google lists: {sched}" if sched and "intern" not in sched.lower() else "") + ")"
        return "internship (Google-reported only; the title and description do not say so: confirm)"
    if e == "contract":
        return "contract (stated)"
    if e == "full_time":
        return f"full-time (Google-reported: {sched}; unverified)"
    return "NOT STATED (no source gives the employment type)"


FRESH_LABEL = {"confirmed_within": "confirmed within the max age (Google's report, unverified)", "unconfirmed": "age UNCONFIRMED (unknown or too coarse)",
               "older": "OLDER than the max age (not fresh)"}


def record(s, group_note: str, max_age=None) -> list[str]:
    j = s.job
    req = [e for e in s.experience.excerpts if e["kind"] == "required"]
    exp = "; ".join(f"{e['years'].split(' (')[0]}: \"{e['text'][:110]}\"" for e in req[:2]) or "not stated"
    unc = [g for g in s.gaps if g.startswith(("unknown", "REVIEW", "BORDERLINE", "gap", "THIN", "PARTIAL", "STACK", "internship", "contract"))]
    if "OUTSIDE" in reported_age(j):
        unc.append(f"posting reported older than the look-back ({reported_age(j)})")
    be = next((r.replace("backend responsibilities: ", "").split("; ")[0] for r in s.reasons if r.startswith("backend responsibilities")), "-")
    options = [o["link"] for o in (j.get("application_options") or []) if o.get("link") and o["link"] != j.get("application_url")]
    return [f"### {j['title']} · {j['company']}",
            f"- **Location:** {j['location']} · **Employment type:** {employment_label(s)} · reported age: {reported_age(j)}"
            + (f" · **Freshness:** {FRESH_LABEL[freshness_at(j, max_age)]}" if max_age else ""),
            f"- **Backend-role fit:** {s.match}{' · REVIEW' if s.review else ''}{' · BORDERLINE' if s.borderline else ''} (discipline: {s.discipline}) · "
            f"**Required-stack fit:** {s.stack_fit.upper()} ({s.stack_note}) · **Human-reviewed:** not yet",
            f"- **Required experience:** {exp}",
            f"- **Relevant responsibilities:** {be}. Description says: {responsibilities_excerpt(j.get('description'))}",
            f"- **Description:** {s.readiness} ({len(j.get('description') or '')} chars; from {provenance(j)})",
            f"- **Uncertainties:** {'; '.join(u[:220] for u in unc) or 'none identified by the rules'}",
            f"- **Apply:** {j.get('application_url')} ({j.get('application_kind')}; link checked for structure only)"
            + ("".join(f"\n  - also listed: {o}" for o in options)),
            *([group_note] if group_note else []), ""]


def merge_by_identifier(rows: list) -> list[list]:
    """Union-find over shared posting identifiers. Returns groups of records that are confidently the same vacancy."""
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    seen: dict[str, int] = {}
    for i, s in enumerate(rows):
        for ident in identifiers(s.job):
            if ident in seen:
                parent[find(i)] = find(seen[ident])
            else:
                seen[ident] = i
    groups: dict[int, list] = {}
    for i, s in enumerate(rows):
        groups.setdefault(find(i), []).append(s)
    return list(groups.values())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="job-profile.md")
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--max-age", default="24h", help="posting age used for the freshness label (as of each run's own collection time)")
    args = ap.parse_args(argv)
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    jobs, _ = load_saved(Path(args.runs))
    sched = schedule_map(Path(args.runs))
    for j in jobs.values():
        if j.get("application_options"):
            app = choose_application_link(j.get("company") or "", j.get("description_source"), j["application_options"])
            if app:
                j["application_url"], j["application_kind"] = app["url"], app["kind"]
        j.setdefault("linkedin_url", j["url"] if j.get("linkedin_job_id") else None)
        if not j.get("schedule_type"):
            j["schedule_type"] = next((sched[normalize_url(o["link"])] for o in j.get("application_options") or []
                                       if o.get("link") and normalize_url(o["link"]) in sched), None)
    cities = [c.split(",")[0] for c in load_config().get("default_locations", [])]
    ok, _ = screen_run(jobs, profile, cities)
    max_age = parse_lookback(args.max_age)
    pool = [s for s in ok if s.match in ("strong", "possible") and not re.search(r"visakh|vizag", s.job.get("location") or "", re.I)]
    merged = []
    for group in merge_by_identifier(pool):                 # confident merges only: shared posting identifier
        head = group[0]
        for other in group[1:]:
            head.job["application_options"] = (head.job.get("application_options") or []) + (other.job.get("application_options") or [])
        merged.append((head, len(group)))
    by_vacancy: dict[tuple, list] = {}
    for s, _ in merged:
        by_vacancy.setdefault(vacancy_key(s.job), []).append(s)

    def note(s, n):
        bits = []
        if n > 1:
            bits.append(f"merged {n} records that share a posting identifier")
        twins = [t for t in by_vacancy[vacancy_key(s.job)] if t is not s]
        if twins:
            bits.append("POSSIBLE DUPLICATE (same company, title and city; no shared posting identifier, so NOT merged): "
                        + " | ".join(f"{t.job.get('application_url')}" for t in twins))
        return ("- " + "; ".join(bits)) if bits else ""

    def section(title, rows):
        out = [f"## {title} ({len(rows)})", ""]
        for s, n in rows:
            out += record(s, note(s, n), max_age)
        return out
    clear = [(s, n) for s, n in merged if s.discipline == "backend" and not s.review]
    review = [(s, n) for s, n in merged if s.discipline != "backend" or s.review]
    full = [(s, n) for s, n in clear if s.employment not in ("internship", "contract")]
    intern = [(s, n) for s, n in clear if s.employment == "internship"]
    other = [(s, n) for s, n in clear if s.employment == "contract"]
    unknown = sum(1 for s, _ in full if s.employment == "unspecified")
    L = [f"# SDE / backend shortlist from saved runs ({len(jobs)} unique jobs, {len(ok)} eligible, {len(merged)} records after merging shared-identifier duplicates)", "",
         "Rule-based labels only; nothing here is human-reviewed. Descriptions, dates, employment types and links are Google's. The description "
         "check is a content heuristic (responsibilities and requirements present), not proof of completeness. Application links are checked "
         "for structure only, not opened. Possible duplicates are flagged, never merged without a shared posting identifier.", ""]
    L += section(f"Full-time or employment type not stated ({unknown} not stated)", full) + section("Internships (kept eligible; you prefer full-time)", intern)
    L += section("Contract roles", other) + section("Ambiguous or mixed roles: review before relying on them", review)
    out = Path(args.out) / f"shortlist-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
    out.write_text("\n".join(L) + "\n")
    print(f"{len(merged)} records ({len(full)} full-time/not stated [{unknown} not stated], {len(intern)} internships, {len(other)} contract, {len(review)} to review)\n{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
