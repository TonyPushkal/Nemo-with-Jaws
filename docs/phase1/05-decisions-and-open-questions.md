# Phase 1 — Decisions, Assumptions, Open Questions (v3)

## What changed in v3.3 (job profile input)
- Primary input is now a UTF-8 Markdown/text job profile (Experience, Desired roles, Must-haves, Nice-to-haves, Exclusions) parsed deterministically into a validated `JobProfile`; no configuration framework. Résumé input is deferred (a later converter can produce the same profile); the résumé-extraction code and fixture were removed.
- Matching answers every criterion yes/no/unknown; missing job information is unknown; only verified contradictions/exclusions block; unknown must-haves cap at `possible_match`.
- Queries are deterministic, one per desired role; model-generated query variations are dropped for now.
- Unchanged: lookback filtering, LinkedIn-only output, the outcome set, SQLite history (with `profile_sha256` in place of `resume_sha256`).

## Model hosting decided (after v3.2)
Local Ollama, `qwen3.5:4b`, 4,096-token context, one request at a time, on this 8 GB M1. Built: `providers/ollama.py` (adapter, `think` off, context preflight, sequential), `tasks.py` (job → match; résumé extraction since removed in v3.3 with code-side quote grounding and outcome rules), `scripts/local_model_check.py`. Measurements and caveats are in `06-local-model-findings.md`; the main ones are memory pressure on 8 GB, ~10–20 s per call, and that only synthetic content has been tested.

## What changed in v3.2 (your latest corrections)
1. **Date gate:** the *entire* posting-time interval must lie inside the requested window (doc 03 §4). Documented consequence: time-zone-unknown date-only evidence (a 50-hour interval) cannot qualify for a 24-hour window, nor can day-granularity evidence in general.
2. **Failed requests are potentially charged** unless the provider documents otherwise. My v3.1 adapter assumed 400/401/422/429/432/433 were free from the status-code table; Tavily documents no such rule, so that assumption is removed and every Tavily failure is charged at the reserved maximum.
3. **Probe queries** are now `site:linkedin.com/jobs/view/ <role>` for the same five generic roles, to target individual postings.
4. **Superseded modules removed** after a local checkpoint (commit `40f58d7`, tag `checkpoint-before-prune`): brief, query planner, merge/closure rules, URL normaliser, job vocabulary, brief CLI and their tests. I also removed `models.py` (closure/verification vocabulary), which you had not listed by name but which only served those modules; it is in the checkpoint if you want it back.
5. **Probe prepared for local use:** `.env`-based key, confirmation prompt, refusal without key/budget. After you run it we review the real returned content and dates before implementing the rest of the service. Model hosting remains undecided.

## What changed in v3.1 (your corrections)
1. **Uncertain-date admission fixed.** The draft admitted date-only values as UTC and allowed an "assume live fetch" flag for relative dates; both could admit a job whose real posting time was outside the window. Now the interval is widened for time zone and rounding, relative text without a provider-supplied retrieval time is `unknown_date`, the assume-live option is gone, and dates whose association with the target job is unclear never admit (doc 03 §4).
2. **Insufficient evidence ≠ irrelevance.** Five outcomes; `not_relevant` needs a quoted mismatch; thin or ungrounded cases go to a separate `unassessed` list with a reason (doc 03 §5).
3. **Monetary guard fixed before any paid call** (doc 03 §8): the draft's guard checked an *estimate* after the fact, skipped a failed call's cost, allowed an unpriced provider (estimate 0) to bypass the ceiling, had no monthly cap across runs, and did not account for retries. All fixed, with offline tests.
4. Model hosting remains undecided. Nothing in the probe or guard needs an LLM.
5. Next step is the feasibility probe only; the service is not expanded until its findings are in.

## What changed in v3 (your simplification)
- The contract is now: **résumé file + lookback → relevant individual LinkedIn job-posting URLs** with title, company, location if available, posting-time evidence and a brief fit explanation.
- Removed: search brief, hard/preferred criteria, non-LinkedIn sources, preferred-company/ATS adapters, employer verification and closure logic, mandatory provider bake-off, feedback commands, scheduling, UI, elaborate evaluation.
- Added: stateful core service function with SQLite runs + de-duplicated jobs; strict posting-time-evidence window; unknown-date count; history never suppresses results; best-effort promise.

## Confirmed by you
- Python; script first, calling the core service function; later joins a larger system.
- Inputs = job profile + lookback (v3.3; originally résumé + lookback).
- LinkedIn job URLs only, found through a search provider; no LinkedIn login or direct scraping.
- Window enforced by posting-time evidence, not search freshness or page-update dates; unknown-date listings excluded from strict results and counted.
- Previously seen jobs may reappear; SQLite persistence for runs and de-duplicated jobs.
- Assessment from available source content, with honest labelling of evidence limits.
- Keep basic tests, bounded calls/retries, error reporting, configurable provider credentials. Provider choice open, including a self-hosted LLM.
- No résumé tailoring, ATS checks or applications (Phases 2–3). Reference repos untouched; their personal data unused. Skillsheet ignored.
- Paid external calls stay disabled until you configure a budget (per-run ceiling required; monthly cap optional but recommended).

## Decisions I made (change any)
- Strict gate = the posting is *guaranteed* inside the window on the widened interval; coarser evidence goes to `boundary`, counted and stored, not returned.
- Date-only evidence with unknown time zone is a 50-hour interval and so cannot qualify for windows shorter than that; relative dates ("N days ago") count only with a provider-supplied retrieval time. No opt-in to assume one.
- `unassessed` jobs (insufficient evidence, failed, out of budget) are returned in a separate list rather than dropped.
- "Reposted/Updated/Active" text and provider `published_date` are never posting evidence.
- Location is reported, not filtered.
- De-duplication is by LinkedIn job id only; reposts under new ids are not merged.
- `results` holds only `strong_match` and `possible_match`; snippet-only judgements cap at `possible_match`.
- Store the normalised profile JSON and its hash.
- No agent framework; fixed pipeline.

## Assumptions (unverified)
- A search provider can return LinkedIn job pages' content, including posting-time text, without us touching LinkedIn. **This is the main feasibility risk;** M1 step 1 tests it before anything else. If it fails, strict results will be empty or tiny and the fix is a different provider or a contract change, not a workaround.
- LinkedIn job URLs look like `/jobs/view/<id>` or `/jobs/view/<slug>-<id>` (rule in 03 §3).
- LinkedIn's wording ("N hours/days ago", "Reposted …") behaves as described in 03 §4.
- Provider-side retrieval of LinkedIn pages is acceptable to you; LinkedIn's terms prohibit scraping and providers may refuse or block it. We add no workaround.
- Default caps in 03 §8 are starting points.
- Tavily's $0.008/credit (highest listed pay-as-you-go rate) is used as the worst-case price; its response reports `usage.credits`, which the guard uses to settle actual cost. Failed requests are charged at the reserved maximum (no Tavily billing rule for failures is documented).

## Open questions
1. **Probe run (yours):** supply `TAVILY_API_KEY` and a small budget locally (about $0.10 covers the default 5 queries at `basic`), run the probe, and tell me the output folder name; I will read the raw returned descriptions and dates with you. Would you rather probe a different provider first? The LLM provider is now local Ollama `qwen3.5:4b` (see above).
2. **Location:** location is reported, and filtered only if you write it as a must-have or exclusion. Should it also narrow the search queries?
3. **Privacy:** resolved by the local model: the profile stays on this machine.
4. **Monthly budget:** still unset, so paid calls stay disabled until you provide one.
5. **Lookback bounds:** proposed min 1h, max 30d; adequate?
6. **Résumé → profile converter:** deferred; when wanted, which formats (PDF/DOCX add two small libraries)?
7. **Repo licence** (before making anything public).
