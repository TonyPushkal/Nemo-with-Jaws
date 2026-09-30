"""The two model tasks Phase 1 needs, kept minimal: résumé -> profile, and (profile, job) -> match.

Small local models drift, so code (not the model) checks the output: quotes must appear in the
source text, outcomes are decided from what survives, and missing evidence yields
`insufficient_evidence`, never `not_relevant` (docs/phase1/03-architecture.md §5).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from nemo.providers.base import LLMProvider

SENIORITY = ["junior", "mid", "senior", "lead", "unknown"]
OUTCOMES = ["strong_match", "possible_match", "not_relevant", "insufficient_evidence"]
DEFAULT_MAX_CHARS = 8000

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}},
        "related_titles": {"type": "array", "items": {"type": "string"}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "seniority": {"type": "string", "enum": SENIORITY},
        "years_experience": {"type": ["number", "null"]},
        "domains": {"type": "array", "items": {"type": "string"}},
        "location_in_resume": {"type": ["string", "null"]},
        "search_queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titles", "related_titles", "skills", "seniority", "years_experience", "domains",
                 "location_in_resume", "search_queries"],
}
PROFILE_SYSTEM = (
    "You extract a job-search profile from a resume. Use ONLY what the resume states; never invent "
    "skills, titles or years. Use [] or null when something is absent. `titles` = job titles the person "
    "has held or clearly targets; `related_titles` = close alternative titles for the same work; `skills` = "
    "tools and technologies named in the resume, copied as written; `search_queries` = up to 5 short "
    "web-search phrases (2-6 words) for job postings that would suit this person.")

ASSESS_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": OUTCOMES},
        "explanation": {"type": "string"},
        "fit_quotes": {"type": "array", "items": {"type": "string"}},
        "mismatch_quotes": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "explanation", "fit_quotes", "mismatch_quotes", "limitations"],
}
ASSESS_SYSTEM = (
    "You judge whether ONE job posting suits a candidate, judging the work described (responsibilities, "
    "seniority), not keyword overlap. Outcomes: strong_match, possible_match, not_relevant (the posting "
    "clearly describes different work or a very different seniority), insufficient_evidence (the text is "
    "too thin to judge, e.g. only a title or a sentence). Quotes must be copied EXACTLY from the JOB TEXT, "
    "each under 200 characters: fit_quotes support a match, mismatch_quotes show a mismatch. Do not quote "
    "the candidate profile. Ignore navigation text, sign-in prompts and lists of other/similar jobs. In "
    "`limitations` name what the text does not show (e.g. responsibilities, seniority, location). "
    "`explanation` is at most 2 sentences.")


def _prompt_chars(llm: LLMProvider, system: str, schema: dict[str, Any]) -> int:
    fn = getattr(llm, "available_prompt_chars", None)
    return fn(system, schema) if fn else DEFAULT_MAX_CHARS


def fit_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Keep the head of `text`; report whether anything was cut."""
    text = text.strip()
    return (text, False) if len(text) <= max_chars else (text[:max_chars].rstrip(), True)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def _str_list(v: Any, cap: int) -> list[str]:
    return [x.strip() for x in v if isinstance(x, str) and x.strip()][:cap] if isinstance(v, list) else []


# ------------------------------------------------------------------ résumé -> profile

@dataclass
class ProfileResult:
    profile: dict[str, Any]
    resume_truncated: bool
    problems: list[str] = field(default_factory=list)
    ungrounded_skills: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def validate_profile(data: dict[str, Any], resume_text: str) -> tuple[dict[str, Any], list[str], list[str]]:
    problems: list[str] = []
    for k in PROFILE_SCHEMA["required"]:
        if k not in data:
            problems.append(f"missing key: {k}")
    seniority = data.get("seniority")
    if seniority not in SENIORITY:
        problems.append(f"bad seniority: {seniority!r}")
        seniority = "unknown"
    yrs = data.get("years_experience")
    if not (yrs is None or (isinstance(yrs, (int, float)) and not isinstance(yrs, bool) and 0 <= yrs <= 60)):
        problems.append(f"bad years_experience: {yrs!r}")
        yrs = None
    loc = data.get("location_in_resume")
    profile = {"titles": _str_list(data.get("titles"), 6), "related_titles": _str_list(data.get("related_titles"), 8),
               "skills": _str_list(data.get("skills"), 30), "seniority": seniority, "years_experience": yrs,
               "domains": _str_list(data.get("domains"), 6),
               "location_in_resume": loc.strip() if isinstance(loc, str) and loc.strip() else None,
               "search_queries": _str_list(data.get("search_queries"), 5)}
    if not profile["titles"]:
        problems.append("no titles extracted")
    if not profile["search_queries"]:
        problems.append("no search queries produced")
    hay = _norm(resume_text)
    ungrounded = [s for s in profile["skills"] if _norm(s) not in hay]
    return profile, problems, ungrounded


def extract_profile(llm: LLMProvider, resume_text: str) -> ProfileResult:
    text, truncated = fit_text(resume_text, _prompt_chars(llm, PROFILE_SYSTEM, PROFILE_SCHEMA) - 40)
    res = llm.complete_json(PROFILE_SYSTEM, f"RESUME:\n{text}", PROFILE_SCHEMA)
    profile, problems, ungrounded = validate_profile(res.data, text)
    return ProfileResult(profile, truncated, problems, ungrounded, getattr(res, "meta", {}))


# ------------------------------------------------------------------ (profile, job) -> match

def content_level(job_text: str) -> str:
    """By code, from length. Thresholds are proposals to be tuned on real returned content."""
    n = len(job_text.strip())
    return "snippet" if n < 500 else "partial_description" if n < 2000 else "full_description"


def ground_quotes(quotes: list[str], text: str) -> tuple[list[str], list[str]]:
    hay, kept, dropped = _norm(text), [], []
    for q in quotes:
        (kept if len(_norm(q)) >= 8 and _norm(q) in hay else dropped).append(q)
    return kept, dropped


def finalize_outcome(model_outcome: str, grounded_fit: list[str], grounded_mismatch: list[str],
                     level: str) -> tuple[str, str]:
    """Code decides the final outcome; unsupported claims become insufficient_evidence."""
    if model_outcome in ("strong_match", "possible_match"):
        if not grounded_fit:
            return "insufficient_evidence", "match claimed but no fit quote could be verified in the job text"
        if model_outcome == "strong_match" and level == "snippet":
            return "possible_match", "strong_match capped at possible_match: snippet-only content"
        return model_outcome, ""
    if model_outcome == "not_relevant":
        if not grounded_mismatch:
            return "insufficient_evidence", "not_relevant claimed but no mismatch quote could be verified"
        return "not_relevant", ""
    return "insufficient_evidence", ""


@dataclass
class AssessResult:
    outcome: str
    model_outcome: str
    note: str
    explanation: str
    fit_quotes: list[str]
    mismatch_quotes: list[str]
    dropped_quotes: list[str]
    limitations: list[str]
    content_level: str
    job_truncated: bool
    meta: dict[str, Any] = field(default_factory=dict)


def _profile_brief(profile: dict[str, Any]) -> str:
    keep = {k: profile.get(k) for k in ("titles", "related_titles", "skills", "seniority", "years_experience",
                                        "domains")}
    return json.dumps(keep, ensure_ascii=False)


def assess_job(llm: LLMProvider, profile: dict[str, Any], job_text: str) -> AssessResult:
    head = f"CANDIDATE PROFILE:\n{_profile_brief(profile)}\n\nJOB TEXT:\n"
    room = _prompt_chars(llm, ASSESS_SYSTEM, ASSESS_SCHEMA) - len(head) - 40
    text, truncated = fit_text(job_text, max(room, 0))
    res = llm.complete_json(ASSESS_SYSTEM, head + text, ASSESS_SCHEMA)
    d = res.data
    model_outcome = d.get("outcome") if d.get("outcome") in OUTCOMES else "insufficient_evidence"
    fit, drop_f = ground_quotes(_str_list(d.get("fit_quotes"), 3), text)
    mis, drop_m = ground_quotes(_str_list(d.get("mismatch_quotes"), 3), text)
    level = content_level(job_text)
    outcome, note = finalize_outcome(model_outcome, fit, mis, level)
    if d.get("outcome") not in OUTCOMES:
        note = f"unrecognised model outcome {d.get('outcome')!r}"
    return AssessResult(outcome, model_outcome, note, str(d.get("explanation", ""))[:400], fit, mis,
                        drop_f + drop_m, _str_list(d.get("limitations"), 5), level, truncated,
                        getattr(res, "meta", {}))
