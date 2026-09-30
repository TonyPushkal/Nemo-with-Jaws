# Phase 1 — Decisions, Assumptions, Open Questions (v3)

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
- Inputs = résumé + lookback. Search terms and relevance are inferred from the résumé.
- LinkedIn job URLs only, found through a search provider; no LinkedIn login or direct scraping.
- Window enforced by posting-time evidence, not search freshness or page-update dates; unknown-date listings excluded from strict results and counted.
- Previously seen jobs may reappear; SQLite persistence for runs and de-duplicated jobs.
- Assessment from available source content, with honest labelling of evidence limits.
- Keep basic tests, bounded calls/retries, error reporting, configurable provider credentials. Provider choice open, including a self-hosted LLM.
- No résumé tailoring, ATS checks or applications (Phases 2–3). Reference repos untouched; their personal data unused. Skillsheet ignored.
- Paid external calls stay disabled until you configure a budget (per-run ceiling required; monthly cap optional but recommended).

## Decisions I made (change any)
- Strict gate = the posting is *guaranteed* inside the window on the widened interval; coarser evidence goes to `boundary`, counted and stored, not returned.
- Date-only evidence is widened by ±the extreme UTC offsets; relative dates ("N days ago") count only with a provider-supplied retrieval time. No opt-in to assume one.
- `unassessed` jobs (insufficient evidence, failed, out of budget) are returned in a separate list rather than dropped.
- "Reposted/Updated/Active" text and provider `published_date` are never posting evidence.
- Location is reported, not filtered.
- De-duplication is by LinkedIn job id only; reposts under new ids are not merged.
- `results` holds only `strong_match` and `possible_match`; snippet-only judgements cap at `possible_match`.
- Store only résumé hash + inferred profile, not résumé text.
- No agent framework; fixed pipeline.

## Assumptions (unverified)
- A search provider can return LinkedIn job pages' content, including posting-time text, without us touching LinkedIn. **This is the main feasibility risk;** M1 step 1 tests it before anything else. If it fails, strict results will be empty or tiny and the fix is a different provider or a contract change, not a workaround.
- LinkedIn job URLs look like `/jobs/view/<id>` or `/jobs/view/<slug>-<id>` (rule in 03 §3).
- LinkedIn's wording ("N hours/days ago", "Reposted …") behaves as described in 03 §4.
- Provider-side retrieval of LinkedIn pages is acceptable to you; LinkedIn's terms prohibit scraping and providers may refuse or block it. We add no workaround.
- Default caps in 03 §8 are starting points.
- Tavily's $0.008/credit (highest listed pay-as-you-go rate) is used as the worst-case price; its response reports `usage.credits`, which the guard uses to settle actual cost. Error-code charging (`401/422/429/432/433` = not charged) is an assumption from the docs' status table, not a stated billing rule; the guard errs conservative elsewhere.

## Open questions
1. **Probe provider and budget:** the only adapter written is Tavily (its raw-content option fits the contract). To run the probe I need `TAVILY_API_KEY` plus a small budget you set yourself (about $0.10 covers the default 5 queries at `basic`). Would you rather probe a different provider first? The LLM provider (hosted or self-hosted, and hardware if local) is not needed until after the probe.
1b. **Probe queries:** the 5 fixed queries are generic role families, not your preferences. Keep them, or give me a queries file?
2. **Location:** résumé location as a search hint/filter, or leave unconstrained (proposed) and just report location?
3. **Privacy:** are you comfortable sending résumé text to a hosted LLM, or should M1 require a self-hosted model?
4. **Monthly budget:** still unset, so paid calls stay disabled until you provide one.
5. **Lookback bounds:** proposed min 1h, max 30d; adequate?
6. **Résumé formats:** PDF and DOCX support adds two small libraries; OK, or text/Markdown only at first?
7. **Repo licence** (before making anything public).
