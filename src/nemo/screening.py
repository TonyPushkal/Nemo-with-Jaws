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
_SENIOR_TITLE = re.compile(r"\b(senior|sr\b\.?|staff|principal|lead|leader|architect|manager|head|director|"
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
    "software engineer": r"\bsde\b|software (development )?engineer|software developer",
}
ADJACENT = re.compile(r"full[- ]?stack|data engineer", re.I)

# ---- Role discipline, from the title AND the responsibilities (counts of distinct signal groups in the description).
# Default shortlist = SDE / backend. Primarily infrastructure-operations, DevOps, SRE, QA and testing roles are excluded
# (kept in the output with the reason); mixed or unclear roles are flagged for review, not guessed.
_FRONT_TITLES = (
    ("frontend", r"front[- ]?end|\bui\b|\bux\b|react|angular|\bvue\b|web developer|javascript developer"),
    ("data_science", r"data scien|data analyst|research scientist|\bml scientist|machine learning scientist|analytics"),
    ("mobile", r"android|\bios\b|mobile"),
    ("fullstack", r"full[- ]?stack"),
)
_INFRA_TITLE = re.compile(r"devops|devsecops|\bsre\b|site reliability|reliability engineer|platform engineer|cloud (engineer|infrastructure|"
                          r"support|operations|architect)|infrastructure engineer|release engineer|systems? (engineer|administrator)|"
                          r"network engineer|production support", re.I)
_QA_TITLE_CLASS = re.compile(r"sdet|\bqa\b|quality (assurance|engineer|analyst)|test (engineer|automation|analyst)|engineer in test|"
                             r"automation test|testing engineer", re.I)
_AI_TITLE = re.compile(r"\bai\b|\bml\b|machine learning|\bllm\b|genai|generative ai|deep learning|\bnlp\b", re.I)
_BACKEND_TITLE = re.compile(r"back[- ]?end|server[- ]side|\bapi\b", re.I)
_SOFTWARE_ROLE_TITLE = re.compile(r"software (development )?engineer|software developer|\bsde\b|\bdeveloper\b|\bprogrammer\b", re.I)
_BE_GROUPS = {
    "APIs": r"\bapis?\b|\brest(ful)?\b|graphql|grpc", "microservices": r"microservices?|service[- ]oriented",
    "backend": r"back[- ]?end|server[- ]side", "databases": r"\b(databases?|sql|nosql|postgres(ql)?|mysql|mongodb|redis|sqlalchemy|orm|dynamodb)\b",
    "distributed systems": r"distributed systems?|scalab(le|ility)|high[- ]throughput|concurrency|caching",
    "backend language": r"\b(python|java|golang|go lang|node\.?js|spring( boot)?|django|flask|fastapi|kotlin|c#|\.net)\b",
    "system design": r"system design|data structures|algorithms|design patterns|code reviews?"}
_INFRA_GROUPS = {
    "devops/SRE": r"devops|\bsre\b|site reliability|devsecops", "infrastructure as code": r"terraform|ansible|pulumi|cloudformation|infrastructure as code|\biac\b",
    "cluster operations": r"kubernetes (cluster|administration|operations)|cluster (management|operations|upgrades?)|helm charts?|argo ?cd|gitops",
    "pipelines/release": r"ci/?cd pipelines?|build (and|&) release|release (engineering|management)|jenkins|deployment pipelines?",
    "monitoring/on-call": r"monitoring|observability|prometheus|grafana|alerting|on[- ]call|incident (response|management)|uptime|capacity planning",
    "system administration": r"linux (system )?administration|sysadmin|system administration|infrastructure (provisioning|operations|management)|"
                             r"server (provisioning|maintenance)|networking|firewall"}
_QA_GROUPS = {
    "test cases": r"test (cases?|plans?|scenarios?|strategy)", "test automation tools": r"selenium|playwright|cypress|appium|testng|robot framework|webdriver",
    "QA work": r"\bqa\b|quality assurance|manual testing|regression testing|functional testing|bug (reports?|triage)"}
_AI_PRODUCT = re.compile(r"\bllm\b|\brag\b|genai|generative ai|openai|langchain|vector (db|database|store)|embeddings?", re.I)
_AI_GROUPS = {
    "LLMs": r"\bllms?\b|large language models?", "RAG": r"\brag\b|retrieval[- ]augmented", "agents": r"\bagent(s|ic)?\b|multi[- ]agent|langchain|llamaindex",
    "vector search": r"vector (search|db|database|store)|embeddings?", "ML/NLP": r"machine learning|deep learning|\bnlp\b|model training|fine[- ]?tun"}

# Required-stack fit (separate from backend-role fit). Python-compatible and language-flexible roles rank first; other stacks are
# kept visible as gaps, never silently excluded and never presented as equally strong.
_LANG = {
    "Python": r"\bpython\b|\bdjango\b|\bflask\b|\bfastapi\b",
    "Java/Spring": r"\bjava\b|\bspring( boot)?\b|\bhibernate\b|\bj2ee\b",
    "Node.js/TypeScript": r"node\.?js|\bexpress(\.js)?\b|\btypescript\b|\bnestjs\b",
    "Go": r"\bgolang\b|\bgo lang\b|\bgo (language|programming|developer)\b|\b(python|java|node(\.?js)?)[,/ ]+(or )?go\b|\bgo[,/ ]+(python|java|node)",
    "C#/.NET": r"\bc#|\.net\b|asp\.net", "PHP": r"\bphp\b|laravel|symfony", "Ruby": r"\bruby\b|\brails\b",
    "C/C++": r"\bc\+\+|\bc/c\+\+", "Kotlin/Scala": r"\bkotlin\b|\bscala\b", "Rust": r"\brust\b"}


def stack_assessment(desc: str) -> dict[str, Any]:
    """fit: 'python' (Python or a Python framework is named among the required stack), 'flexible' (no backend language is named),
    'gap' (a different required language and no Python). Languages named only as preferred ('a plus') do not count as required."""
    text = (desc or "")[:3500]
    required: list[str] = []
    python_plus = False
    for sent in _SPLIT.split(text):
        if _PREFERRED.search(sent):
            python_plus = python_plus or bool(re.search(_LANG["Python"], sent, re.I))
            continue
        for lang, pat in _LANG.items():
            if lang not in required and re.search(pat, sent, re.I):
                required.append(lang)
    if "Python" in required:
        others = [x for x in required if x != "Python"]
        return {"fit": "python", "languages": required, "note": "Python named" + (f"; also lists {', '.join(others)}" if others else "")}
    if not required:
        return {"fit": "flexible", "languages": [], "note": "no backend language named in the description"}
    return {"fit": "gap", "languages": required,
            "note": f"requires {', '.join(required)}; " + ("Python is only listed as a plus" if python_plus else "Python is not mentioned")}


TARGET_DISCIPLINES = {"backend"}
ADJACENT_DISCIPLINES = {"frontend", "fullstack", "data_science", "mobile"}
DEFAULT_EXCLUDE_NOTE = "excluded from the default SDE/backend shortlist (profile: not infrastructure operations, DevOps, SRE, QA or testing)"


def _groups(table: dict[str, str], text: str) -> list[str]:
    return [name for name, pat in table.items() if re.search(pat, text, re.I)]


def classify_role(title: str, desc: str) -> dict[str, Any]:
    """Role discipline from title and responsibilities. Returns discipline plus the evidence and, when applicable, an
    `exclude` reason (primarily infrastructure operations / DevOps / SRE or QA / testing) or a `review` reason (ambiguous)."""
    title, head = title or "", (desc or "")[:3000]
    be, infra, qa = _groups(_BE_GROUPS, head), _groups(_INFRA_GROUPS, head), _groups(_QA_GROUPS, head)
    out: dict[str, Any] = {"discipline": "unspecified", "backend_evidence": be, "infra_evidence": infra, "qa_evidence": qa,
                           "ai_product": bool(_AI_PRODUCT.search(head)), "exclude": None, "review": None,
                           "title_backend": bool(_BACKEND_TITLE.search(title))}
    hits = [n for n, pat in _FRONT_TITLES if re.search(pat, title, re.I)]
    be_t, sw_t = bool(_BACKEND_TITLE.search(title)), bool(_SOFTWARE_ROLE_TITLE.search(title))
    if hits:
        out["discipline"] = "fullstack" if "frontend" in hits and (be_t or "fullstack" in hits) else min(hits, key=["frontend", "data_science", "mobile", "fullstack"].index)
        return out
    if _QA_TITLE_CLASS.search(title):
        out.update(discipline="qa_testing", exclude=f"QA / testing role by title '{title}'")
    elif _INFRA_TITLE.search(title):
        if be_t or sw_t:
            out.update(discipline="infra_ops", review=f"title mixes infrastructure and software-engineering terms ('{title}')")
        else:
            out.update(discipline="infra_ops", exclude=f"primarily infrastructure / DevOps / SRE role by title '{title}'")
    elif _AI_TITLE.search(title) and not be_t:
        ai = _groups(_AI_GROUPS, head)
        out["ai_evidence"] = ai
        if len(ai) >= 2:     # the work is AI engineering (LLMs, RAG, agents, vector search), not primarily backend software
            out.update(discipline="ai_ml", review=f"AI/ML title ('{title}') and the responsibilities centre on AI engineering ({', '.join(ai)}); "
                                                   "backend work is incidental: moved out of the default SDE/backend shortlist")
        elif len(be) >= 3 and len(be) > len(infra):
            out.update(discipline="backend", review=None)
        else:
            out.update(discipline="ai_ml", review=f"AI/ML title ('{title}'): confirm it is a software-engineering role, not research or data science")
    else:
        if len(qa) >= 3 and len(qa) > len(be):
            out.update(discipline="qa_testing", exclude=f"primarily QA / testing responsibilities ({', '.join(qa)}) for '{title}'")
        elif len(infra) >= 3 and len(infra) >= 2 * len(be):
            out.update(discipline="infra_ops", exclude=f"primarily infrastructure-operations responsibilities ({', '.join(infra)}) for '{title}'")
        elif len(infra) >= 3 and (len(infra) >= len(be) + 3 if be_t else len(infra) > len(be)):
            out.update(discipline="infra_ops", review=f"responsibilities mix backend work ({', '.join(be) or 'none'}) with infrastructure operations ({', '.join(infra)})")
        elif len(be) >= 2 and len(be) >= len(infra) and len(be) >= len(qa):
            out["discipline"] = "backend"
        elif be_t:
            out["discipline"] = "backend"
        elif len(desc or "") >= 500:
            out.update(discipline="unspecified", review="responsibilities do not clearly show backend / software-engineering work")
    return out


def discipline(title: str, desc: str) -> str:
    return classify_role(title, desc)["discipline"]


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


def employment_type(title: str, description: str, schedule: str | None = None) -> str:
    """internship | contract | full_time | unspecified. `schedule` is Google Jobs' own label (unverified). Internship or contract
    wording in the title/description wins over a Full-time label; 'unspecified' means no source states the type."""
    head = f"{title} {(description or '')[:400]}"
    sched = (schedule or "").lower()
    if re.search(r"\bintern(ship)?\b", title or "", re.I) or re.search(r"\bintern(ship)?\b", (description or "")[:300], re.I) or "intern" in sched:
        return "internship"
    if re.search(r"\bcontract(or)?\b|\bconsultant\b|\bFTC\b|fixed[- ]term", head, re.I) or re.search(r"contract|temporary", sched):
        return "contract"
    if re.search(r"full[–\-‑ ]?time", sched):
        return "full_time"
    return "unspecified"


_CITY_ALIASES = {"bangalore": "bengaluru", "bengaluru urban": "bengaluru", "bangalore urban": "bengaluru", "bangalore rural": "bengaluru",
                 "bengaluru rural": "bengaluru", "secunderabad": "hyderabad", "cyberabad": "hyderabad", "hitec city": "hyderabad"}
_OTHER_CITIES = ("pune", "mumbai", "navi mumbai", "thane", "chennai", "delhi", "new delhi", "gurugram", "gurgaon", "noida", "greater noida",
                 "ghaziabad", "faridabad", "kolkata", "ahmedabad", "vadodara", "surat", "indore", "bhopal", "jaipur", "kochi", "coimbatore",
                 "chandigarh", "mohali", "lucknow", "nagpur", "bhubaneswar", "visakhapatnam", "vizag", "vijayawada", "hosur", "mysuru",
                 "mysore", "pimpri-chinchwad", "pimpri chinchwad", "thiruvananthapuram", "trivandrum", "nashik", "patna", "ranchi", "mangaluru")


def location_fit(location: str, chosen_cities: tuple[str, ...] | list[str] | None) -> tuple[str, str]:
    """('chosen'|'remote'|'other'|'unknown', city). 'other' = a recognised Indian city that is not one of the chosen cities;
    'unknown' = no city stated (e.g. just 'India' or a state)."""
    loc = (location or "").lower()
    if re.search(r"\bremote\b|work from home|\bwfh\b|\banywhere\b", loc):
        return "remote", ""
    first = re.sub(r"\s*\(\+\d+ others?\)", "", loc).split(",")[0].strip()
    norm = lambda c: _CITY_ALIASES.get(c.strip().lower(), c.strip().lower())
    chosen = {norm(c) for c in chosen_cities or ()}
    city = norm(first)
    if city in chosen:
        return "chosen", city
    if city in {norm(c) for c in _OTHER_CITIES}:
        return "other", city
    return "unknown", city


def _in_india(location: str) -> bool | None:
    t = (location or "").lower()
    if "india" in t or any(s in t for s in _STATES) or any(c in t for c in PREFERRED_CITIES + _OTHER_INDIAN_CITIES):
        return True
    return False if _ABROAD.search(location or "") else None


_RESP_HEADER = re.compile(r"responsibilit|what you.?ll (do|work on|be doing)|key (duties|tasks)|your role|role overview|the role|day[- ]to[- ]day|you will\b", re.I)
_ACTION_SENTENCE = re.compile(r"(?:^|[.!?\n•;]\s*)(design|build|develop|implement|own|maintain|collaborate|write|deploy|work with|work on|participate|lead|create|"
                              r"ensure|optimi[sz]e|integrate|troubleshoot|architect|contribute|improve|debug|review|support|scale)\b", re.I)
_REQ_HEADER = re.compile(r"requirements?|qualifications?|what we.?re looking for|who you are|must[- ]have|you have\b|skills|"
                         r"proficien|knowledge of|familiar(ity)? with|experience (with|in|of)|(experience|exp)\s*[:\-]|\d\s*\+?\s*years?", re.I)
_TRUNCATED = re.compile(r"(\.\.\.|…|show more|see more|read more)\s*$", re.I)


def description_content(desc: str) -> dict[str, Any]:
    """What the description actually contains, not how long it is. 'content_ok' = it describes the work (responsibilities) AND
    states requirements and is not cut off; 'partial' = only one of them, or truncated; 'thin' = neither. A heuristic: it
    does not prove the posting is complete, only that Nemo has the parts it needs to assess and later tailor a resume."""
    t = (desc or "").strip()
    resp = bool(_RESP_HEADER.search(t)) or len(_ACTION_SENTENCE.findall(t)) >= 3
    req = bool(_REQ_HEADER.search(t))
    truncated = bool(_TRUNCATED.search(t))
    level = "thin" if len(t) < 100 or not (resp or req) else "content_ok" if resp and req and not truncated else "partial"
    missing = [n for n, ok in (("responsibilities", resp), ("requirements", req)) if not ok] + (["text is cut off"] if truncated else [])
    return {"level": level, "responsibilities": resp, "requirements": req, "truncated": truncated, "missing": missing}


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
    readiness: str = "content_ok"    # content_ok | partial | thin: judged from content (responsibilities + requirements), not length
    application_status: str = "structurally_valid"  # structurally_valid | needs_review (URL shape only; destination not checked)
    stack_fit: str = "flexible"      # python | flexible | gap (required-stack fit, separate from backend-role fit)
    stack_note: str = ""


def screen(job: dict[str, Any], profile: JobProfile, *, max_min_years: int = MAX_MIN_YEARS,
           total_months: int = TOTAL_MONTHS, full_time_months: int = FULL_TIME_MONTHS,
           chosen_cities: tuple[str, ...] | list[str] | None = None) -> Screened:
    title, desc, loc = job.get("title") or "", job.get("description") or "", job.get("location") or ""
    exclusions, reasons, gaps, borderline, review = [], [], [], False, False
    exp = parse_experience(title, desc)
    emp = employment_type(title, desc, job.get("schedule_type"))
    cls = classify_role(title, desc)
    disc = cls["discipline"]
    stack = stack_assessment(desc)
    content = description_content(desc)
    readiness = content["level"]
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
    if cls["exclude"] and not any(e.startswith(("non-software", "quality role")) for e in exclusions):
        exclusions.append(f"{cls['exclude']}; {DEFAULT_EXCLUDE_NOTE}")
    if cls["review"]:
        review = True
        gaps.append(f"REVIEW: ambiguous role: {cls['review']}")
    if chosen_cities:
        where, city = location_fit(loc, chosen_cities)
        if where == "other":
            exclusions.append(f"location '{loc}' is outside the chosen cities ({', '.join(chosen_cities)}) and not remote; "
                              "profile: Bengaluru or Hyderabad (relocation fine) or remote")
        elif where == "unknown":
            gaps.append(f"unknown: no city stated in '{loc}'; chosen cities are {', '.join(chosen_cities)}")
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
    if readiness != "content_ok":
        gaps.append(f"{readiness.upper()} description ({len(desc)} chars; missing: {', '.join(content['missing']) or 'nothing identified'}): "
                    "needs more information before it is relied on; nothing inferred")
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
    be_ev = cls["backend_evidence"]
    if disc in ADJACENT_DISCIPLINES:
        fit += 1
        reasons.append(f"adjacent discipline ({disc.replace('_', ' ')}); not backend / SDE work")
    elif disc == "backend":
        fit += (3 if role_hit or cls["title_backend"] else 2.5) + min(len(be_ev), 4) * 0.75
        reasons.append("backend responsibilities: " + (", ".join(be_ev) if be_ev else "title says backend; responsibilities not shown")
                       + ("; AI product work" if cls["ai_product"] else ""))
        if re.search(r"\bgcp\b|\baws\b|google cloud|azure|kubernetes|cloud", desc, re.I):
            reasons.append("software role involving cloud infrastructure")
    elif role_hit:
        fit += 2
        reasons.append(f"title matches desired role '{role_hit}'; discipline not evident from the posting")
    elif ADJACENT.search(title):
        fit += 1
        reasons.append("title is an adjacent role")
    else:
        gaps.append("title matches none of your desired roles")
    if stack["fit"] == "python":
        fit += 1
        reasons.append(f"stack: {stack['note']}")
    elif stack["fit"] == "gap":
        fit -= 1.5
        gaps.append(f"STACK GAP: {stack['note']} (kept visible; ranked below Python-compatible roles)")
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

    known = sum([req is not None, disc != "unspecified", readiness == "content_ok", india is True])
    confidence = "high" if known >= 4 else "medium" if known >= 3 else "low"
    target = disc in TARGET_DISCIPLINES
    title_evidence = bool(role_hit) or cls["title_backend"]            # not body keywords alone
    responsibilities_shown = len(be_ev) >= 2                            # strong needs backend responsibilities, not a title alone
    if exclusions:
        match = "excluded"
    elif disc in ADJACENT_DISCIPLINES:
        match = "adjacent"
    elif (target and title_evidence and responsibilities_shown and readiness == "content_ok" and stack["fit"] != "gap" and not borderline and not review and emp not in ("internship", "contract")
          and confidence != "low" and (early or (req is not None and req * 12 <= full_time_months))):
        match = "strong"
    elif role_hit or target or ADJACENT.search(title) or disc in ("ai_ml", "infra_ops"):
        match = "possible"          # includes thin descriptions: never a confident strong match without enough JD
    else:
        match = "weak"
    return Screened(job, not exclusions, exclusions, reasons, gaps, round(fit, 2), confidence, exp, emp, borderline,
                    review, match, gap_ft, gap_total, disc, readiness, app_status, stack["fit"], stack["note"])


def rank(screened: list[Screened]) -> tuple[list[Screened], list[Screened]]:
    level = {"strong": 0, "possible": 1, "adjacent": 2, "weak": 3}
    stack = {"python": 0, "flexible": 1, "gap": 2}
    ok = sorted((s for s in screened if s.eligible),
                key=lambda s: (level[s.match], stack[s.stack_fit], -s.fit, {"high": 0, "medium": 1, "low": 2}[s.confidence]))
    return ok, [s for s in screened if not s.eligible]
