"""Deterministic query planning from the brief. No model calls, no invented terms."""

from __future__ import annotations

from dataclasses import dataclass

from nemo.brief import Brief


@dataclass(frozen=True)
class PlannedQuery:
    text: str
    purpose: str  # target_title | related_title | responsibility | company_careers | variation


def _suffixes(brief: Brief) -> list[str]:
    seen: dict[str, None] = {}
    for loc in brief.hard.locations:
        seen[f"remote {loc.place}" if loc.mode == "remote" else loc.place] = None
    return list(seen) or [""]


def plan_queries(brief: Brief) -> tuple[list[PlannedQuery], int]:
    """Return (queries, dropped_by_cap). Order = priority; capped so that
    `max_query_variations` stay reserved for model-generated variations."""
    s, out, seen = brief.search, [], set()

    def add(text: str, purpose: str) -> None:
        text = " ".join(text.split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(PlannedQuery(text, purpose))

    suffixes = _suffixes(brief)
    for title in s.target_titles:
        for suf in suffixes:
            add(f"{title} jobs {suf}", "target_title")
    for title in s.related_titles:
        for suf in suffixes:
            add(f"{title} jobs {suf}", "related_title")
    for resp in s.responsibilities:
        add(f"{resp} jobs", "responsibility")
    for company in s.companies_preferred:
        for title in s.target_titles:
            add(f"{company.name} careers {title}", "company_careers")

    reserved = min(brief.budgets.max_query_variations, brief.budgets.max_queries)
    cap = brief.budgets.max_queries - reserved
    return out[:cap], max(0, len(out) - cap)
