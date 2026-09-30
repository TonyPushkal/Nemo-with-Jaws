# Phase 1 — Scope (v3: résumé → LinkedIn postings contract)

Status: DRAFT v3.2. Supersedes v1/v2 (multi-source, search-brief, ATS, verification and closure design). v3.1 corrected assessment outcomes and the monetary guard; v3.2 makes the date gate require the *entire* posting-time interval inside the window and treats failed requests as potentially charged. The guard, a feasibility probe and a Tavily adapter are built; the search service is not.

## The contract

**Inputs**
1. A résumé file (`.pdf`, `.docx`, `.txt`, `.md`).
2. A lookback duration, e.g. `24h`, `7d`.

**Output** — relevant *individual LinkedIn job-posting URLs* posted within the window, each with:
- `title`, `company`, `location` (if available, else `null`)
- posting-time evidence (quote, how it was derived, the time interval it implies)
- a brief explanation of fit, plus the limits of the evidence it was based on

**Promise: best-effort discovery, not exhaustive LinkedIn coverage.** Every response says so and reports what could not be checked.

## Shape
A small, stateful service: one core function, `search_linkedin_jobs(resume_path, lookback) -> SearchResponse` (JSON-serialisable, schema-versioned) so it can later be embedded in a larger system. It keeps state in SQLite (runs and de-duplicated jobs). The first delivery is a script that calls that function; no HTTP layer, scheduler, UI or CLI subcommands.

## Behavioural rules
1. **Everything is inferred from the résumé.** Search terms (titles, related titles, skills, seniority) and relevance criteria come from the résumé via a model. There is no separate search brief, no hard/preferred criteria and no user-supplied preferences. The résumé is read-only background; it is never edited.
2. **LinkedIn only, via a search provider.** Discovery runs through a search provider restricted to `linkedin.com`, and results are kept only if the URL is an individual job posting. We never log in to LinkedIn, and our code never makes an HTTP request to linkedin.com. Content comes only from what the provider returns.
3. **The window is enforced with posting-time evidence.** Only evidence of *when the job was posted* counts (a posting date, or "posted N hours/days ago" with a known retrieval time). Search-result freshness, provider "published/updated" dates, crawl dates and page-update dates are never used to admit a job.
   - **Uncertain evidence never admits a job.** A job is admitted only if the *entire* interval in which its posting time could lie, after allowing for every reading we cannot rule out (time zone of a date-only value, rounding of "N days ago", unknown retrieval time, dates that might belong to another job on the page), falls inside the requested window. **Consequence: date-only evidence with an unknown time zone cannot qualify for a 24-hour window** (its interval is 50 hours wide, wider than the window), and day-granularity evidence in general cannot; such jobs are counted as `boundary` or `unknown_date`, not returned.
   - Jobs with unknown or unusable posting time are **excluded from strict results and their count is reported** (`unknown_date`, kept in SQLite with the reason). Jobs whose evidence is too coarse to guarantee the window are excluded and counted separately (`boundary`).
4. **Assessment uses whatever source content exists, and says so.** Each result states the content level it was judged from (`snippet`, `partial_description`, `full_description`) and lists what could not be judged. Fit claims must quote the job content.
   - **Insufficient evidence is not irrelevance.** `not_relevant` requires a quoted mismatch in the job content. When the content is too thin or the claims are not grounded, the outcome is `insufficient_evidence` (or `assessment_failed` / `not_assessed_budget` for technical or budget reasons). These in-window jobs are returned in a separate `unassessed` list with the reason and counted, never silently dropped or labelled irrelevant.
5. **History informs but never hides.** Previously seen jobs appear again if they match the current request. Results carry `previously_seen` and `first_seen_at` as information only. (Stored posting-time evidence may be reused, since a posting date does not change.)
6. **Bounded and honest.** Hard caps on queries, provider calls, model calls and retries; every failed or empty search is reported; a run is `complete`, `partial` or `failed`.
7. **Paid calls stay disabled until you configure a budget.** The guard reserves each call's worst-case cost before the call, enforces a per-run ceiling and a persistent calendar-month cap, and charges failed calls conservatively (details in doc 03 §8). Provider credentials are configuration (environment/config file), never stored in SQLite or logs. Model hosting (hosted or self-hosted) stays undecided.

## Non-goals
- Sources other than LinkedIn (other boards, employer pages, ATS APIs). Employer-page verification. Closed-job detection.
- Search briefs, salary/location/authorization/exclusion rules, preferred-company lists.
- Provider bake-off, feedback commands (`mark`, `show`, `history`), scheduling, UI, notifications, elaborate evaluation harness.
- Any LinkedIn login, direct scraping, browser automation, CAPTCHA or anti-bot workarounds.
- Résumé tailoring, ATS checks, recruiter contact, applications (Phases 2–3).
- Merging reposts of the same role under different LinkedIn ids (de-duplication is by LinkedIn job id).
- Match percentages.

## Flow (script)
`python scripts/run_search.py --resume path/to/resume.pdf --lookback 7d` → prints a Markdown summary and writes the JSON response; exit code 0 for `complete`/`partial`, non-zero for `failed`.

## Acceptance criteria (Phase 1)

| # | Criterion |
|---|---|
| A1 | The function takes only a résumé path and a lookback; there is no brief file and no preference argument. Invalid résumé (unreadable, empty, image-only) or lookback (unparseable, outside configured bounds) fails fast with a clear error and no provider calls. |
| A2 | Every returned result URL is an individual LinkedIn job-posting URL (canonicalised, with a LinkedIn job id). Search/company/collection/post URLs are dropped and counted. |
| A3 | No code path issues an HTTP request to linkedin.com; only provider adapters make outbound calls, and only to their own APIs. Enforced by a test. |
| A4 | Every result carries posting-time evidence (quote from the returned content, basis, retrieval time if relevant, earliest/latest posting instant) and the *entire* widened interval `[earliest, latest]` lies inside `[window_start, window_end]` (doc 03 §4). |
| A5 | Provider `published_date`/freshness filters/page-update dates never admit a job (fixture: recent provider dates, old or absent posting evidence). One test each: a time-zone-unknown date-only value never qualifies for a 24h window; relative text without a provider-supplied retrieval time never qualifies; "Reposted/Updated" text and other jobs' dates never qualify; an interval that merely overlaps the window (`boundary`) is not returned. |
| A6 | Unknown-date and boundary jobs are absent from `results` and present as counts in the response and as rows in SQLite. |
| A7 | Each result has a fit tier (`strong_match`/`possible_match`), a brief explanation with job-content quotes verified as substrings, and a stated content level and limitations. Ungrounded claims are removed. |
| A7b | Thin or ungrounded content yields `insufficient_evidence` (never `not_relevant`); `not_relevant` requires a verified quoted mismatch. Such jobs, plus `assessment_failed` and `not_assessed_budget`, appear in `unassessed` with their reason and in the counts. |
| A8 | Re-running the same request returns previously seen matching jobs (flagged `previously_seen`); SQLite holds one `jobs` row per LinkedIn job id. |
| A9 | Injected failures (provider 429/5xx/timeout, blocked content, malformed model output, empty résumé text) never crash the run; they appear in `issues` with stage and reason. "Zero results" and "search failed" are distinguished. |
| A10 | Call/retry caps are enforced and the run stops cleanly naming the cap. Monetary guard (tested offline): paid calls refused unless enabled with a ceiling; unpriced paid providers refused; worst-case cost reserved before every attempt (each retry separately); **failed calls charged at the reserved max unless the provider's documentation says otherwise**; actual cost above the reservation stops the run; per-run and monthly caps enforced, the latter across runs via the ledger. |
| A11 | One real end-to-end run (résumé → LinkedIn results) has been executed with the chosen providers, and the report says honestly how many candidates were lost to unknown dates. |
| A12 | The feasibility probe (doc 04 M1 step 1) has been run and its findings recorded before the service is built. |
