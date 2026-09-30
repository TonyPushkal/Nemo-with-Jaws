# Phase 1 — Milestones and Tests (v3)

Status: DRAFT v3.2. Deliberately small.

## M0 — Scaffold (done) and guard fix (done in v3.1)
Project skeleton, provider interfaces, offline fakes, no-network test fixture, and the corrected monetary guard with its SQLite spend ledger (92 offline tests pass).

## M1 — One complete résumé-to-LinkedIn-results run
Deliverable: `python scripts/run_search.py --resume <file> --lookback 7d` produces a real result set (or an honest empty one) using the providers you choose, persisted in SQLite. Built in this order:

1. **Feasibility probe (ready for you to run locally).** `scripts/probe_provider.py` sends 5 fixed generic queries — `site:linkedin.com/jobs/view/ <role>` for `software engineer`, `registered nurse`, `accountant`, `data analyst`, `marketing manager` (chosen only to exercise the provider, not your preferences; `--queries-file` accepts other roles) — through one provider restricted to linkedin.com with raw content requested, and saves the raw responses plus a summary in `probe_out/<timestamp>/`. It needs no résumé and no LLM and does no date parsing or job admission. One adapter exists (Tavily; `basic` ≈ $0.04 worst case for 5 queries, `--depth advanced` ≈ $0.08).
   **To run:** copy `.env.example` to `.env` (gitignored) and fill `TAVILY_API_KEY`, `NEMO_PAID_CALLS_ENABLED=true`, `NEMO_MAX_USD_PER_RUN=0.10`; then `.venv/bin/python scripts/probe_provider.py --dry-run` (no call) and `.venv/bin/python scripts/probe_provider.py` (asks for confirmation; `--yes` to skip). The key is read locally, never printed, never written to output. Without a key, a budget, or a confirmation it refuses and makes no call.
   **What we read from it:** (a) share of results that are individual job-view URLs (this also tests whether the provider honours `site:` with a path); (b) whether raw page content is returned and how long it is; (c) whether it contains posting-time text and in which form (structured `datePosted`, absolute date, "N days ago"); (d) whether "Reposted/Updated" or other jobs' times appear; (e) whether the response carries any retrieval/crawl time; (f) login-wall text.
   **Then, before any further build:** we read the actual returned descriptions and date evidence together (the raw JSON, not only the counts), apply the doc 03 §4 gate by hand to a sample, and decide: proceed as designed, change provider/parameters and re-probe, or change the contract (e.g. if only day-granularity evidence comes back, strict 24 h results will be empty by design and we say so rather than loosen the gate).
2. Config (providers, credentials, caps), résumé reader, lookback/window.
3. Résumé profile + query generation (one LLM call, schema-validated).
4. Discovery with bounded retries (each attempt through the guard); URL filter; de-duplication by job id.
5. Posting-time extraction and the corrected window gate (doc 03 §4).
6. Assessment with the five outcomes, content level, limitations and quote grounding (doc 03 §5).
7. SQLite persistence, `SearchResponse` JSON (incl. `unassessed`), Markdown summary, issues and counts.
8. Tests (below) and one real run; report counts honestly, including how many candidates were lost to unknown dates.

Done when acceptance criteria A1–A12 in doc 02 hold.

## M2 — Later, only if wanted
Service wrapper for the larger system (importable package or thin HTTP layer), a second provider adapter, assessment reuse cache, and tuning of caps/prompts from what M1 shows.

## Tests (basic, offline by default)
- Unit: lookback parsing and bounds; window maths; LinkedIn URL accept/reject/canonicalise (done); posting-time parser and widened intervals (absolute, date-only, relative, "Reposted"/"Updated" ignored, other jobs' dates, future dates, missing retrieval time); gate classification incl. boundary; grounding gate and the five assessment outcomes; caps, retries and the monetary guard (done).
- Fixture-based pipeline tests with fake providers: full run to SQLite and response; re-run returns previously seen jobs; provider dates/freshness never admit a job (A5); injected 429/5xx/timeout/malformed-JSON produce issues, not crashes; no request to linkedin.com is ever attempted (A3).
- Live smoke test: opt-in only (env flag), disabled by default, respects the paid-call guard.
- No evaluation harness. After the first real run, you eyeball the results; if you want a stricter check later we can add it.
