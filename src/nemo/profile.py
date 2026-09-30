"""Job profile: the primary Phase 1 input (UTF-8 Markdown or plain text).

Five `##` sections, all required (a section may be empty):

    ## Experience       what you have done (free text or bullets) — background for matching
    ## Desired roles    one role per bullet — also drives the search queries
    ## Must-haves       one requirement per bullet; a job CONTRADICTING one is not a match
    ## Nice-to-haves    one preference per bullet; affects the explanation only
    ## Exclusions       one bullet each; a job SHOWING one is not a match

Anything a job posting does not state about these items is treated as unknown, never as met
or failed. Parsing is deterministic (no model); a later résumé converter can produce the same
`JobProfile`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

SECTIONS = {
    "experience": {"experience", "existing experience", "background"},
    "desired_roles": {"desired roles", "desired role", "target roles", "roles"},
    "must_haves": {"must-haves", "must haves", "must-have", "must have", "requirements"},
    "nice_to_haves": {"nice-to-haves", "nice to haves", "nice-to-have", "nice to have", "preferences"},
    "exclusions": {"exclusions", "exclude", "deal-breakers", "dealbreakers"},
}
_ALIAS = {alias: key for key, aliases in SECTIONS.items() for alias in aliases}

MAX_ROLES, MAX_CRITERIA, MAX_ITEM_CHARS, MAX_EXPERIENCE_CHARS = 8, 6, 160, 1200
MAX_FILE_BYTES = 64_000
MAX_PROFILE_CHARS = 2500  # leaves room for job text in a 4,096-token context

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


class ProfileError(ValueError):
    """The profile file is unreadable or invalid; the message says where and why."""


def _key(s: str) -> str:
    return " ".join(s.casefold().split())


class JobProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experience: str
    desired_roles: tuple[str, ...]
    must_haves: tuple[str, ...] = ()
    nice_to_haves: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()

    @field_validator("experience")
    @classmethod
    def _experience(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Experience is empty: describe what you have done")
        if len(v) > MAX_EXPERIENCE_CHARS:
            raise ValueError(f"Experience is {len(v)} characters; keep it under {MAX_EXPERIENCE_CHARS}")
        return v

    @field_validator("desired_roles", "must_haves", "nice_to_haves", "exclusions")
    @classmethod
    def _items(cls, v: tuple[str, ...], info) -> tuple[str, ...]:
        out, seen = [], set()
        for item in v:
            item = " ".join(item.split())
            if not item or _key(item) in seen:
                continue
            if len(item) > MAX_ITEM_CHARS:
                raise ValueError(f"{info.field_name}: item longer than {MAX_ITEM_CHARS} characters: {item[:40]!r}…")
            seen.add(_key(item))
            out.append(item)
        limit = MAX_ROLES if info.field_name == "desired_roles" else MAX_CRITERIA
        if len(out) > limit:
            raise ValueError(f"{info.field_name}: {len(out)} items; at most {limit} (one short item per bullet)")
        return tuple(out)

    @model_validator(mode="after")
    def _consistent(self) -> "JobProfile":
        if not self.desired_roles:
            raise ValueError("Desired roles is empty: list at least one role")
        total = len(self.experience) + sum(len(x) for x in self.desired_roles + self.must_haves
                                           + self.nice_to_haves + self.exclusions)
        if total > MAX_PROFILE_CHARS:
            raise ValueError(f"profile is {total} characters in total; keep it under {MAX_PROFILE_CHARS} "
                             "so job text still fits the model context")
        groups = {"must_haves": self.must_haves, "nice_to_haves": self.nice_to_haves, "exclusions": self.exclusions}
        names = list(groups)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                both = {_key(x) for x in groups[a]} & {_key(x) for x in groups[b]}
                if both:
                    raise ValueError(f"same item in {a} and {b}: {sorted(both)}")
        return self

    def criteria(self) -> list[tuple[str, str, str]]:
        """(id, kind, text) for every criterion the matcher must answer: M* must, N* nice, X* exclusion."""
        return ([(f"M{i}", "must_have", t) for i, t in enumerate(self.must_haves, 1)]
                + [(f"N{i}", "nice_to_have", t) for i, t in enumerate(self.nice_to_haves, 1)]
                + [(f"X{i}", "exclusion", t) for i, t in enumerate(self.exclusions, 1)])

    def sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def parse_profile(text: str) -> JobProfile:
    text = _COMMENT.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    found: dict[str, list[str]] = {}
    current: str | None = None
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        h = _HEADING.match(line.strip())
        if h and len(h.group(1)) == 1 and current is None and not found:
            continue  # optional "# Title" at the top
        if h:
            name = _key(h.group(2).rstrip(":"))
            if len(h.group(1)) != 2 or name not in _ALIAS:
                raise ProfileError(f"line {n}: unknown heading {line.strip()!r}; use '## ' with one of: "
                                   "Experience, Desired roles, Must-haves, Nice-to-haves, Exclusions")
            current = _ALIAS[name]
            if current in found:
                raise ProfileError(f"line {n}: section {h.group(2)!r} appears twice")
            found[current] = []
            continue
        if current is None:
            raise ProfileError(f"line {n}: text before the first '## ' section: {line.strip()[:40]!r}")
        b = _BULLET.match(line.strip())
        items = found[current]
        if b:
            items.append(b.group(1))
        elif current == "experience":
            items.append(line.strip())
        elif raw[:1] in (" ", "\t") and items:
            items[-1] += " " + line.strip()  # indented continuation of the previous bullet
        else:
            raise ProfileError(f"line {n}: in this section write one item per bullet ('- …'): {line.strip()[:40]!r}")
    missing = [k for k in SECTIONS if k not in found]
    if missing:
        raise ProfileError("missing section(s): " + ", ".join(m.replace("_", " ") for m in missing)
                           + " (a section may be empty, but its heading must be present)")
    try:
        return JobProfile(experience=" ".join(found["experience"]), desired_roles=tuple(found["desired_roles"]),
                          must_haves=tuple(found["must_haves"]), nice_to_haves=tuple(found["nice_to_haves"]),
                          exclusions=tuple(found["exclusions"]))
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        msgs = [e["msg"].removeprefix("Value error, ") for e in getattr(exc, "errors", lambda: [])()] or [str(exc)]
        raise ProfileError("; ".join(msgs)) from None


def load_profile(path: Path | str) -> JobProfile:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ProfileError(f"cannot read {p}: {exc.strerror}") from None
    if len(data) > MAX_FILE_BYTES:
        raise ProfileError(f"{p} is {len(data)} bytes; keep the profile under {MAX_FILE_BYTES}")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProfileError(f"{p} is not valid UTF-8 (byte {exc.start}); save it as UTF-8") from None
    return parse_profile(text)


def search_queries(profile: JobProfile, prefix: str = "site:linkedin.com/jobs/view/") -> list[str]:
    """Deterministic: one LinkedIn job-view query per desired role."""
    return [f"{prefix} {role}" for role in profile.desired_roles]


TEMPLATE = """\
# Job profile
<!-- UTF-8 Markdown. Keep all five sections; a section may be empty. One short item per bullet.
     Anything a job posting does not state about an item is treated as unknown. -->

## Experience
<!-- What you have done: roles, years, domains, main skills. Free text or bullets. -->

## Desired roles
<!-- One role per bullet. Each becomes a LinkedIn search. -->

## Must-haves
<!-- A job that clearly contradicts one of these is not a match. -->

## Nice-to-haves
<!-- Preferences; they shape the explanation, not the verdict. -->

## Exclusions
<!-- A job that clearly shows one of these is not a match. -->
"""
