# Phase 1 — Architecture (v3)

> **Note (2026-10-02):** partly superseded by [09 Phase 1 architecture](09-phase1-architecture.md): the working approach is SerpApi search with ranked results, and the strict posting-date gate below is not built.

Status: DRAFT v3.2 (entire-interval date gate; failed requests treated as potentially charged; superseded v2 modules removed). Plain Python 3.12, one process, SQLite, a fixed pipeline. No agent framework, no loops beyond bounded retries.

## 1. Data flow

```
 script ──► search_linkedin_jobs(profile_path, lookback)        (core service function)
              │
              ▼
   [1 Validate]     lookback -> window; parse + validate the job profile (no model)
              │
              ▼
   [2 Queries]      one `site:linkedin.com/jobs/view/ <role>` per desired role (deterministic)
              ▼
   [3 Discover]     SearchProvider (restricted to linkedin.com) ──► hits {url, title, snippet, content?}
              │        every hit filtered by URL rule; freshness/date hints from the provider are ignored
              ▼
   [4 Canonicalise + de-duplicate]   LinkedIn job id is the key; note previously seen
              │
              ▼
   [5 Posting-time evidence]   parse absolute date / relative "N hours|days ago" from provider content;
              │                 compute [earliest, latest] instant; reuse stored evidence if narrower
              ▼
   [6 Window gate]   in_window | outside | boundary | unknown_date      (only in_window continues)
              │
              ▼
   [7 Assess]   LLM per job vs job profile; each criterion yes/no/unknown; content level + limitations; quote-checked;
              │        outcomes: match | not_relevant | insufficient_evidence (never conflated)
              │
              ▼
   [8 Persist + respond]   SQLite (runs, run_queries, jobs, run_jobs, run_issues) → SearchResponse JSON
   every stage ── Budget/caps + retry policy + issue log ──► response.issues
```

Modules (`src/nemo/`): `service.py` (core function, orchestration), `profile.py` (job profile; built), `tasks.py` (matcher; built), `window.py` (lookback parsing, interval maths), `linkedin.py` (URL rules), `posting_time.py` (evidence extraction), `assess.py`, `store.py` (SQLite), `config.py` (providers, credentials, caps), `budget.py`, `providers/{base,guard,fake,…}.py`, `schema.py` (request/response models). `scripts/run_search.py` is a thin caller.

## 2. Core-function boundary

```python
search_linkedin_jobs(profile_path: Path, lookback: timedelta, *, config: Config | None = None) -> SearchResponse
```
Response (pydantic, `schema_version` field, JSON-serialisable):

```json
{
  "schema_version": 1, "run_id": "…", "status": "complete|partial|failed",
  "window": {"requested": "7d", "start": "…Z", "end": "…Z"},
  "coverage_note": "Best-effort discovery through a search provider; not exhaustive LinkedIn coverage.",
  "results": [{
    "url": "https://www.linkedin.com/jobs/view/<id>", "linkedin_job_id": "<id>",
    "title": "…", "company": "…", "location": "… | null",
    "posting_time": {"earliest": "…Z", "latest": "…Z", "basis": "structured_date|text_date|relative_text",
                     "quote": "…", "retrieved_at": "…Z | null", "notes": ["date-only, time zone unknown, interval 50h wide"]},
    "fit": {"tier": "strong_match|possible_match", "explanation": "…", "quotes": ["…"],
            "content_level": "snippet|partial_description|full_description", "limitations": ["…"]},
    "previously_seen": true, "first_seen_at": "…Z"
  }],
  "unassessed": [{"url": "…", "linkedin_job_id": "…", "title": "… | null", "company": "… | null",
                  "posting_time": {"…": "as above"},
                  "reason": "insufficient_evidence|assessment_failed|not_assessed_budget",
                  "content_level": "snippet|partial_description|full_description", "detail": "…"}],
  "counts": {"queries": 0, "candidates": 0, "non_job_urls_dropped": 0, "duplicates": 0,
             "in_window": 0, "outside_window": 0, "boundary": 0, "unknown_date": 0,
             "strong_match": 0, "possible_match": 0, "not_relevant": 0,
             "insufficient_evidence": 0, "assessment_failed": 0, "not_assessed_budget": 0},
  "issues": [{"stage": "discover", "target": "query 3", "kind": "rate_limited", "message": "…"}],
  "usage": {"queries": 0, "llm_calls": 0, "usd_spent": 0.0}
}
```
`results` holds only `strong_match`/`possible_match`, ordered `strong_match` first then posting time (newest first). `unassessed` holds in-window jobs we could not judge, with the reason. All timestamps UTC.

## 3. LinkedIn URL rule (assumption — verify against real provider output)
Accept hosts `linkedin.com` / `*.linkedin.com` with path `/jobs/view/<id>` or `/jobs/view/<slug>-<id>` where `<id>` is the trailing run of digits. Canonical form `https://www.linkedin.com/jobs/view/<id>`. Drop and count everything else (`/jobs/search`, `/jobs/collections`, `/company/…`, `/posts/…`, `/in/…`). The job id is the de-duplication key.

## 4. Posting-time evidence and the window gate (v3.2: the entire interval must be inside the window)
**Window.** `window_end` = `T_gate`, one UTC instant taken after all discovery/retrieval for the run has finished (so every retrieval time `T_ret ≤ T_gate`); `window_start = T_gate − lookback`. Both are reported in the response; the run start is recorded separately.

**Evidence.** It must come from **content the provider returned for that URL** (the quote must be a substring of it). From it we derive the interval `[earliest, latest]` that the posting instant must lie in, **widened to cover every reading we cannot rule out**:

| Evidence | Interval `[earliest, latest]` | Width | Notes |
|---|---|---|---|
| Absolute date-time with an explicit UTC offset | that instant | ~0 | best case |
| Date only (e.g. `2026-09-29`), time zone unknown | `[D 00:00Z − 14h, D+1 00:00Z + 12h]` | **50 h** | covers local calendar day D in any UTC offset (−12…+14) |
| Date only with a time zone stated in the content | `[D 00:00, D+1 00:00]` in that zone | 24 h | fits a 24h window only if perfectly aligned, i.e. in practice never |
| Relative text "N unit ago" with a provider-supplied `T_ret` | `[T_ret − (N+1)·u, T_ret − max(N−1,0)·u]` | ≈ 2 units | widened to cover floor or round-to-nearest; "month" uses 31d for the lower and 28d for the upper bound; narrow only after the probe shows actual behaviour |
| Relative text and **no provider-supplied `T_ret`** | none → `unknown_date` | — | there is **no** "assume the fetch was live" option |
| Association with the target posting not established | none → `unknown_date` | — | e.g. the content also lists other jobs ("similar jobs") with their own times, or holds several different time phrases and no structured posting data tied to this job id |
| "Reposted …", "Updated …", "Active …", "Renewed …", "Be an early applicant" | **not posting evidence** | — | ignored |
| Provider `published_date`, freshness filters, crawl/index dates, HTTP `Last-Modified` | **never used** | — | may narrow a search, never admit a job |

**Gate (on the widened interval, closed window `[window_start, window_end]`):**
- `in_window` iff `window_start ≤ earliest` **and** `latest ≤ window_end` — the entire interval is inside the window.
- `outside` iff `latest < window_start`.
- `boundary` iff the interval overlaps the window without being contained in it.
- `unknown_date` iff there is no usable, associated evidence; the evidence is contradictory; or `earliest > window_end` (future-dated).

Only `in_window` reaches assessment/results; the other three are counted and stored.

**Consequence: a job can qualify only if the interval width is ≤ the lookback.** Hence time-zone-unknown date-only evidence (50 h) can never qualify for a 24-hour window, and can qualify for longer windows only when the date is far enough back that the whole 50 h interval sits inside. Day-granularity relative text ("1 day ago" ≈ 2 days wide) cannot qualify for 24 h either. For a 24 h lookback, essentially only an explicit time with offset, or "N minutes/hours ago" with a provider-supplied `T_ret` close to `T_gate`, can qualify. If the probe shows the provider returns only day-granularity evidence, strict 24 h results will be empty and the response will say why.

Stored evidence: `jobs` keeps the narrowest *consistent* interval seen across runs (intersection of independent evidence; a posting instant does not change). An empty intersection ⇒ `unknown_date` plus an issue.

*Assumptions the probe must settle before this is implemented:* whether provider content for LinkedIn job pages contains posting-time text at all; whether it is structured or relative; whether the provider reports retrieval time (Tavily's documented response fields do not); how "N days ago" is rounded; whether other jobs' times appear in the same content.

## 5. Relevance assessment
1. **Job profile (input; `profile.py`, no model).** UTF-8 Markdown/text (BOM and CRLF accepted), optional `# Title`, then exactly these `##` sections, each present once (aliases accepted, e.g. *Requirements*, *Deal-breakers*): *Experience* (free text or bullets), *Desired roles*, *Must-haves*, *Nice-to-haves*, *Exclusions* (one item per bullet; indented lines continue a bullet; `<!-- -->` comments ignored). Validation: Experience and Desired roles non-empty; ≤8 roles, ≤6 items per criteria list, ≤160 chars per item, Experience ≤1,200 chars, whole profile ≤2,500 chars (so job text still fits a 4,096-token context); duplicates removed; an item may not be in two lists; unknown headings or stray text are errors with line numbers. It normalises into a frozen `JobProfile` whose criteria get stable ids `M1…` (must), `N1…` (nice), `X1…` (exclusion). `nemo.profile.TEMPLATE` is an empty template. Stored: normalised profile JSON and its SHA-256.
2. **Queries:** one `site:linkedin.com/jobs/view/ <role>` per desired role, deterministic, capped by `max_queries`. (Model-generated variations are dropped for now.)
3. **Per-job assessment (one call per in-window job, capped):** input = job profile + the job content the provider returned. The model proposes an outcome, explanation, fit/mismatch quotes and, **for every criterion id, `yes` (the job text shows it is true of the job) / `no` (shows it is false) / `unknown` (text is silent)** with a quote for yes/no. Judged on responsibilities and seniority, not keyword overlap; no percentages.
   Code then resolves criteria: a missing answer, an invalid status, or a yes/no whose quote is not in the job text becomes `unknown`. **A verified `no` on a must-have or a verified `yes` on an exclusion makes the job `not_relevant`** (reason names the ids). Unknowns never block; an unknown must-have caps the job at `possible_match`. Nice-to-haves affect only the explanation.
4. **Content level** (`snippet` / `partial_description` / `full_description`) is assigned by code from the length/structure of the returned content, not by the model, and is shown to the user.
5. **Grounding gate:** every quote must be a verbatim substring of the job content; claims that fail are removed.
6. **Outcomes are decided by code from what survives the gate — insufficient evidence is never turned into irrelevance:**

| Outcome | Condition | Where it goes |
|---|---|---|
| `strong_match` / `possible_match` | no blocking criterion; ≥1 grounded fit quote (or verified must-have `yes`); `strong_match` needs `partial_description` or better and no unknown must-have, else capped at `possible_match` | `results` |
| `not_relevant` | a verified must-have `no` or exclusion `yes`, or ≥1 grounded quote showing clearly different work or seniority | stored, counted, not returned |
| `insufficient_evidence` | content too thin to judge (typically snippet-only with no clear mismatch), or the model's claims were all removed by the gate | `unassessed` (+ count) |
| `assessment_failed` | provider error or malformed output after one repair attempt | `unassessed` + issue |
| `not_assessed_budget` | LLM cap reached before this job | `unassessed` + `budget` issue |

7. **Location:** returned when present in the content; it is not a filter (unless the profile states a location must-have, which is then judged like any other criterion). *Open question 2.*
8. Optional cost saver: reuse a stored assessment when (job content hash, profile hash, prompt version, model) match. It never suppresses a job from results.

## 6. Providers and configuration
Interfaces (already scaffolded in M0): `SearchProvider` (`search(query, max_results) -> hits with url/title/snippet/content/retrieved_at`) and `LLMProvider` (`complete_json`). Both declare `is_paid`. Configuration comes from environment variables or a gitignored config file: provider names, base URLs, API keys, model names, caps. Secrets never enter SQLite, responses or logs.

Requirements on the search provider: restrict results to `linkedin.com`; return page content or enough of it to hold posting-time text; report failures explicitly. Discovery queries take the form `site:linkedin.com/jobs/view/ <role or skill terms>` in addition to the domain restriction; whether a provider honours a path-level `site:` is unverified and measured by the probe (share of results that are job-view URLs). Options (docs checked 2026-09-30; none chosen):

| Option | Relevant capabilities | Concerns for this contract |
|---|---|---|
| Tavily Search (+Extract) | `include_domains` (≤300), `include_raw_content`, `max_results` ≤20, per-result content; search 1–2 credits, free 1,000 credits/month; Extract ≤20 URLs/call with `failed_results`. Its `published_date` is a "publication/update date estimate" → **not used**. | Returns no retrieval time in the documented fields (relative dates would be `unknown` unless `assume_live_fetch…` is enabled). Whether it returns useful content for LinkedIn job pages is untested. |
| OpenAI Responses `web_search` | `filters.allowed_domains` (≤100), sources listing, citations; $10/1k calls + tokens. | The model reads pages and we see its output, not raw page text, so posting-time quotes cannot be verified deterministically — weaker for this contract unless the tool exposes page content. |
| Other search APIs (e.g. a SERP-style API) | Domain-restricted queries; snippets. | Not checked. Snippets rarely carry posting time. |
| LLM: hosted vs self-hosted | Self-hosted = OpenAI-compatible endpoint (assumption to verify per server). | Keeps the profile on your machine; schema-following and quality vary, so the schema check and quote gate matter more. |

The profile is sent to whichever LLM provider is configured; with the chosen local Ollama model it stays on this machine. *Open question 3.*

## 7. SQLite (6 tables, `data/nemo.sqlite`, gitignored)

| Table | Columns |
|---|---|
| `runs` | id, schema_version, started_at, finished_at, status, profile_sha256, lookback_seconds, window_start, window_end, profile_json, providers_json, counts_json, usage_json |
| `run_queries` | id, run_id, text, status (`ok`/`empty`/`failed`), result_count, error |
| `jobs` | linkedin_job_id (PK), url, title, company, location, first_seen_run, first_seen_at, last_seen_run, last_seen_at, posted_earliest, posted_latest, posting_basis, posting_quote, posting_retrieved_at |
| `run_jobs` | run_id, linkedin_job_id (PK together), window_status (`in_window`/`outside`/`boundary`/`unknown_date`), content_level, content_hash, content_excerpt, assessment_json, outcome (`strong_match`/`possible_match`/`not_relevant`/`insufficient_evidence`/`assessment_failed`/`not_assessed_budget`), returned (bool), exclusion_reason |
| `run_issues` | id, run_id, stage, target, kind, message |
| `spend_ledger` | id, run_id, provider, month (`YYYY-MM`), reserved_micro, settled_micro (null until settled), created_at — integer micro-USD; implemented |

WAL mode, one writer at a time (a second concurrent run waits or fails fast with a clear error).

## 8. Bounds, retries, errors
- **Caps per run (config; proposed defaults, not derived from data):** queries ~15, provider results per query ~20, LLM calls ~40 (1 profile + assessments), wall clock ~10 min.
- **Monetary guard (implemented in `budget.py`/`ledger.py`, tested offline; fixes the v3 draft):**
  1. Paid calls are refused unless `NEMO_PAID_CALLS_ENABLED=true` **and** `NEMO_MAX_USD_PER_RUN` is set; optional `NEMO_MONTHLY_USD_CAP` is a calendar-month (UTC) cap shared across runs through the SQLite ledger.
  2. A paid provider must declare a positive worst-case cost per call (`max_cost_usd`; for LLMs an upper bound from input plus max output tokens). An unpriced paid provider is refused, so an estimate of 0 cannot bypass the ceiling.
  3. The worst case is **reserved before every attempt** and checked against the run ceiling and monthly cap atomically (`BEGIN IMMEDIATE`); a retry is a new attempt with its own reservation.
  4. After the call the reservation is settled to the provider-reported actual cost (e.g. Tavily `usage.credits`). If none is reported, the reserved max is charged.
  5. **A failed request is treated as potentially charged**: the reserved max is charged. The only exception is a failure the provider's own documentation says is not billed (`charged=False`, with the doc page cited in the adapter). Tavily's docs list status codes but no billing rule for failed searches, so every Tavily failure is charged; the Extract-only note "failed extractions incur no charges" is not applied to Search. Unsettled reservations (crash) keep counting at their reserved max.
  6. An actual cost above the reservation is recorded and stops the run (`cost_overrun`).
  7. Money is integer micro-USD, rounded up. The USD-per-credit rate for Tavily is the highest listed pay-as-you-go rate ($0.008), so even free-tier calls are treated as paid until you say otherwise.
- **Retries:** only transient failures (429/5xx/timeouts/connection): ≤2 retries, exponential backoff with jitter, honour `Retry-After`, each retry counts toward the caps. No retry on 401/403/404. Per-provider circuit breaker after 3 consecutive failures. One repair attempt for malformed model JSON, then `assessment_failed` (the job is counted, not silently dropped).
- **Errors:** every stage returns typed results; nothing is swallowed. Status is `failed` only if no usable candidates could be produced (e.g. every search failed; an invalid profile fails before the run starts), `partial` if any stage hit a cap or failure, else `complete`.
- **Privacy:** the profile file is never modified; its hash and normalised JSON are stored.

## 9. Status of the scaffold
Built: `budget.py` + `ledger.py` (monetary guard), `providers/{base,guard,fake,tavily}.py`, `linkedin.py` (URL rule), `probe.py` + `scripts/probe_provider.py`, `envfile.py`, and an offline test suite that blocks all network access.
Removed in v3.2 (recoverable from local commit `40f58d7`, tag `checkpoint-before-prune`): `brief.py`, `plan.py`, `identity.py`, `lifecycle.py`, `urlnorm.py`, `models.py` (closure/verification vocabulary), the brief CLI (`cli.py`, `__main__.py`) and their tests. `PyYAML` and the `nemo` console script went with them.
Left in place, to revisit when the service is built: the `PageFetcher`/`FetchResult`/`GuardedFetcher`/`max_pages` surface, which v3 does not need (content comes from the search provider only, and nothing may fetch linkedin.com).
Not started, by design, until the probe has been run and its output reviewed: `service.py`, `window.py`, `posting_time.py`, `assess.py`, `store.py`, `config.py`, `schema.py`.
