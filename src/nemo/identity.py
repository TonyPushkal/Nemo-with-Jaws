"""Pure rules for merging job records. Merging needs strong identity evidence;
weak matches are reported as possible duplicates, never merged."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from nemo.urlnorm import canonical_url

DEFAULT_SIMILARITY = 0.90  # starting value; tune on real data


class MergeKind(str, Enum):
    MERGE = "merge"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    DISTINCT = "distinct"


@dataclass(frozen=True)
class Candidate:
    company: str
    title: str
    location: str = ""
    ats_key: str | None = None          # e.g. "greenhouse:acme:12345"
    requisition_id: str | None = None
    apply_url: str | None = None
    text: str | None = None             # full fetched description; None for snippet-only leads

    @property
    def snippet_only(self) -> bool:
        return not self.text


@dataclass(frozen=True)
class MergeDecision:
    kind: MergeKind
    rule: str


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.casefold()).split())


def text_similarity(a: str, b: str) -> float:
    """Jaccard similarity over word 3-shingles."""
    def shingles(t: str) -> set[tuple[str, ...]]:
        w = norm(t).split()
        return {tuple(w[i:i + 3]) for i in range(max(1, len(w) - 2))}
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def decide_merge(a: Candidate, b: Candidate, threshold: float = DEFAULT_SIMILARITY) -> MergeDecision:
    same_company = norm(a.company) == norm(b.company)

    if a.ats_key and b.ats_key:
        if a.ats_key == b.ats_key:
            return MergeDecision(MergeKind.MERGE, "same_ats_key")
        return MergeDecision(MergeKind.DISTINCT, "different_ats_key")
    if same_company and a.requisition_id and b.requisition_id:
        if a.requisition_id == b.requisition_id:
            return MergeDecision(MergeKind.MERGE, "same_requisition_id")
        return MergeDecision(MergeKind.DISTINCT, "different_requisition_id")
    if a.apply_url and b.apply_url and canonical_url(a.apply_url) == canonical_url(b.apply_url):
        return MergeDecision(MergeKind.MERGE, "same_canonical_apply_url")

    if not (same_company and norm(a.title) == norm(b.title)):
        return MergeDecision(MergeKind.DISTINCT, "company_or_title_differs")
    if a.location and b.location and norm(a.location) != norm(b.location):
        return MergeDecision(MergeKind.DISTINCT, "location_differs")
    if not (a.location and b.location):
        return MergeDecision(MergeKind.POSSIBLE_DUPLICATE, "location_unknown")
    if a.snippet_only or b.snippet_only:
        return MergeDecision(MergeKind.POSSIBLE_DUPLICATE, "snippet_only_record")
    if text_similarity(a.text or "", b.text or "") >= threshold:
        return MergeDecision(MergeKind.MERGE, "title_company_location_and_text_similarity")
    return MergeDecision(MergeKind.POSSIBLE_DUPLICATE, "text_similarity_below_threshold")
