# Phase 1 — Architecture

Status: DRAFT. Plain Python 3.12 package with a CLI, SQLite, and a fixed pipeline. No agent framework: the "agent" behaviour is one bounded, optional extra search round (see §6). The pipeline is deterministic code that calls models only where judgement is needed.

## 1. Data flow

```
 brief.yaml ──► [1 Load/validate] ──► [2 Plan queries]
 (+ optional résumé, read-only)            │  templates now; LLM expansion (M5)
                                            ▼
                                  [3 Discover]  ◄── SearchProvider (Tavily | OpenAI web_search | …)
                                   │   └─ ATS adapters for preferred companies (Greenhouse/Lever/Ashby public APIs)
                                   ▼  candidate URLs + snippets
                                  [4 Triage]  canonicalise URL, drop non-job pages,
                                   │          skip URLs already assessed & unchanged (history)
                                   ▼
                                  [5 Fetch & extract]  page/ATS JSON → schema.org JobPosting JSON-LD
                                   │                    → LLM extraction fallback; store text + evidence quotes
                                   ▼
                                  [6 Verify & freshness]  employer/ATS match, open/closed, posted date
                                   ▼
                                  [7 Merge/dedup]  one job, many sources
                                   ▼
                                  [8 Assess]  hard checks (code) + fit judgement (LLM, schema-bound, quote-checked)
                                   ▼
                                  [9 Report + persist]  shortlist.md, results.json, run-report.md, SQLite
                                   ▲
      every stage ── Budget (caps) + Retry policy + Issue log ──► run-report "Source health / failures"
```

Modules (proposed `src/nemo/`): `brief.py`, `plan.py`, `providers/{base,tavily,openai_search}.py`, `ats/{greenhouse,lever,ashby}.py`, `fetch.py`, `extract.py`, `verify.py`, `dedup.py`, `assess.py`, `budget.py`, `store.py`, `report.py`, `cli.py`. Private data (`briefs/`, `résumé`, `data/`, `runs/`) is gitignored; only `*.example` files are committed.

Two narrow interfaces keep providers swappable, because the provider is not decided:

- `SearchProvider.search(query, filters) -> list[Hit]` (url, title, snippet, published date if given, raw cost)
- `PageFetcher.fetch(url) -> FetchResult` (status, text, final_url, error kind). Default: polite HTTP client honouring robots.txt and per-domain rate limits; Tavily Extract is an alternative implementation.

## 2. Search-provider options (docs checked 2026-09-30)

| Option | What it gives | Costs / limits (from docs) | Fit for this project |
|---|---|---|---|
| **OpenAI Responses API `web_search` tool** | Model searches, opens pages, cites (`url_citation`). Supports `allowed_domains`/`blocked_domains` (≤100 each), `user_location`, `search_context_size`, `include: web_search_call.action.sources`. Closest to "what ChatGPT does". | Pricing page: $10 / 1k calls + search-content tokens at model rates. Uses the model's rate limits. "Search context is limited to 128k." Docs list search-capable models; **model names in the fetched text must be re-confirmed at build time.** | Best recall per line of code and the behaviour you already like. Weaknesses: opaque (you don't fully control queries/pages opened), harder to cap spend precisely, less reproducible, sources listing must be requested. Good as a discovery provider, less good as the only one. |
| **Tavily Search + Extract** | Search: `include_domains` (≤300), `exclude_domains` (≤150), `time_range`/`start_date`, `max_results` ≤20, `include_raw_content`, optional `published_date`, `search_depth` basic/advanced. Extract: ≤20 URLs/call, returns `failed_results` with errors. | Search 1 credit (basic) / 2 (advanced). Extract 1 credit per 5 URLs. Free 1,000 credits/month; PAYG $0.008/credit. Snippets ≤500 chars/chunk. | Transparent, cheap, budgetable per call, explicit failures. Weakness: a general web index, not job-specific: coverage of postings and date reliability unknown until tested. Needs our own LLM for judgement. |
| **Public ATS APIs** (Greenhouse Job Board, Lever Postings, Ashby posting API) | Unauthenticated GET JSON with description, location, apply URL. Greenhouse has `updated_at`/`first_published`; Ashby `publishedAt`, `isRemote`, optional compensation; Lever gives `workplaceType`, `salaryRange`, `descriptionPlain` (no posting date found in the docs I read). | Free; polite rate limits. Need the company's board token/slug. | Excellent for *preferred companies* and for verifying "is it on the employer's own board". Only covers employers on those three ATSs. |
| **schema.org `JobPosting` JSON-LD** parsed from any fetched page | Deterministic title/date/location/`validThrough` when present. | Free. | Try before spending LLM tokens on extraction. *Assumption: common on career pages; to be measured in M1.* |
| **Anthropic web search/fetch tools** | Possible alternative model+search combo. | **Not verified in this session.** | Only if you choose Anthropic. |
| **JobSpy / board scrapers** | Structured board results. | Library licence not reviewed; LinkedIn/Glassdoor terms prohibit scraping. | **Excluded** for now. |
| Job-board aggregator APIs (Adzuna, Google-Jobs proxies) | Structured feeds. | Not checked. | Candidates for a later spike, not v1. |

**Recommendation.** Build against the interfaces, then run **M0b, a bake-off** on your own briefs: Tavily vs OpenAI `web_search` (and ATS adapters for your preferred companies) scored on (a) how many of the jobs *you* consider relevant they surface, (b) share of results that are live, real postings, (c) cost per useful job. Default until then: **Tavily for discovery+extract, ATS APIs for preferred companies, one LLM for assessment**, because it is the only combination whose cost and failures are fully under our control. Switch or combine if OpenAI search wins the bake-off.

## 3. Minimal data model

### 3.1 Search brief (YAML, validated by pydantic; *schema shown, values are placeholders*)

```yaml
version: 1
about_me:                     # background only; never used as a hard filter
  summary: "<free text>"
  resume_path: null           # optional, local, gitignored
search:
  target_titles: ["<title>"]
  related_titles: ["<title>"]        # allowed to expand
  responsibilities: ["<work I want to do>"]
  companies_preferred: [{name: "<co>", careers_url: null}]
  freshness_max_age_days: <n>
hard:                         # violated => excluded; unknown => "needs check" (unknown_policy)
  locations: []               # each: {place, mode: onsite|hybrid|remote}
  work_authorization: {countries: [], needs_sponsorship: <bool>}
  min_salary: {amount: <n>, currency: "<c>", period: year}   # optional
  exclusions: {companies: [], title_keywords: [], industries: []}
  seniority: {allowed: [], years_experience: <n>}
  must_skills: []
  unknown_policy: flag        # flag | drop
preferred:                    # ranking/notes only
  skills: []
  remote: "<preference>"
  salary_target: null
  company_traits: []
budgets: {max_queries: <n>, max_pages: <n>, max_llm_calls: <n>, max_usd: <n>}
```

Validation: a criterion listed in both `hard` and `preferred` is an error; empty `hard` is allowed but warned.

### 3.2 SQLite (9 small tables)

| Table | Key columns |
|---|---|
| `brief_snapshots` | id, sha256, yaml, created_at |
| `runs` | id, brief_id, started_at, finished_at, status (`complete`/`partial`/`failed`), budgets_json, usage_json |
| `queries` | id, run_id, provider, text, purpose, status (`ok`/`empty`/`failed`), result_count, error, cost |
| `jobs` | id, canonical_key, company, title, location_text, work_mode, employer_url, apply_url, posted_at, posted_at_basis (`structured`/`page_text`/`unknown`), salary_min/max/currency (nullable), status (`open`/`closed`/`unknown`), verification, first_seen_run, last_seen_run, user_state (`new`/`interested`/`dismissed`), user_reason |
| `job_sources` | id, job_id, url, url_canonical, source_type (`employer_ats`/`employer_site`/`job_board`/`linkedin_link`/`snippet_only`), fetch_status, http_status, fetched_at, content_hash, text |
| `evidence` | id, job_id, source_id, field (`responsibility`/`skill`/`location`/`salary`/`seniority`/`posted`/`closing`/`auth`), quote, offset_start, offset_end |
| `assessments` | id, job_id, run_id, brief_sha, model, verdict, hard_results_json, dimensions_json (rating + evidence_ids + note), fit_reasons, gaps, unknowns |
| `job_events` | id, job_id, run_id, event (`first_seen`/`seen_again`/`changed`/`closed`/`reappeared`/`verdict_changed`/`user_marked`), detail, at |
| `run_issues` | id, run_id, stage, target, kind (`rate_limited`/`blocked`/`timeout`/`not_found`/`parse_failed`/`budget_hit`/…), message |


Assessments are keyed to `brief_sha`, so editing the brief triggers re-assessment of stored text rather than a new search.

## 4. Deduplication and identity

1. Canonical URL: strip tracking params, lowercase host, resolve redirects; ATS job id when known (e.g. Greenhouse board+job id).
2. Otherwise `canonical_key` = normalised (company, title, location, work mode); fuzzy title + description-hash similarity to merge near-duplicates.
3. Merge = one `jobs` row, many `job_sources`; prefer employer/ATS source for `apply_url`; keep a `job_events` entry.
4. Different location or requisition id ⇒ different job even with same title.
5. A job not seen for K runs (default 3) is marked `unknown`; a direct re-fetch returning 404/410/"no longer accepting" marks it `closed`.

## 5. Relevance assessment (not keyword overlap, no percentages)

Inputs: brief, extracted posting text (full, not a 500-char snippet), evidence store, optional résumé summary.

1. **Hard checks (code first, LLM only for fuzzy ones).** Location/work mode, work authorization, exclusions, salary floor, seniority band, must-skills → each `met | violated | unknown`. Explicit text ("must be located in…", "no sponsorship") is quoted. Any `violated` ⇒ excluded (kept in DB with reason). `unknown` follows `unknown_policy`.
2. **Dimension ratings (LLM, structured output, one call per surviving job):** `responsibilities` (does the day-to-day work match, judged from duties rather than title), `seniority` (scope, ownership, years, management vs IC), `required_skills` (must vs nice, per skill), `location/work-mode`, `compensation`, `company/preferences`, `freshness/status`. Each is `strong | partial | weak | unknown` with evidence ids.
3. **Grounding gate.** Every quote must be a substring of the stored text; ungrounded claims are removed. A dimension left with no support becomes `unknown`.
4. **Verdict by rule, not by the model's mood.** LLM proposes; code caps it: `strong_fit` requires responsibilities=strong, seniority ≥ partial, no hard `unknown`; any hard `unknown` caps at `possible_fit (needs check)`. Tiers: `strong_fit / possible_fit / weak_fit`; `not_a_fit` is excluded.
5. **Ranking** = verdict tier, then number of preferred criteria met, then freshness. The report shows reasons, gaps, and unknowns; no score.
6. Résumé, if supplied, may only support statements like "posting asks for X; your résumé mentions X" and is labelled as such; it never overrides the brief.
7. Cost control: cheap deterministic pre-filters (exclusions, obvious non-job pages, dead links) run before any model call; assessment prompts are cached by (text hash, brief sha, prompt version).

## 6. Budgets, retries, freshness, errors

**Budgets** (per run, from the brief; proposed defaults to tune after M0b, not confirmed): max queries (~30), provider calls, pages fetched (~60), LLM assessments (~40), wall-clock (~15 min), estimated spend cap. The `Budget` object is checked before every external call; on exhaustion the stage stops, the run status becomes `partial`, and `run_issues` names the cap.

**Adaptive round.** After the first pass, if fewer than N assessed candidates reach `possible_fit`, run **one** extra round of *new* queries (never repeating query text from `queries` history) — and only while budget remains. That is the entire "agent loop"; the round count is a loop variable checked in code and unit-tested (avoiding the never-incremented-counter defect in the reference).

**Retries.** Only for transient errors: 429/5xx/timeouts/connection errors; max 2 retries, exponential backoff with jitter, honour `Retry-After`; each retry counts against the budget. No retry for 401/403/404/410 (classified `blocked`/`not_found`/`closed`). Per-domain circuit breaker after 3 consecutive failures ⇒ skip that domain for the rest of the run and report it. Malformed model JSON: use schema-constrained output; one repair attempt; otherwise mark `assessment_failed` and keep the job in the report as unassessed rather than dropping it.

**Freshness.** Prefer structured dates (ATS `updated_at`/`publishedAt`, JSON-LD `datePosted`/`validThrough`). Page text dates ("Posted 3 days ago") are labelled `page_text`. No date ⇒ `unknown`, never guessed. Jobs older than `freshness_max_age_days` are excluded only when the date is known. Shortlisted jobs from earlier runs are re-fetched before being shown again.

**Politeness/legal.** robots.txt honoured, identifying User-Agent, per-domain rate limit, no login, no CAPTCHA bypass, LinkedIn URLs never fetched.

**Error reporting.** Every external call returns a typed result; stages never swallow exceptions silently. `run-report.md` has a "Source health" table (attempted / ok / failed / blocked by domain and provider), "Incomplete jobs" and "Budgets used". Exit code non-zero only when the run is `failed` (nothing usable), not `partial`.
