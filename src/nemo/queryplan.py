"""Search plan from the job profile and config/search_terms.json: role families, early-career aliases, locations.

Order: queries are taken round-robin across role families (so a small budget covers every family), and each query
is assigned a location by rotating through the configured locations (so a small budget is not all Bengaluru).
Later passes shift the rotation, so every query is eventually searched in every location. No network here.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from nemo.discovery import Step, city_name, google_freshness

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "search_terms.json"


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    cfg = json.loads(Path(path or DEFAULT_CONFIG).read_text(encoding="utf-8"))
    if not isinstance(cfg.get("families"), dict):
        raise ValueError("search config needs a 'families' object")
    for name, fam in cfg["families"].items():
        if not fam.get("roles") or not fam.get("queries"):
            raise ValueError(f"family {name!r} needs 'roles' and 'queries'")
    return cfg


def select_families(desired_roles: list[str] | tuple[str, ...], cfg: dict[str, Any]) -> list[tuple[str, list[dict]]]:
    """(family, queries) for each family a desired role maps to; unmatched roles become plain-role queries."""
    chosen: list[tuple[str, list[dict]]] = []
    unmatched = []
    for role in desired_roles:
        hit = False
        for name, fam in cfg["families"].items():
            if any(re.search(p, role, re.I) for p in fam["roles"]):
                hit = True
                if name not in [c[0] for c in chosen]:
                    chosen.append((name, fam["queries"]))
        if not hit:
            unmatched.append(role)
    if unmatched:
        chosen.append(("other", [{"q": r, "kind": "plain"} for r in unmatched]))
    return chosen


def round_robin(families: list[tuple[str, list[dict]]]) -> list[tuple[str, dict]]:
    out, seen, depth = [], set(), max((len(q) for _, q in families), default=0)
    for i in range(depth):
        for name, queries in families:
            if i < len(queries) and queries[i]["q"].casefold() not in seen:
                seen.add(queries[i]["q"].casefold())
                out.append((name, queries[i]))
    return out


def build_plan(desired_roles, locations: list[str], lookback: timedelta, gl: str | None,
               cfg: dict[str, Any]) -> list[Step]:
    common = {"hl": "en", **({"gl": gl} if gl else {})}
    base = round_robin(select_families(desired_roles, cfg))
    steps: list[Step] = []
    if locations:
        for p in range(len(locations)):
            for k, (fam, q) in enumerate(base):
                loc = locations[(p + k) % len(locations)]
                steps.append(Step("google_jobs", fam, {"q": q["q"], "location": loc, **common}))
            if p == 0 and cfg.get("include_site_search"):
                cities = " OR ".join(city_name(l) for l in locations)
                for fam, q in base:
                    if q["kind"] == "plain":
                        steps.append(Step("google", fam, {"q": f'site:linkedin.com/jobs/view "{q["q"]}" ({cities})',
                                                          "num": "10", "tbs": google_freshness(lookback), **common}))
    return _pin_priority(steps, cfg.get("priority") or [], base)


def _pin_priority(steps: list[Step], priority: list[dict], base: list[tuple[str, dict]]) -> list[Step]:
    """Move explicitly pinned (query, city) searches to the front, in the listed order. Everything else keeps its place,
    so the pins reorder the plan without dropping any query/location pair (no permanent location exclusion)."""
    front: list[Step] = []
    for pin in priority:
        for st in steps:
            if (st.params["q"].casefold() == pin["q"].casefold() and st.engine == "google_jobs"
                    and city_name(st.params.get("location", "")).casefold() == pin["city"].casefold() and st not in front):
                front.append(st)
                break
    return front + [st for st in steps if st not in front]


def load_queries_file(path: Path | str, gl: str | None) -> list[Step]:
    """Explicit searches for a diagnostic or benchmark: [{"label", "engine": "google_jobs"|"google", "q", "location"?}].
    Google Jobs needs a location. Web ("google") searches get NO freshness filter unless the item gives "tbs"."""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items or len(items) > 20:
        raise ValueError("queries file must be a list of 1-20 searches")
    common = {"hl": "en", **({"gl": gl} if gl else {})}
    steps = []
    for i, it in enumerate(items, 1):
        engine, q = it.get("engine"), (it.get("q") or "").strip()
        if engine not in ("google_jobs", "google") or not q:
            raise ValueError(f"search {i}: needs engine google_jobs|google and a non-empty q")
        if engine == "google_jobs":
            if not it.get("location"):
                raise ValueError(f"search {i}: google_jobs needs a location")
            params = {"q": q, "location": it["location"], **common}
        else:
            params = {"q": q, "num": str(it.get("num", 10)), **common, **({"tbs": it["tbs"]} if it.get("tbs") else {})}
        steps.append(Step(engine, it.get("label") or f"query {i}", params))
    return steps
