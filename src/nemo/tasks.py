"""Job matching: (job profile, job text) -> outcome, with every criterion answered yes/no/unknown.

Small local models drift, so code (not the model) decides: quotes must appear in the job text;
a yes/no without a verifiable quote becomes unknown (missing job information is unknown, never
met or failed); `not_relevant` needs verified evidence; thin or unsupported cases become
`insufficient_evidence` (docs/phase1/03-architecture.md §5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nemo.profile import JobProfile
from nemo.providers.base import LLMProvider

OUTCOMES = ["strong_match", "possible_match", "not_relevant", "insufficient_evidence"]
DEFAULT_MAX_CHARS = 8000

ASSESS_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": OUTCOMES},
        "explanation": {"type": "string"},
        "fit_quotes": {"type": "array", "items": {"type": "string"}},
        "mismatch_quotes": {"type": "array", "items": {"type": "string"}},
        "criteria": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "status": {"type": "string", "enum": ["yes", "no", "unknown"]},
                           "quote": {"type": "string"}},
            "required": ["id", "status", "quote"]}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "explanation", "fit_quotes", "mismatch_quotes", "criteria", "limitations"],
}
ASSESS_SYSTEM = (
    "You judge whether ONE job posting suits a candidate described by a job profile. Judge the work "
    "described (responsibilities, seniority) against the desired roles and experience, not keyword overlap. "
    "Outcomes: strong_match, possible_match, not_relevant (clearly different work or very different "
    "seniority), insufficient_evidence (text too thin to judge). For EVERY criterion id in the profile give "
    "status: yes = the JOB TEXT shows this is true of the job; no = the job text shows it is false; "
    "unknown = the job text does not say. Do not guess: if the text is silent, answer unknown. For yes/no, "
    "`quote` is copied EXACTLY from the JOB TEXT (under 150 characters); for unknown, quote is \"\". "
    "fit_quotes support a match, mismatch_quotes show different work; both copied exactly from the job text. "
    "Ignore sign-in prompts and lists of other/similar jobs. `limitations` names what the text does not "
    "show. `explanation` is at most 2 sentences.")


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


def render_profile(profile: JobProfile) -> str:
    lines = [f"EXPERIENCE: {profile.experience}", "DESIRED ROLES: " + "; ".join(profile.desired_roles)]
    crit = profile.criteria()
    if crit:
        lines.append("CRITERIA (answer every id):")
        lines += [f"{cid} ({kind.replace('_', '-')}): {text}" for cid, kind, text in crit]
    return "\n".join(lines)


def content_level(job_text: str) -> str:
    """By code, from length. Thresholds are proposals to be tuned on real returned content."""
    n = len(job_text.strip())
    return "snippet" if n < 500 else "partial_description" if n < 2000 else "full_description"


def is_grounded(quote: str, text: str) -> bool:
    q = _norm(quote)
    return len(q) >= 8 and q in _norm(text)


def ground_quotes(quotes: list[str], text: str) -> tuple[list[str], list[str]]:
    kept, dropped = [], []
    for q in quotes:
        (kept if is_grounded(q, text) else dropped).append(q)
    return kept, dropped


@dataclass(frozen=True)
class CriterionResult:
    id: str
    kind: str        # must_have | nice_to_have | exclusion
    text: str
    status: str      # yes | no | unknown  ("yes" = the job text shows this is true of the job)
    quote: str
    note: str = ""


def resolve_criteria(profile: JobProfile, raw: Any, job_text: str) -> tuple[list[CriterionResult], list[str]]:
    """Every profile criterion gets a result. Missing, malformed or unquoted answers become unknown."""
    answers: dict[str, dict] = {}
    for a in raw if isinstance(raw, list) else []:
        if isinstance(a, dict) and isinstance(a.get("id"), str):
            answers.setdefault(a["id"].strip().upper(), a)
    out, dropped = [], []
    for cid, kind, text in profile.criteria():
        a = answers.get(cid)
        if a is None:
            out.append(CriterionResult(cid, kind, text, "unknown", "", "not answered"))
            continue
        status, quote = a.get("status"), a.get("quote") if isinstance(a.get("quote"), str) else ""
        if status not in ("yes", "no"):
            out.append(CriterionResult(cid, kind, text, "unknown", ""))
        elif is_grounded(quote, job_text):
            out.append(CriterionResult(cid, kind, text, status, quote.strip()))
        else:
            if quote.strip():
                dropped.append(quote.strip())
            out.append(CriterionResult(cid, kind, text, "unknown", "", f"model said {status!r} without a verifiable quote"))
    return out, dropped


def finalize_outcome(model_outcome: str, fit: list[str], mismatch: list[str], criteria: list[CriterionResult],
                     level: str) -> tuple[str, str]:
    """Code decides the final outcome from verified evidence only."""
    blocking = [c.id for c in criteria if (c.kind == "must_have" and c.status == "no")
                or (c.kind == "exclusion" and c.status == "yes")]
    if blocking:
        return "not_relevant", "blocked by " + ", ".join(blocking)
    if model_outcome in ("strong_match", "possible_match"):
        support = fit or [c.quote for c in criteria if c.kind == "must_have" and c.status == "yes"]
        if not support:
            return "insufficient_evidence", "match claimed but no supporting quote could be verified in the job text"
        if model_outcome == "strong_match" and level == "snippet":
            return "possible_match", "strong_match capped at possible_match: snippet-only content"
        unknown_must = [c.id for c in criteria if c.kind == "must_have" and c.status == "unknown"]
        if model_outcome == "strong_match" and unknown_must:
            return "possible_match", "strong_match capped at possible_match: unknown must-have(s) " + ", ".join(unknown_must)
        return model_outcome, ""
    if model_outcome == "not_relevant":
        if mismatch:
            return "not_relevant", ""
        return "insufficient_evidence", "not_relevant claimed but no mismatch could be verified"
    return "insufficient_evidence", ""


@dataclass
class AssessResult:
    outcome: str
    model_outcome: str
    note: str
    explanation: str
    fit_quotes: list[str]
    mismatch_quotes: list[str]
    criteria: list[CriterionResult]
    dropped_quotes: list[str]
    limitations: list[str]
    content_level: str
    job_truncated: bool
    meta: dict[str, Any] = field(default_factory=dict)


def assess_job(llm: LLMProvider, profile: JobProfile, job_text: str) -> AssessResult:
    head = f"JOB PROFILE:\n{render_profile(profile)}\n\nJOB TEXT:\n"
    room = _prompt_chars(llm, ASSESS_SYSTEM, ASSESS_SCHEMA) - len(head) - 40
    text, truncated = fit_text(job_text, max(room, 0))
    res = llm.complete_json(ASSESS_SYSTEM, head + text, ASSESS_SCHEMA)
    d = res.data
    model_outcome = d.get("outcome") if d.get("outcome") in OUTCOMES else "insufficient_evidence"
    fit, drop_f = ground_quotes(_str_list(d.get("fit_quotes"), 3), text)
    mis, drop_m = ground_quotes(_str_list(d.get("mismatch_quotes"), 3), text)
    criteria, drop_c = resolve_criteria(profile, d.get("criteria"), text)
    level = content_level(job_text)
    outcome, note = finalize_outcome(model_outcome, fit, mis, criteria, level)
    if d.get("outcome") not in OUTCOMES:
        note = (note + "; " if note else "") + f"unrecognised model outcome {d.get('outcome')!r}"
    return AssessResult(outcome, model_outcome, note, str(d.get("explanation", ""))[:400], fit, mis, criteria,
                        drop_f + drop_m + drop_c, _str_list(d.get("limitations"), 5), level, truncated,
                        getattr(res, "meta", {}))
