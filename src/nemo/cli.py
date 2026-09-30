"""Command line entry point. M0: `init` and `brief check` only (all offline)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nemo.brief import TEMPLATE, Brief, BriefError, check_brief, load_brief
from nemo.plan import plan_queries
from nemo.providers import AVAILABLE

DEFAULT_BRIEF = Path("briefs/brief.yaml")


def _criteria_lines(brief: Brief) -> tuple[list[str], list[str]]:
    h, p = brief.hard, brief.preferred
    hard = []
    if h.locations:
        hard.append("locations: " + "; ".join(f"{r.place} ({r.mode})" for r in h.locations))
    if h.work_authorization:
        hard.append(f"work authorization: {h.work_authorization.model_dump(exclude_none=True)}")
    if h.min_salary:
        hard.append(f"min salary: {h.min_salary.amount:g} {h.min_salary.currency}/{h.min_salary.period}")
    if h.seniority:
        hard.append(f"seniority: {h.seniority.model_dump(exclude_none=True)}")
    if h.must_skills:
        hard.append("must skills: " + ", ".join(h.must_skills))
    ex = h.exclusions
    for label, vals in (("exclude companies", ex.companies), ("exclude title keywords", ex.title_keywords),
                        ("exclude industries", ex.industries)):
        if vals:
            hard.append(f"{label}: " + ", ".join(vals))
    hard.append(f"unknown hard values: {h.unknown_policy}")
    pref = []
    if p.skills:
        pref.append("skills: " + ", ".join(p.skills))
    if p.remote:
        pref.append(f"remote: {p.remote}")
    if p.salary_target:
        pref.append(f"salary target: {p.salary_target.amount:g} {p.salary_target.currency}/{p.salary_target.period}")
    if p.company_traits:
        pref.append("company traits: " + ", ".join(p.company_traits))
    return hard, pref


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    print(f"Wrote an empty brief template to {path}. Fill in your own values, then run: nemo brief check")
    return 0


def cmd_brief_check(args: argparse.Namespace) -> int:
    path = Path(args.brief)
    try:
        brief = load_brief(path)
    except BriefError as exc:
        print(f"{path}: invalid brief\n{exc}", file=sys.stderr)
        return 1

    hard, pref = _criteria_lines(brief)
    print(f"Brief: {path}\n\nHard criteria (violation excludes a job):")
    print("\n".join(f"  - {line}" for line in hard))
    print("Preferred criteria (ranking and notes only):")
    print("\n".join(f"  - {line}" for line in pref) if pref else "  (none)")

    queries, dropped = plan_queries(brief)
    b = brief.budgets
    print(f"\nPlanned deterministic queries: {len(queries)}"
          + (f" ({dropped} more dropped by max_queries)" if dropped else ""))
    for i, q in enumerate(queries, 1):
        print(f"  {i:>2}. [{q.purpose}] {q.text}")
    print(f"  (up to {min(b.max_query_variations, b.max_queries)} model-generated variations are reserved for M1)")
    print(f"\nCaps: queries {b.max_queries}, pages {b.max_pages}, LLM calls {b.max_llm_calls}, "
          f"wall {b.max_wall_seconds}s")
    print("Paid calls: " + (f"ENABLED up to ${b.max_usd:g}" if b.paid_calls_enabled else "DISABLED"))

    findings = check_brief(brief, available_providers=AVAILABLE)
    if findings:
        print("\nFindings:")
        for f in findings:
            print(f"  {f.level.upper():7} {f.message}")
    return 1 if any(f.level == "error" for f in findings) else 0


def cmd_search(args: argparse.Namespace) -> int:
    print("`nemo search` is not implemented yet (planned for M1).", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nemo", description="Personal job-search agent (Phase 1).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write an empty brief template")
    p_init.add_argument("--path", default=str(DEFAULT_BRIEF))
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_brief = sub.add_parser("brief", help="brief utilities")
    brief_sub = p_brief.add_subparsers(dest="brief_command", required=True)
    p_check = brief_sub.add_parser("check", help="validate the brief and show what would be searched")
    p_check.add_argument("--brief", default=str(DEFAULT_BRIEF))
    p_check.set_defaults(func=cmd_brief_check)

    p_search = sub.add_parser("search", help="run a search (not implemented yet)")
    p_search.add_argument("--brief", default=str(DEFAULT_BRIEF))
    p_search.set_defaults(func=cmd_search)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
