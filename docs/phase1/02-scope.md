# Phase 1 — Scope, Non-goals, User Flow, Acceptance Criteria

Status: DRAFT, awaiting approval. Nothing here has been implemented.

Goal: an on-demand agent that does what ChatGPT's web search does for a job query, but systematically and repeatably: run varied searches from a structured brief, open the promising postings, check them against the employer's own page where possible, judge fit from evidence, and return a shortlist with links and honest "unknown"s. It remembers what it has already shown you.

## In scope (Phase 1, v1 = on-demand)

1. **Search brief** (YAML, one file): target roles and related titles, responsibilities wanted, skills, experience/seniority, locations, remote policy, salary expectation, work authorization, exclusions, preferred companies. Every criterion is tagged **hard** (a violation removes the job) or **preferred** (affects ranking/notes only).
2. **Varied discovery** across public web search results, public job boards, and employer career pages, using related titles and responsibility phrases, not only the exact title.
3. **Reading postings**: fetch the description where legally and technically possible; check the employer's page / ATS board for the same role.
4. **Evidence-based assessment**: reasons it fits, gaps, and explicitly *unknown* details. Each claim points to a quote from the fetched text.
5. **Deduplication** across sources and **persistent history** across runs (new / seen / closed / dismissed).
6. **Result fields**: title, company, location, work mode, source URL, application/employer URL, posting date (or "unknown"), fit explanation, gaps, unknowns, verification status.
7. **Honest run report**: which sources/searches failed or were blocked, which jobs are incomplete, budgets used.
8. **Résumé as optional background** for matching statements only (read-only).

Later Phase 1 milestone (not v1): scheduled runs (see milestones).

## Non-goals (explicit)

- Editing or tailoring résumés, ATS parsing/format checks (Phase 2).
- Contacting recruiters, filling or submitting applications, account creation (Phase 3).
- Logging in to, crawling, or scraping LinkedIn, Glassdoor or any site whose terms forbid it; CAPTCHA/anti-bot bypass; proxy rotation.
- Any percentage "match score".
- Multi-agent orchestration frameworks, agent-to-agent protocols, vector databases.
- Web UI, email/Slack notifications, multi-user support, cloud deployment.
- Salary negotiation, company due-diligence beyond "is this posting real, live and from the employer".
- Skillsheet (ignored per instruction).
- Using any data from the reference repos (their résumé, cached profile, DB, CSVs).

## User flow

1. `nemo init` writes `briefs/brief.yaml` from a commented template (placeholders only, no invented preferences). User edits it.
2. `nemo brief check` validates the brief and prints: hard vs preferred criteria, the queries it *would* run, and worst-case budget/cost. Nothing is searched yet.
3. `nemo search` runs the pipeline within budgets, streaming progress. Outputs under `runs/<timestamp>/`: `shortlist.md`, `results.json`, `run-report.md`.
4. User reads the shortlist. `nemo show <id>` prints the evidence (quotes + source links) for one job. `nemo mark <id> interested|dismissed --reason "..."` records feedback.
5. Next `nemo search` shows what is **new**, what is **still open**, what **closed**, and skips dismissed jobs. Dismissal reasons feed evaluation (see doc 04).
6. `nemo history` lists past runs and their status (complete / partial / failed).

## Acceptance criteria (Phase 1 v1)

Each is testable by a test or a manual check on a real run.

| # | Criterion |
|---|---|
| A1 | A brief with no `hard`/`preferred` distinction fails validation with a clear message; a valid brief produces the same planned queries on every `brief check` (deterministic part). |
| A2 | One `nemo search` produces a shortlist where every job has: title, company, location, ≥1 source URL, posting date or `unknown`, fit reasons, gaps, unknowns, and a verification status. |
| A3 | Every factual claim in a fit explanation carries an evidence quote that is a verbatim substring of the stored fetched text; claims failing this check are dropped and counted. |
| A4 | A job violating a hard criterion never appears in the shortlist; a job with an *unknown* hard criterion appears only flagged "needs check" (configurable to drop). Excluded jobs are still stored with the reason. |
| A5 | The same posting found via two sources appears once with both URLs; a re-run does not re-list it as new. |
| A6 | Verification status is one of: `verified_employer` (found on employer domain/ATS), `verified_live` (page fetched, open, not on employer domain), `unverified` (search snippet only), `closed`, `inaccessible`. Nothing is called verified without a fetch. |
| A7 | Injected failures (provider 429/5xx, timeouts, 403 pages, malformed model JSON) never crash the run; they appear in `run-report.md` with stage, target and reason. "0 results" and "search failed" are reported differently. |
| A8 | Hard caps on queries, provider calls, pages fetched, model calls, and estimated spend are enforced; a run stops cleanly at the cap and says which cap. No unbounded loops. |
| A9 | No résumé is modified; no application/contact action exists in the codebase; LinkedIn is only ever *linked*, never fetched or logged into. |
| A10 | The user's real brief runs end to end at least once, and the user labels the output (relevant / not) so evaluation can start. |
