"""Benchmark report for one search run: results per query, unique jobs, screening outcome, application links.

Pure formatting plus screening of this run's jobs (rules only). It reports what was returned; it draws no conclusion
about whether relevance improved: that needs your review of the actual jobs.
"""

from __future__ import annotations

import collections
from typing import Any

from nemo.profile import JobProfile
from nemo.screening import Screened, rank, screen


def exclusion_category(reason: str) -> str:
    for prefix, name in (("requires a minimum", "experience requirement above your limit"), ("senior", "senior/lead/staff/manager title"),
                         ("non-software", "non-software role"), ("quality role", "quality role with non-software signals"),
                         ("location", "outside India")):
        if reason.startswith(prefix):
            return name
    return "other"


def screen_run(jobs: dict[str, dict], profile: JobProfile) -> tuple[list[Screened], list[Screened]]:
    return rank([screen(j, profile) for j in jobs.values()])


def cell(x: Any) -> str:
    """Markdown table cell: escape pipes, flatten newlines."""
    return str("" if x is None else x).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


AGE_STATUS = {"reported_within": "within the look-back (reported)", "reported_outside": "OUTSIDE the look-back (reported)",
              "ambiguous": "ambiguous vs look-back", "unknown": "date unknown"}


def reported_age(j: dict) -> str:
    """Google's reported age, labelled unverified; unknown dates are stated, never guessed. Display only: no date exclusion."""
    raw = (j.get("best_reported_age") or {}).get("raw")
    return f"{raw} ({AGE_STATUS.get(j.get('lookback_status') or 'unknown', 'date unknown')})" if raw else "date unknown"


def job_record(s: Screened) -> dict[str, Any]:
    j = s.job
    return {"job_key": j["job_key"], "title": j["title"], "company": j["company"], "location": j["location"],
            "eligible": s.eligible, "match": s.match, "readiness": s.readiness, "discipline": s.discipline, "application_status": s.application_status,
            "review": s.review, "borderline": s.borderline, "fit": s.fit,
            "confidence": s.confidence, "experience_required": s.experience.required_mentions,
            "experience_preferred": s.experience.preferred_mentions, "experience_excerpts": s.experience.excerpts,
            "experience_alternatives": s.experience.alternatives, "gap_full_time_months": s.gap_full_time_months,
            "gap_total_months": s.gap_total_months, "employment": s.employment, "reasons": s.reasons,
            "gaps_and_unknowns": s.gaps, "exclusions": s.exclusions, "application_link": j.get("application_url"),
            "application_kind": j.get("application_kind"), "all_application_links": j.get("application_options"),
            "linkedin_url": j.get("linkedin_url"), "linkedin_link_status": j.get("linkedin_link_status"),
            "description_source": j.get("description_source"), "description_chars": len(j.get("description") or ""),
            "reported_age": reported_age(j)}


def _row(s: Screened) -> str:
    j = s.job
    tag = s.match + (" · REVIEW" if s.review else "") + (" · BORDERLINE" if s.borderline else "")
    return (f"| {cell(tag)} | {cell(s.discipline)} | {cell(j['title'])} | {cell(j['company'])} | {cell(j['location'])} | "
            f"{cell(', '.join(s.experience.required_mentions) or 'not stated')} | {cell(reported_age(j))} | "
            f"{cell(j.get('application_url') or '-')} ({cell(j.get('application_kind') or '-')}{'; LINK NEEDS REVIEW' if s.application_status != 'structurally_valid' else '; link structurally valid'}) | "
            f"{cell(j.get('linkedin_url') or 'none')} | {cell(j.get('description_source') or '-')} ({len(j.get('description') or '')} chars) |")


HEADER = ["| Match | Discipline | Job | Company | Location | Required experience | Reported age (Google, unverified) | "
          "Application link (kind) | LinkedIn (consistency-checked) | Description source |", "|---|---|---|---|---|---|---|---|---|---|"]


def screening_sections(ok: list[Screened], out: list[Screened]) -> list[str]:
    levels = collections.Counter(s.match for s in ok)
    sufficient = [s for s in ok if s.readiness == "length_ok"]
    incomplete = [s for s in ok if s.readiness == "short"]
    L = ["## Screening (rules only)", "",
         f"- Eligible: **{len(ok)}** (strong {levels['strong']}, possible {levels['possible']}, adjacent {levels['adjacent']}, "
         f"weak {levels['weak']}); borderline {sum(s.borderline for s in ok)}, needs review {sum(s.review for s in ok)}; "
         f"description 500+ chars {len(sufficient)} (completeness not verified), short {len(incomplete)}",
         f"- Excluded: **{len(out)}** (kept with reasons in screened.json)"]
    cats = collections.Counter(exclusion_category(x) for s in out for x in s.exclusions)
    L += [f"  - {name}: {n}" for name, n in cats.most_common()]
    L += ["", "## Eligible jobs: description 500+ characters (length only; completeness not verified)", ""] + HEADER + [_row(s) for s in sufficient]
    L += ["", "## Eligible jobs: short description (<500 chars), needs more information (no confident match label)", ""] + HEADER + [_row(s) for s in incomplete]
    L += ["", "## Experience statements (exact JD excerpts)", ""]
    for s in ok:
        for e in s.experience.excerpts:
            L.append(f"- **{cell(s.job['title'])}** · {cell(s.job['company'])} · {e['kind']} · {e['years']}: \"{cell(e['text'])}\"")
    L += ["", "## Excluded jobs", ""]
    for s in out:
        L.append(f"- {cell(s.job['title'])} · {cell(s.job['company'])} · {cell(s.job['location'])} · {cell(reported_age(s.job))}: "
                 f"{'; '.join(s.exclusions)}")
    return L


def render(meta: dict, query_rows: list[dict], ok: list[Screened], out: list[Screened], total_jobs: int) -> str:
    L = [f"# Benchmark report: run {meta['run_id']}", "",
         f"- Status: **{meta['status']}**; plan items {meta['batch'][0]}-{meta['batch'][1]} of {meta['planned']} "
         f"(request-count cap {meta['cap']}); searches run {meta['searches_ok']} ok / {meta['searches_failed']} failed",
         f"- Unique jobs: **{total_jobs}** ({meta['new_jobs']} new). Skipped for missing identity/application link: "
         f"**{meta['skipped_no_identity']}**. LinkedIn links rejected as mismatched: {meta['rejected_links']}.",
         "- Descriptions and dates are Google's; LinkedIn links are consistency-checked against the listing, not verified.", "",
         "## Results per query", "", "| # | Search | City | Status | Returned | Kept | Without LinkedIn link | Rejected LinkedIn | Skipped (no identity) |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in query_rows:
        L.append(f"| {r['seq']} | {cell(r['q'])} | {cell(r['city'])} | {r['status']} | {r['returned']} | {r['kept']} | {r['no_linkedin']} | "
                 f"{r['rejected']} | {r['skipped']} |")
    L += [""] + screening_sections(ok, out)
    L += ["", "> No claim that relevance improved: review the jobs above against your own judgement first."]
    return "\n".join(L) + "\n"
