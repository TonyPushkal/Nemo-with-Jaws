"""Search brief: schema, template, loading and checks.

The brief holds the user's own preferences. Nothing here supplies defaults for
them: the template is empty, and `check_brief` reports what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos in the YAML are errors


class AboutMe(_Model):
    """Background only. Never used as a hard filter."""

    summary: Text | None = None
    resume_path: Text | None = None


class Company(_Model):
    name: Text
    careers_url: Text | None = None


class Search(_Model):
    target_titles: list[Text] = []
    related_titles: list[Text] = []
    responsibilities: list[Text] = []
    companies_preferred: list[Company] = []
    freshness_max_age_days: int | None = Field(default=None, ge=1)


class LocationRule(_Model):
    place: Text
    mode: Literal["onsite", "hybrid", "remote"]


class WorkAuthorization(_Model):
    countries: list[Text] = []
    needs_sponsorship: bool | None = None


class Salary(_Model):
    amount: float = Field(gt=0)
    currency: Text
    period: Literal["year", "month", "hour"] = "year"


class Exclusions(_Model):
    companies: list[Text] = []
    title_keywords: list[Text] = []
    industries: list[Text] = []


class Seniority(_Model):
    allowed: list[Text] = []
    years_experience: float | None = Field(default=None, ge=0)


class Hard(_Model):
    """A violation removes the job; an unknown follows `unknown_policy`."""

    locations: list[LocationRule] = []
    work_authorization: WorkAuthorization | None = None
    min_salary: Salary | None = None
    exclusions: Exclusions = Exclusions()
    seniority: Seniority | None = None
    must_skills: list[Text] = []
    unknown_policy: Literal["flag", "drop"] = "flag"


class Preferred(_Model):
    """Affects ranking and notes only."""

    skills: list[Text] = []
    remote: Text | None = None
    salary_target: Salary | None = None
    company_traits: list[Text] = []


class Providers(_Model):
    """Provider names; null = not chosen. Provider selection is undecided."""

    search: Text | None = None
    llm: Text | None = None
    fetch: Text | None = None


class Budgets(_Model):
    paid_calls_enabled: bool = False
    max_usd: float | None = Field(default=None, ge=0)
    max_queries: int = Field(default=30, ge=0)
    max_query_variations: int = Field(default=10, ge=0)  # counted inside max_queries
    max_pages: int = Field(default=60, ge=0)
    max_llm_calls: int = Field(default=40, ge=0)
    max_wall_seconds: int = Field(default=900, ge=1)

    @model_validator(mode="after")
    def _paid_needs_ceiling(self) -> "Budgets":
        if self.paid_calls_enabled and self.max_usd is None:
            raise ValueError("budgets.paid_calls_enabled is true but budgets.max_usd is not set")
        return self


class Brief(_Model):
    version: Literal[1]
    about_me: AboutMe = AboutMe()
    search: Search = Search()
    hard: Hard  # required: the hard/preferred distinction must be explicit
    preferred: Preferred
    providers: Providers = Providers()
    budgets: Budgets = Budgets()

    @model_validator(mode="after")
    def _no_criterion_in_both(self) -> "Brief":
        hard = {s.casefold() for s in self.hard.must_skills}
        both = sorted(s for s in self.preferred.skills if s.casefold() in hard)
        if both:
            raise ValueError(f"skills listed as both hard and preferred: {', '.join(both)}")
        return self


TEMPLATE = """\
# Nemo-with-Jaws search brief. Fill in your own values; nothing here is pre-filled.
# `hard`  : a violation removes the job (unknown values follow unknown_policy).
# `preferred`: affects ranking and notes only.
# Keep this file private (briefs/ is gitignored).
version: 1

about_me:                       # background only, never a hard filter
  summary: null
  resume_path: null             # optional local file, read-only

search:
  target_titles: []
  related_titles: []
  responsibilities: []          # the work you want to do, in your words
  companies_preferred: []       # each: {name: ..., careers_url: ...}
  freshness_max_age_days: null

hard:
  locations: []                 # each: {place: ..., mode: onsite|hybrid|remote}
  work_authorization: null      # {countries: [], needs_sponsorship: true|false}
  min_salary: null              # {amount: ..., currency: ..., period: year|month|hour}
  exclusions: {companies: [], title_keywords: [], industries: []}
  seniority: null               # {allowed: [], years_experience: ...}
  must_skills: []
  unknown_policy: flag          # flag = keep and mark "needs check"; drop = exclude

preferred:
  skills: []
  remote: null
  salary_target: null
  company_traits: []

providers:                      # null = not chosen (selection is undecided)
  search: null
  llm: null
  fetch: null

budgets:
  paid_calls_enabled: false     # paid calls are refused unless true AND max_usd is set
  max_usd: null
  max_queries: 30
  max_query_variations: 10
  max_pages: 60
  max_llm_calls: 40
  max_wall_seconds: 900
"""


class BriefError(Exception):
    """The brief file could not be read or failed validation."""


def parse_brief(text: str) -> Brief:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BriefError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise BriefError("the brief must be a YAML mapping")
    try:
        return Brief.model_validate(raw)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"]) or "(brief)"
            lines.append(f"{where}: {err['msg']}")
        raise BriefError("\n".join(lines)) from exc


def load_brief(path: Path) -> Brief:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BriefError(f"cannot read {path}: {exc.strerror}") from exc
    return parse_brief(text)


@dataclass(frozen=True)
class Finding:
    level: Literal["error", "warning", "info"]
    message: str


def check_brief(brief: Brief, *, available_providers: dict[str, set[str]] | None = None) -> list[Finding]:
    """Semantic checks beyond the schema. Never invents defaults."""
    available_providers = available_providers or {}
    out: list[Finding] = []
    if not brief.search.target_titles:
        out.append(Finding("error", "search.target_titles is empty: there is nothing to search for"))
    h = brief.hard
    if not (h.locations or h.work_authorization or h.min_salary or h.seniority or h.must_skills
            or h.exclusions.companies or h.exclusions.title_keywords or h.exclusions.industries):
        out.append(Finding("warning", "no hard criteria set: nothing will be excluded automatically"))
    if not h.locations:
        out.append(Finding("warning", "hard.locations is empty: location will not be checked"))
    if brief.about_me.resume_path and not Path(brief.about_me.resume_path).expanduser().is_file():
        out.append(Finding("error", f"about_me.resume_path not found: {brief.about_me.resume_path}"))
    for kind, name in (("search", brief.providers.search), ("llm", brief.providers.llm),
                       ("fetch", brief.providers.fetch)):
        if name is None:
            out.append(Finding("info", f"providers.{kind} not chosen"))
        elif name not in available_providers.get(kind, set()):
            out.append(Finding("warning", f"providers.{kind}={name!r} is not implemented yet"))
    if not brief.budgets.paid_calls_enabled:
        out.append(Finding("info", "paid calls are disabled (budgets.paid_calls_enabled is false)"))
    return out
