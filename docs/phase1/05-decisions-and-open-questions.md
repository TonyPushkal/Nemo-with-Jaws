# Phase 1 — Decisions, Assumptions, Open Questions

## Confirmed by you
- Three-phase roadmap; this plan is **Phase 1 only** (find relevant jobs). Phase 1 never edits résumés, runs ATS checks, contacts recruiters or applies.
- Requirements 1–8 in your brief (search brief with hard/preferred, varied searches, read postings + employer pages, evidence-based fit with gaps/unknowns, dedup + history, result fields, failure reporting, on-demand first).
- Skillsheet is ignored. Reference repos stay unchanged. Their résumé/profile data is not yours and is not used.
- **Interface: Python CLI.**
- **LinkedIn: search-surfaced links only**, no login, no crawling; verify on the employer page where possible.
- **Search/LLM provider: undecided** → provider-agnostic design plus a bake-off (M0b).
- Your search preferences (roles, locations, salary, authorization, exclusions, companies) are **yours to supply**; none are assumed in code or docs.

## Decisions made by me (change any of them)
- No copying of code from either reference repo (no licence files; GitHub reports none). Ideas only.
- No agent framework; fixed pipeline with one bounded extra search round.
- SQLite for history; Markdown + JSON for output (no CSV/Excel/email in v1).
- Verdict tiers instead of match percentages; every claim must quote the source text.
- Unknown hard criteria are flagged, not silently dropped (`unknown_policy: flag`).

## Assumptions (unverified)
- Single user, macOS, runs locally, English-language postings.
- schema.org `JobPosting` JSON-LD is common enough on career pages to be worth parsing first (measure in M1).
- Tavily's index will surface useful posting pages for niche queries (measure in M0b).
- Budget defaults in doc 03 (~30 queries, ~60 pages, ~40 assessments, ~15 min) are a starting point, not derived from data.
- Model names/capabilities for OpenAI `web_search` in the docs I fetched need re-confirming at build time.
- Lever's posting date availability was not found in the docs read; do not rely on it.

## Open questions (none block approval of the plan; several block M1)
1. **Monthly spend ceiling** for search + model calls? (Sets default `max_usd` and how aggressive expansion can be.)
2. **Which LLM vendor for assessment** (OpenAI, Anthropic, other) — only needed once M0b is done.
3. **Résumé:** which file, and how much weight should it have (background statements only, as proposed)?
4. **Repo visibility/licence** for Nemo-with-Jaws: your brief and résumé are gitignored regardless; pick a licence before publishing anything.
5. **Brief authoring:** hand-edited YAML (proposed) vs a guided Q&A that writes the YAML.
6. **Unknown hard criteria** (e.g. salary not posted): keep flagged (proposed) or drop?
7. **Job types** in scope (full-time, contract, part-time) and **geographies**: these live in your brief; confirm you are happy to fill it in at M0.
8. **Evaluation labels:** willing to label ~20 seed jobs plus each early shortlist (≈15–20 min per run)?
9. **Targets** in doc 04: acceptable as starting goals?
