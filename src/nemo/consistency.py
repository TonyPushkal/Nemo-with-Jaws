"""Listing ↔ LinkedIn-link consistency. Uses only the listing fields and the LinkedIn URL slug;
linkedin.com is never fetched."""

from __future__ import annotations

import re
import urllib.parse

CITIES = ["bengaluru", "bangalore", "hyderabad", "visakhapatnam", "vizag", "chennai", "pune", "mumbai", "delhi",
          "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad", "indore", "kochi", "coimbatore", "jaipur"]
ALIASES = {"bangalore": "bengaluru", "vizag": "visakhapatnam", "gurgaon": "gurugram"}
STOP = {"at", "the", "and", "of", "in", "for", "a", "an", "sr", "senior", "jr", "ii", "iii", "i", "inc", "ltd", "pvt",
        "private", "limited", "llc", "gmbh", "co"}


def words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", s.casefold()) if w not in STOP]


def slug_parts(url: str) -> tuple[str, str]:
    """('title words', 'company words') from /jobs/view/<title>-at-<company>-<id>."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    slug = re.sub(r"-?\d{6,}$", "", path.rsplit("/jobs/view/", 1)[-1].strip("/"))
    title, _, company = slug.rpartition("-at-") if "-at-" in slug else (slug, "", "")
    return title.replace("-", " "), company.replace("-", " ")


def coverage(a: list[str], b: list[str]) -> float:
    return sum(w in set(b) for w in a) / len(a) if a else 0.0


def cities_in(text: str) -> set[str]:
    t = text.casefold()
    return {ALIASES.get(c, c) for c in CITIES if re.search(rf"\b{c}\b", t)}


def check(job: dict, link: str) -> list[str]:
    """Problems between a listing (title, company_name, location, description) and its LinkedIn link."""
    problems = []
    s_title, s_company = slug_parts(link)
    company = job.get("company_name") or ""
    if not s_company:
        problems.append("LinkedIn slug has no company part to compare")
    elif max(coverage(words(s_company), words(company)), coverage(words(company), words(s_company))) < 0.5:
        problems.append(f"company mismatch: listing {company!r} vs slug {s_company!r}")
    t_cov = coverage(words(job.get("title") or ""), words(s_title))
    if t_cov < 0.6:
        problems.append(f"title mismatch ({t_cov:.0%} of listing title words in slug): {job.get('title')!r} vs {s_title!r}")
    loc, desc = cities_in(job.get("location") or ""), cities_in(job.get("description") or "")
    if loc and desc and not (loc & desc):
        problems.append(f"location mismatch: listing {sorted(loc)} vs description {sorted(desc)}")
    return problems


def is_clear_mismatch(problems: list[str]) -> bool:
    """Exclude only a different company or a clearly different title; other problems are flags."""
    return any(p.startswith(("company mismatch", "title mismatch")) for p in problems)
