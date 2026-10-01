"""Feasibility probe: what does a search provider return for LinkedIn job pages?

Runs FIXED queries through a provider restricted to linkedin.com and records,
per result, what content came back and what posting-time text it contains. It
draws no conclusions itself: it writes raw responses plus counts so a human can
decide whether the v3 contract is achievable. Nothing here parses dates into
instants or admits jobs; that is service work that waits on these findings.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from nemo.budget import Budget, BudgetExceeded
from nemo.linkedin import linkedin_job_id, path_shape
from nemo.providers.base import ProviderError

# Generic role families chosen only to exercise the provider. They are NOT the user's preferences.
ROLES = ["software engineer", "registered nurse", "accountant", "data analyst", "marketing manager"]
SITE_PREFIX = "site:linkedin.com/jobs/view/"


def build_query(role: str) -> str:
    """Target individual postings. Whether the provider honours `site:` with a path is itself
    something the probe measures (share of results that are job-view URLs)."""
    role = role.strip()
    return role if role.lower().startswith("site:") else f"{SITE_PREFIX} {role}"


FIXED_QUERIES = [build_query(r) for r in ROLES]

_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("structured_datePosted", re.compile(r'"datePosted"\s*:\s*"([^"]+)"')),
    ("relative", re.compile(r"\b(\d{1,3})\s*(second|minute|hour|day|week|month|year)s?\s+ago\b", re.I)),
    ("long_date", re.compile(rf"\b(?:posted|listed)\s+(?:on\s+)?({_MONTHS}\s+\d{{1,2}},?\s+20\d{{2}})", re.I)),
    ("iso_date", re.compile(r"\b(20\d{2}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")),
]
_QUALIFIER = re.compile(r"(reposted|posted|updated|active|renewed)\W{0,4}(?:on\W{0,3})?$", re.I)
_LOGIN = re.compile(r"\b(sign in|join now|log in to|authwall|to see who you already know)\b", re.I)
_SIMILAR = re.compile(r"\b(similar jobs|people also viewed|more jobs|jobs you may be interested in)\b", re.I)
_TIME_KEYS = re.compile(r"retriev|crawl|fetch|timestamp|cached|scrap", re.I)
NON_POSTING_QUALIFIERS = {"reposted", "updated", "active", "renewed"}


def scan_time_mentions(text: str) -> list[dict[str, Any]]:
    """Find time-like phrases. Exploratory only: no interpretation of what they mean."""
    found: list[dict[str, Any]] = []
    ld_spans: list[tuple[int, int]] = []
    for kind, pat in _PATTERNS:
        for m in pat.finditer(text):
            if kind == "structured_datePosted":
                ld_spans.append(m.span())
            elif kind == "iso_date" and any(a <= m.start() < b for a, b in ld_spans):
                continue  # already reported as structured
            if kind == "long_date":   # the qualifier ("Posted"/"Listed") is part of the match itself
                qualifier = m.group(0).split()[0].lower()
            else:
                q = _QUALIFIER.search(text[max(0, m.start() - 24):m.start()])
                qualifier = q.group(1).lower() if q else None
            found.append({"kind": kind, "text": m.group(0), "qualifier": qualifier,
                          "context": text[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")})
    return found


def analyze_result(result: dict[str, Any]) -> dict[str, Any]:
    url = result.get("url", "")
    raw_content = result.get("raw_content") or ""
    snippet = result.get("content") or ""
    scanned = raw_content or snippet
    mentions = scan_time_mentions(scanned)
    return {
        "url": url, "job_id": linkedin_job_id(url), "shape": path_shape(url), "title": result.get("title"),
        "snippet_len": len(snippet), "raw_content_len": len(raw_content),
        "scanned_field": "raw_content" if raw_content else "content",
        "provider_published_date": result.get("published_date"),  # informational; never posting evidence
        "mentions": mentions,
        "login_wall_text": bool(_LOGIN.search(scanned)), "similar_jobs_text": bool(_SIMILAR.search(scanned)),
        "has_jobposting_ld": '"JobPosting"' in scanned,
        "result_keys": sorted(result.keys()),
    }


def _usable_kind(a: dict[str, Any]) -> str:
    """Rough bucket for planning only: which evidence class a candidate shows."""
    ms = [m for m in a["mentions"] if m["qualifier"] not in NON_POSTING_QUALIFIERS]
    if any(m["kind"] in {"structured_datePosted", "long_date"} for m in ms):
        return "absolute"
    if any(m["kind"] == "iso_date" for m in ms):
        return "absolute_candidate"     # bare ISO date in text: unclear what it dates
    if any(m["kind"] == "relative" for m in ms):
        return "relative_only"          # needs a retrieval time the provider may not give
    return "none"


def summarize(query_records: list[dict[str, Any]]) -> dict[str, Any]:
    results = [a for q in query_records for a in q.get("analysis", [])]
    job = [a for a in results if a["job_id"]]
    raw_lens = [a["raw_content_len"] for a in job]
    buckets = {k: sum(1 for a in job if _usable_kind(a) == k)
               for k in ("absolute", "absolute_candidate", "relative_only", "none")}
    shapes: dict[str, int] = {}
    for a in results:
        shapes[a["shape"]] = shapes.get(a["shape"], 0) + 1
    time_keys = sorted({k for q in query_records for k in q.get("top_level_keys", []) if _TIME_KEYS.search(k)}
                       | {k for a in results for k in a["result_keys"] if _TIME_KEYS.search(k)})
    return {
        "queries": len(query_records), "queries_failed": sum(1 for q in query_records if q.get("error")),
        "results_total": len(results), "job_view_urls": len(job), "other_urls": len(results) - len(job),
        "url_shapes": dict(sorted(shapes.items(), key=lambda kv: -kv[1])[:15]),
        "job_with_raw_content": sum(1 for a in job if a["raw_content_len"] > 0),
        "job_raw_content_len": ({"min": min(raw_lens), "median": statistics.median(raw_lens), "max": max(raw_lens)}
                                if raw_lens else None),
        "job_evidence_buckets": buckets,
        "job_with_only_nonposting_mentions": sum(
            1 for a in job if a["mentions"] and all(m["qualifier"] in NON_POSTING_QUALIFIERS for m in a["mentions"])),
        "job_with_multiple_relative_mentions": sum(
            1 for a in job if sum(m["kind"] == "relative" for m in a["mentions"]) > 1),
        "job_login_wall_text": sum(1 for a in job if a["login_wall_text"]),
        "job_similar_jobs_text": sum(1 for a in job if a["similar_jobs_text"]),
        "job_with_jobposting_ld": sum(1 for a in job if a["has_jobposting_ld"]),
        "job_with_provider_published_date": sum(1 for a in job if a["provider_published_date"]),
        "response_keys_seen": sorted({k for q in query_records for k in q.get("top_level_keys", [])}),
        "result_keys_seen": sorted({k for a in results for k in a["result_keys"]}),
        "time_or_retrieval_like_keys": time_keys,
        "credits_reported": sum(q.get("credits") or 0 for q in query_records),
    }


class RawSearchProvider(Protocol):
    name: str
    is_paid: bool
    max_cost_usd: float

    def search_raw(self, query: str, *, max_results: int = 10) -> dict[str, Any]: ...
    def cost_usd_from_response(self, raw: dict[str, Any]) -> float | None: ...


def run_probe(provider: RawSearchProvider, queries: list[str], budget: Budget, out_dir: Path, *,
              max_results: int = 10, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the fixed queries once each through the budget guard; write raw JSON + summary files."""
    records: list[dict[str, Any]] = []
    stopped = None
    for i, q in enumerate(queries, 1):
        rec: dict[str, Any] = {"query": q}
        try:
            with budget.call("queries", provider.name, is_paid=provider.is_paid,
                             max_cost_usd=provider.max_cost_usd) as res:
                raw = provider.search_raw(q, max_results=max_results)
                cost = provider.cost_usd_from_response(raw)
                if cost is not None:
                    res.set_actual_usd(cost)
            (out_dir / "raw").mkdir(parents=True, exist_ok=True)   # only once a call has succeeded
            (out_dir / "raw" / f"{i:02d}.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False))
            rec.update(top_level_keys=sorted(raw.keys()), credits=(raw.get("usage") or {}).get("credits"),
                       analysis=[analyze_result(r) for r in raw.get("results", [])])
        except ProviderError as exc:
            rec["error"] = {"kind": exc.kind, "message": str(exc)}
        except BudgetExceeded as exc:
            rec["error"] = {"kind": "budget", "message": str(exc)}
            stopped = exc.cap
            records.append(rec)
            break
        records.append(rec)
    summary = {"meta": {**(meta or {}), "finished_at": datetime.now(timezone.utc).isoformat(),
                        "stopped_by_cap": stopped, "usage": budget.usage()},
               "counts": summarize(records), "queries": records}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    (out_dir / "summary.md").write_text(render_markdown(summary))
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    c, meta = summary["counts"], summary["meta"]
    lines = ["# Provider feasibility probe", "",
             "Read the raw files in `raw/` too. These counts are prompts for judgement, not a verdict.", "",
             f"- provider: {meta.get('provider')} · params: `{json.dumps(meta.get('params'))}`",
             f"- usage: {meta.get('usage')} · stopped by cap: {meta.get('stopped_by_cap')}", "",
             "## Counts", "",
             f"- queries: {c['queries']} (failed: {c['queries_failed']})",
             f"- results: {c['results_total']} → individual job-view URLs: **{c['job_view_urls']}**, other: {c['other_urls']}",
             f"- job URLs with raw page content: {c['job_with_raw_content']}; length: {c['job_raw_content_len']}",
             f"- posting-time text on job URLs (rough buckets): {c['job_evidence_buckets']}",
             f"- only reposted/updated-type mentions: {c['job_with_only_nonposting_mentions']}; "
             f"multiple relative mentions (ambiguous): {c['job_with_multiple_relative_mentions']}",
             f"- login-wall text: {c['job_login_wall_text']}; similar-jobs text: {c['job_similar_jobs_text']}; "
             f"JobPosting structured data: {c['job_with_jobposting_ld']}",
             f"- provider `published_date` present (NOT used as evidence): {c['job_with_provider_published_date']}",
             f"- retrieval/crawl-time-like keys in responses: {c['time_or_retrieval_like_keys'] or 'none'}",
             f"- response keys: {c['response_keys_seen']}", f"- result keys: {c['result_keys_seen']}",
             f"- URL shapes: {c['url_shapes']}", "", "## Per query", ""]
    for q in summary["queries"]:
        if q.get("error"):
            lines.append(f"### `{q['query']}` — FAILED: {q['error']}")
            continue
        lines.append(f"### `{q['query']}` — {len(q['analysis'])} results, credits {q.get('credits')}")
        for a in q["analysis"]:
            lines.append(f"- {a['url']} · job_id={a['job_id']} · raw={a['raw_content_len']} · "
                         f"snippet={a['snippet_len']} · evidence={_usable_kind(a) if a['job_id'] else 'n/a'}")
            for m in a["mentions"][:3]:
                lines.append(f"    - {m['kind']} [{m['qualifier'] or '-'}]: …{m['context']}…")
    return "\n".join(lines) + "\n"
