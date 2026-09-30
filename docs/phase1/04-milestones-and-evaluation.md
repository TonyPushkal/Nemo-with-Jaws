# Phase 1 — Milestones and Evaluation

Status: DRAFT. Each milestone is small, ends in something runnable, and can be stopped after without wasted work.

## Milestones

| # | Milestone | Done when |
|---|---|---|
| **M0** | **Foundations.** Repo scaffold (`pyproject`, tests, `.gitignore` for `briefs/ runs/ data/ .env`), brief schema + validation, `nemo init` / `nemo brief check`, budget object. Also: you write your real brief and list ~15–20 jobs you already consider relevant / not relevant (the seed gold set). | A1, A8 unit-tested; your brief validates. |
| **M0b** | **Provider bake-off** (throwaway script, no pipeline): run 3–5 of your queries through Tavily, OpenAI `web_search`, and ATS adapters for your preferred companies. | A table of recall vs your seed list, live-link rate, cost per useful hit; provider default chosen. |
| **M1** | **One useful end-to-end search** (no database). Deterministic queries from the brief → one provider → fetch top pages → JSON-LD/LLM extraction → hard checks → LLM assessment with quote grounding → `shortlist.md` + `run-report.md`. Budgets, retry policy and issue log included from day one. | A2, A3, A4, A7, A8 on your real brief; you label the output. |
| **M2** | **History and dedup.** SQLite schema, canonical URLs, job identity, `new / still open / closed`, `nemo mark`, `nemo show`, `nemo history`. | A5; second run shows only new jobs as new. |
| **M3** | **Evaluation harness.** Labels stored, metrics command, stage-level failure attribution, saved-page snapshots for assessment regression. | `nemo eval` prints metrics vs your labels. |
| **M4** | **Verification and freshness.** Greenhouse/Lever/Ashby adapters, employer-page matching, closed-job detection, re-verify before reshowing. | A6; dead-link rate measured. |
| **M5** | **Query expansion.** LLM proposes related titles and responsibility phrases from the brief; one bounded adaptive round; per-query yield tracking so weak queries are dropped. | Recall on your labels improves without exceeding budgets. |
| **M6** | **Scheduled runs** (later Phase 1). `launchd`/cron calling `nemo search --brief …`, a "what's new" summary file, optional notification. No always-on daemon. | A scheduled run completes and reports partials. |

## Evaluation

Purpose: compare the shortlist with jobs *you* consider relevant, and know which stage lost the ones we missed.

**Gold set (yours).** (1) Seed: jobs you already consider relevant or not, with a one-line reason each. (2) After each early run, you label the shortlist top-k and a random sample of *excluded* jobs (to catch false negatives from hard filters). (3) Optionally the same brief given to ChatGPT; label the union blind, so the baseline you have been using is measured with the same ruler. Snapshots of the posting text are saved, because live jobs expire.

**Metrics** (all reported with sample sizes; small counts are anecdotes, not statistics):
- **Recall of known-relevant jobs still open**, plus a failure-stage breakdown: *not discovered / fetch failed / extraction failed / wrongly excluded by hard filter / verdict too low*.
- **Precision@k** of `strong_fit` + `possible_fit` (k = 10, 20) using your labels.
- **False-exclusion rate** among sampled excluded jobs.
- **Live-link rate** and **duplicate rate** in the shortlist.
- **Grounding rate**: share of fit claims whose quote verified (should be 100% by construction; monitors the gate).
- **"Unknown honesty"** spot check: sampled unknowns where the posting actually stated the fact.
- **Cost and time** per run and per relevant job found.

**Regression.** Assessment logic (prompts, model, rules) is re-run on saved posting snapshots and compared with your labels whenever it changes; discovery quality is measured only on fresh runs.

**Proposed targets (to be confirmed by you, not requirements):** find ≥70% of your known-relevant, still-open jobs; ≥60% of top-10 judged relevant; 0 ungrounded claims; dead links <10%. Revisit after M3 once real numbers exist.
