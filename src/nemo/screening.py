"""Rules-first screening and ranking of collected jobs. No model, no network.

Hard constraints come from the job profile's Must-haves and Exclusions (cited in every exclusion reason).
Everything else only ranks. A requirement the posting does not state is UNKNOWN: the job stays eligible, is
flagged, and is not penalised. Fit (rank score) and confidence (how much the posting actually says) are separate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nemo.discovery import application_url_status
from nemo.profile import JobProfile

# Candidate experience in months (from the user; the profile text also states them).
FULL_TIME_MONTHS, TOTAL_MONTHS = 14, 21
MAX_MIN_YEARS = 2                      # profile must-have: no required minimum above 2 years; exactly 2 is borderline

_YEARS = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)(?:\s*(?:-|–|—|to)\s*(\d{1,2}(?:\.\d)?))?\s*(\+)?\s*(?:years?|yrs?)\b", re.I)
_PREFERRED = re.compile(r"preferred|\bplus\b|nice to have|bonus|good to have|desirable|ideally|advantage|a plus", re.I)
_EXPERIENCE = re.compile(r"experience|\bexp\b|experienced|hands-on|proven|professional|background|expertise|minimum|at least", re.I)
# Sentences about the company or its people (founder/CEO/company history) are never applicant requirements.
_COMPANY_CONTEXT = re.compile(
    r"\b(ceo|cto|cfo|coo|founder|co-?founder|chairman|president|our (company|group|organi[sz]ation|leadership)|"
    r"organi[sz]ation with|corporation|since \d{4}|founded|established|decades|in business|"
    r"years of (innovation|excellence|legacy|history)|history of|"
    r"we (have|are|['’]ve)\b(?!\s+(looking|seeking|hiring|searching)))", re.I)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])|(?<=[a-z]{2}[.!?])(?=[A-Z0-9])|[\n•;]")
_EDUCATION = re.compile(r"^\s*(of\s+)?(formal\s+)?(education|schooling|study)", re.I)
MAX_PLAUSIBLE_YEARS = 25   # larger figures are company history, not a requirement
_ALT_ROUTE = re.compile(
    r"(fresh(?:ers?| graduates?)|(?:recent|new) graduates?|entry[- ]level candidates?|early[- ]career candidates?)"
    r"[^.]{0,160}?(encouraged|welcome|can apply|may apply|eligible|considered|invited|apply)|"
    r"(encourage|welcome|open to|consider)[^.]{0,60}(fresh graduates?|freshers?|new grads?|recent graduates?)", re.I)
_EXCLUDES_INTERN = re.compile(r"non[- ]?internship|excluding intern|does not include intern|not including intern", re.I)
_NON_SOFTWARE_DESC = re.compile(r"laborator|supplier quality|manufactur|\bgmp\b|iso ?9001|pharma|\baudit(s|ing)?\b|inspection|"
                                r"chemical|process quality|six sigma|revenue cycle|medical coding", re.I)
_FRESHER = re.compile(r"\bfreshers?\b|entry[- ]level|new grad|fresh graduate", re.I)

# "SDE I, II, III" / "Engineer I-III": one posting for several levels, including an early-career one.
_MIXED_LEVEL = re.compile(r"(?<![a-z0-9])(?:i|1)(?:\s*(?:,|/|&|\band\b|\bor\b|\bto\b|–|-)\s*(?:sde[- ]?|engineer[- ]?)?"
                          r"(?:iii|ii|iv|v|2|3|4|5)(?![a-z0-9]))+", re.I)
_SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|leader|architect|manager|head|director|"
                           r"sde[- ]?(3|4|5|iii|iv|v)|engineer (iii|iv|v|3|4)|(iii|iv)\b)", re.I)
_NON_SOFTWARE_TITLE = re.compile(
    r"\b(lab|laboratory|supplier|manufacturing|factory|mechanical|chemical|civil|industrial|"
    r"technical content|content (creator|writer)|technical writer|martech|marketing|sales|recruit\w*|talent|"
    r"project manager|program manager|product manager|accountant|finance|nurse)\b", re.I)
_QA_TITLE = re.compile(r"\b(quality|qa)\b", re.I)
_SOFTWARE_TITLE = re.compile(r"software|sdet|sde\b|developer|test automation|automation test|qa automation|"
                             r"qa engineer|devops|backend|full[- ]?stack|cloud|platform", re.I)
_SOFTWARE_TERMS = ("selenium", "playwright", "cypress", "api testing", "test automation", "automated test", "ci/cd",
                   "python", "java", "javascript", "sdlc", "software", "kubernetes", "docker")
_STATES = ("karnataka", "telangana", "andhra pradesh", "maharashtra", "tamil nadu", "uttar pradesh", "gujarat",
           "haryana", "delhi", "west bengal", "kerala", "rajasthan", "madhya pradesh", "punjab", "odisha", "goa")
_ABROAD = re.compile(r"united states|\busa\b|\bu\.s\.|united kingdom|\buk\b|canada|germany|singapore|australia|"
                     r"ireland|netherlands|, [A-Z]{2}\b", re.I)

# Desired-role phrase -> title pattern. Roles not listed fall back to their own words.
ROLE_PATTERNS = {
    "backend engineer": r"back[- ]?end",
    "software development engineer": r"\bsde\b|software (development )?engineer|software developer",
    "devops engineer": r"devops",
    "platform engineer": r"platform engineer",
    "site reliability engineer": r"site reliability|\bsre\b",
    "cloud engineer": r"cloud (engineer|infrastructure|developer)",
    "sdet": r"sdet|development engineer in test|test automation|automation (test )?engineer|qa automation",
    "ai platform engineer": r"ai (platform|infrastructure)|ml ?ops|machine learning (platform|engineer)|llm",
}
ADJACENT = re.compile(r"full[- ]?stack|data engineer|software qa|infrastructure engineer|site reliability", re.I)

# Role discipline. SDE-level titles are only a strong match for the disciplines in TARGET_DISCIPLINES.
_DISCIPLINE_TITLE = (
    ("frontend", r"front[- ]?end|\bui\b|\bux\b|react|angular|\bvue\b|web developer|javascript developer"),
    ("data_science", r"data scien|data analyst|\bnlp\b|research scientist|\bml scientist|machine learning scientist|analytics"),
    ("mobile", r"android|\bios\b|mobile"),
    ("fullstack", r"full[- ]?stack"),
    ("backend", r"back[- ]?end|server[- ]side|\bapi\b"),
    ("devops", r"devops|\bsre\b|site reliability|platform engineer|cloud (engineer|infrastructure|support)|infrastructure engineer|release engineer"),
    ("sdet", r"sdet|test automation|qa automation|engineer in test|automation (test )?engineer"),
    ("ai_ml", r"ai (platform|infrastructure|engineer)|ml ?ops|\bllm\b|genai|ml engineer|machine learning engineer"),
)
_DISCIPLINE_BODY = {
    "backend": r"\b(apis?|microservices?|rest(ful)?|backend|back-end|database|sql|python|java|golang|go lang|spring|node\.?js)\b",
    "devops": r"kubernetes|ci/cd|terraform|ansible|docker|jenkins|\baws\b|\bgcp\b|azure|infrastructure|devops",
    "sdet": r"selenium|test automation|test cases|playwright|cypress",
    "frontend": r"\breact\b|angular|\bvue\b|\bcss\b|\bhtml\b|front[- ]?end",
    "data_science": r"data science|machine learning models?|pandas|statistics|\bnlp\b",
}
TARGET_DISCIPLINES = {"backend", "devops", "sdet", "ai_ml"}
ADJACENT_DISCIPLINES = {"frontend", "fullstack", "data_science", "mobile"}


def discipline(title: str, desc: str) -> str:
    """frontend | fullstack | data_science | mobile | backend | devops | sdet | ai_ml | unspecified. Title first; for a
    generic title (e.g. 'SDE I') the description must show one discipline clearly, else 'unspecified'."""
    hits = [name for name, pat in _DISCIPLINE_TITLE if re.search(pat, title or "", re.I)]
    if "frontend" in hits and ("backend" in hits or "fullstack" in hits):
        return "fullstack"
    if hits:
        order = ["frontend", "data_science", "mobile", "fullstack", "backend", "devops", "sdet", "ai_ml"]
        return min(hits, key=order.index)
    head = (desc or "")[:1500]
    counts = {k: len(re.findall(p, head, re.I)) for k, p in _DISCIPLINE_BODY.items()}
    best = max(counts, key=counts.get)
    runner_up = sorted(counts.values())[-2]
    return best if counts[best] >= 3 and counts[best] > runner_up else "unspecified"


SKILLS = {"python": r"\bpython\b", "kubernetes": r"kubernetes|\bk8s\b|\bgke\b|\beks\b", "ci/cd": r"ci/cd|\bcicd\b|jenkins|gitlab",
          "gitops": r"gitops|argo ?cd|helm", "cloud": r"\bgcp\b|\baws\b|google cloud|azure", "llm/rag": r"\bllm\b|\brag\b|genai"}
EARLY = re.compile(r"\bsde[- ]?(1|i)\b|engineer (i|1)\b|\bjunior\b|\bassociate\b|\bfresher|entry[- ]level|\b0\s*[-–to]+\s*2 years", re.I)
PREFERRED_CITIES = ("bengaluru", "bangalore", "hyderabad")      # profile: Bengaluru or Hyderabad preferred
_OTHER_INDIAN_CITIES = ("visakhapatnam", "vizag")                # still recognised as India, just not preferred


@dataclass
class Experience:
    required_min: float | None = None            # highest minimum among required statements
    min_of_mentions: float | None = None         # lowest minimum among required statements (for mixed-level openings)
    required_mentions: list[str] = field(default_factory=list)
    preferred_mentions: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)   # explicit other routes, e.g. "fresh graduates ... encouraged"
    excludes_internship: bool = False                        # "non-internship experience": internship months don't count
    excerpts: list[dict] = field(default_factory=list)       # exact JD text supporting each statement


def parse_experience(title: str, description: str) -> Experience:
    """Applicant requirements, read sentence by sentence. A range counts by its minimum; '4+' means 4. Sentences about the
    company or its people (CEO, founder, company history) and education years are ignored."""
    exp, mins = Experience(), []
    description = description or ""
    sources = [("title", title or "", None)] + [("description", sent.strip(), None) for sent in _SPLIT.split(description) if sent and sent.strip()]
    for source, sent, _ in sources:
        for m in _YEARS.finditer(sent):
            ctx = sent
            if len(sent) > 320:      # run-on text without punctuation: use a window, snapped to whole words
                lo_i, hi_i = max(0, m.start() - 110), m.end() + 110
                ctx = sent[lo_i:hi_i]
                if lo_i > 0:
                    ctx = ctx.split(" ", 1)[-1]
                if hi_i < len(sent):
                    ctx = ctx.rsplit(" ", 1)[0]
            if source == "description" and (not _EXPERIENCE.search(ctx) or _COMPANY_CONTEXT.search(ctx)):
                continue
            lo, hi, plus = float(m.group(1)), m.group(2), bool(m.group(3))
            if lo > MAX_PLAUSIBLE_YEARS or _EDUCATION.match(sent[m.end(): m.end() + 30]):
                continue
            label = f"{lo:g}{'-' + hi if hi else ''}{'+' if plus else ''} years ({source})"
            kind = "preferred" if _PREFERRED.search(ctx) else "required"
            note = {"years": label, "kind": kind, "text": re.sub(r"\s+", " ", ctx).strip()[:260]}
            if note not in exp.excerpts:
                exp.excerpts.append(note)
            if kind == "preferred":
                if label not in exp.preferred_mentions:
                    exp.preferred_mentions.append(label)
            else:
                if label not in exp.required_mentions:
                    exp.required_mentions.append(label)
                mins.append(lo)
                if _EXCLUDES_INTERN.search(ctx):
                    exp.excludes_internship = True
    for m in _ALT_ROUTE.finditer(description):
        start = max(description.rfind(".", 0, m.start()) + 1, m.start() - 60)
        end = description.find(".", m.end())
        snippet = re.sub(r"\s+", " ", description[start: end if end != -1 else m.end() + 80]).strip()
        if snippet not in exp.alternatives:
            exp.alternatives.append(snippet[:220])
    if mins:
        exp.required_min, exp.min_of_mentions = max(mins), min(mins)
    elif _FRESHER.search(f"{title} {description[:400]}"):
        exp.required_min = exp.min_of_mentions = 0.0
        exp.required_mentions.append("fresher / entry level")
    return exp


def employment_type(title: str, description: str) -> str:
    if re.search(r"\bintern(ship)?\b", title or "", re.I) or re.search(r"\bintern(ship)?\b", (description or "")[:300], re.I):
        return "internship"
    if re.search(r"\bcontract(or)?\b|\bconsultant\b|\bFTC\b|fixed[- ]term", f"{title} {(description or '')[:400]}", re.I):
        return "contract"
    return "unspecified"


def _in_india(location: str) -> bool | None:
    t = (location or "").lower()
    if "india" in t or any(s in t for s in _STATES) or any(c in t for c in PREFERRED_CITIES + _OTHER_INDIAN_CITIES):
        return True
    return False if _ABROAD.search(location or "") else None


MIN_ASSESSABLE_CHARS = 500


@dataclass
class Screened:
    job: dict[str, Any]
    eligible: bool
    exclusions: list[str]
    reasons: list[str]           # why it matches
    gaps: list[str]              # experience gaps, flags, unknowns
    fit: float
    confidence: str
    experience: Experience
    employment: str
    borderline: bool = False
    review: bool = False
    match: str = "weak"          # strong | possible | adjacent | weak (excluded jobs: "excluded")
    gap_full_time_months: float | None = None
    gap_total_months: float | None = None
    discipline: str = "unspecified"
    readiness: str = "length_ok"     # length_ok | short: description length only; completeness is NOT verified
    application_status: str = "structurally_valid"  # structurally_valid | needs_review (URL shape only; destination not checked)


def screen(job: dict[str, Any], profile: JobProfile, *, max_min_years: int = MAX_MIN_YEARS,
           total_months: int = TOTAL_MONTHS, full_time_months: int = FULL_TIME_MONTHS) -> Screened:
    title, desc, loc = job.get("title") or "", job.get("description") or "", job.get("location") or ""
    exclusions, reasons, gaps, borderline, review = [], [], [], False, False
    exp = parse_experience(title, desc)
    emp = employment_type(title, desc)
    disc = discipline(title, desc)
    readiness = "length_ok" if len(desc) >= MIN_ASSESSABLE_CHARS else "short"
    mixed = bool(_MIXED_LEVEL.search(title))
    # a mixed-level opening is judged by its lowest stated requirement: the early-career opening may be the low one
    eff_min = exp.min_of_mentions if mixed else exp.required_min

    # ---- hard constraints (profile Must-haves / Exclusions)
    if eff_min is not None and exp.required_min is not None and exp.required_min > max_min_years:
        if mixed and eff_min <= max_min_years:
            review = True
            gaps.append(f"REVIEW: mixed-level title '{title}'; stated requirements {', '.join(exp.required_mentions)} may apply "
                        f"to the higher levels, the lowest is within your limit")
        elif not mixed and exp.alternatives:
            review = True
            gaps.append(f"REVIEW: required {', '.join(exp.required_mentions)} (above your limit) but the posting states another route: "
                        f"\"{exp.alternatives[0]}\"")
        else:
            lowest = f"{eff_min:g}" if mixed else f"{exp.required_min:g}"
            exclusions.append(f"requires a minimum of {lowest} years ({', '.join(exp.required_mentions)}); profile must-have: "
                              f"nothing above {max_min_years} years" + ("; every stated requirement of this mixed-level opening exceeds it" if mixed else ""))
    if _SENIOR_TITLE.search(_MIXED_LEVEL.sub(" ", title)):
        exclusions.append(f"senior/lead/staff/manager-level title '{title}'; profile must-have: not a senior, staff, lead or manager role")
    elif mixed and not review:
        review = True
        gaps.append(f"REVIEW: mixed-level title '{title}' includes your level; confirm the opening for it exists")
    if _NON_SOFTWARE_TITLE.search(title):
        exclusions.append(f"non-software role by title '{title}'; profile exclusion")
    elif _QA_TITLE.search(title) and not _SOFTWARE_TITLE.search(title):
        non_sw = sorted({m.group(0).lower() for m in _NON_SOFTWARE_DESC.finditer(desc)})
        if non_sw:
            exclusions.append(f"quality role with non-software signals ({', '.join(non_sw[:4])}) in '{title}'; profile exclusion")
        else:
            hits = [t for t in _SOFTWARE_TERMS if t in desc.lower()]
            if hits:
                reasons.append(f"QA role with software evidence ({', '.join(hits[:3])})")
            else:
                gaps.append("unknown: QA role, software relevance not evident from the description (kept; missing keywords alone do not exclude)")
    india = _in_india(loc)
    if india is False:
        exclusions.append(f"location '{loc}' is outside India; profile must-have")
    elif india is None:
        gaps.append(f"unknown: location '{loc}' not recognised as India")

    # ---- experience gap and unknowns (flags only); full-time and total gaps are shown separately
    gap_ft = gap_total = None
    req = eff_min
    if req is None:
        gaps.append("unknown: experience requirement not stated")
    else:
        need = req * 12
        gap_ft = max(0.0, need - full_time_months)
        gap_total = None if exp.excludes_internship else max(0.0, need - total_months)
        where = (f"{gap_ft:g} short of your {full_time_months} full-time months (requirement excludes internships)"
                 if exp.excludes_internship else
                 f"{gap_ft:g} short of your {full_time_months} full-time months, {gap_total:g} short of your {total_months} total")
        if req == max_min_years:
            borderline = True
            gaps.append(f"BORDERLINE: requires exactly {max_min_years:g} years ({need:g} months): {where}")
        elif req <= max_min_years and (gap_ft > 0 or (gap_total or 0) > 0):
            gaps.append(f"gap: needs {need:g} months: {where}")
        elif req <= max_min_years:
            reasons.append(f"experience requirement ({req:g} yrs) within your {full_time_months} full-time months")
        if exp.alternatives and req >= max_min_years and not review:
            review = True
            gaps.append(f"REVIEW: alternative route stated: \"{exp.alternatives[0]}\"")
    if exp.preferred_mentions:
        gaps.append(f"preferred (not required): {', '.join(exp.preferred_mentions)}")
    if len(set(exp.required_mentions)) > 1:
        gaps.append(f"several experience statements: {', '.join(exp.required_mentions)}")
    if readiness == "short":
        gaps.append(f"SHORT description ({len(desc)} chars): likely incomplete, needs more information; nothing inferred")
    gaps += [f"link/listing check: {f}" for f in job.get("flags", [])]
    app_status, app_reason = application_url_status(job.get("application_url") or "") if job.get("application_url") else ("needs_review", "no application link")
    if app_status == "needs_review":
        better = next((o["link"] for o in job.get("application_options") or [] if application_url_status(o["link"])[0] == "structurally_valid"), None)
        gaps.append(f"REVIEW: application link not structurally valid ({app_reason})" + (f"; a structurally valid option exists: {better}" if better else ""))

    # ---- preferences (rank only)
    fit = 0.0
    role_hit = None
    for role in profile.desired_roles:
        pat = ROLE_PATTERNS.get(role.lower().strip(), re.escape(role.lower().strip()))
        if re.search(pat, title, re.I):
            role_hit = role
            break
    if disc in ADJACENT_DISCIPLINES:
        fit += 1
        reasons.append(f"adjacent discipline ({disc.replace('_', ' ')}); not one of your backend / platform / DevOps / SDET targets")
    elif role_hit and disc in TARGET_DISCIPLINES:
        fit += 3
        reasons.append(f"title matches desired role '{role_hit}' ({disc} work)")
    elif role_hit:
        fit += 2
        reasons.append(f"title matches desired role '{role_hit}'; discipline not evident from the posting")
    elif disc in TARGET_DISCIPLINES:
        fit += 2.5
        reasons.append(f"{disc} work")
    elif ADJACENT.search(title):
        fit += 1
        reasons.append("title is an adjacent role")
    else:
        gaps.append("title matches none of your desired roles")
    found = [k for k, pat in SKILLS.items() if re.search(pat, desc, re.I)]
    if found:
        fit += min(len(found), 4) * 0.5
        reasons.append(f"mentions {', '.join(found)}")
    if any(c in loc.lower() for c in PREFERRED_CITIES):
        fit += 1
        reasons.append(f"preferred location ({loc})")
    early = bool(EARLY.search(f"{title} {desc[:300]}"))
    if early:
        fit += 2
        reasons.append("explicit early-career level (fresher / junior / associate / SDE-1 / Engineer I)")
    elif req is not None and req <= 1:
        fit += 1
        reasons.append("requires at most 1 year")
    if borderline:
        fit -= 1
    if emp == "internship":
        fit -= 1.5
        gaps.append("internship (you prefer full-time; kept eligible)")
    elif emp == "contract":
        fit -= 0.5
        gaps.append("contract/consulting (you prefer permanent)")

    known = sum([req is not None, disc != "unspecified", readiness == "length_ok", india is True])
    confidence = "high" if known >= 4 else "medium" if known >= 3 else "low"
    target = disc in TARGET_DISCIPLINES
    title_evidence = bool(role_hit) or any(re.search(pat, title, re.I) for _, pat in _DISCIPLINE_TITLE)   # not body keywords alone
    if exclusions:
        match = "excluded"
    elif disc in ADJACENT_DISCIPLINES:
        match = "adjacent"
    elif (target and title_evidence and readiness == "length_ok" and not borderline and not review and emp not in ("internship", "contract")
          and confidence != "low" and (early or (req is not None and req * 12 <= full_time_months))):
        match = "strong"
    elif role_hit or target or ADJACENT.search(title):
        match = "possible"          # includes thin descriptions: never a confident strong match without enough JD
    else:
        match = "weak"
    return Screened(job, not exclusions, exclusions, reasons, gaps, round(fit, 2), confidence, exp, emp, borderline,
                    review, match, gap_ft, gap_total, disc, readiness, app_status)


def rank(screened: list[Screened]) -> tuple[list[Screened], list[Screened]]:
    level = {"strong": 0, "possible": 1, "adjacent": 2, "weak": 3}
    ok = sorted((s for s in screened if s.eligible),
                key=lambda s: (level[s.match], -s.fit, {"high": 0, "medium": 1, "low": 2}[s.confidence]))
    return ok, [s for s in screened if not s.eligible]
