# Local model findings — Ollama `qwen3.5:4b`, 4,096-token context, one request at a time

> **Update (v3.3, job-profile matcher, same machine and settings):** 8/8 requests ok against `tests/fixtures/synthetic_profile.md`; all 7 synthetic jobs landed in their expected outcome sets; repeat run identical. Answering every criterion roughly doubled output (200–300 generated tokens, within `num_predict=600`), so assessment now takes **14–29 s, median ≈ 18 s** (was ≈ 11 s). Memory unchanged (10–11 % free while loaded). Observed: the model answered an exclusion (`X1` people-management) `yes` with an invented quote on a job that clearly was not a management role; the quote gate turned it into `unknown`, so the job was not wrongly blocked. The profile-driven blocks worked (principal-engineer job blocked by `M1` Python and `X1`), and a job not stating its work mode left `M2` unknown and capped at `possible_match`. The résumé-extraction section below is historical; that code has been removed.

Measured 2026-09-30 on this machine: Apple M1, **8 GB RAM**, Ollama 0.35.0 (`OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=4096`), model `qwen3.5:4b` (4.7B parameters, Q4_K_M, 3.4 GB download), `num_predict=600`, temperature 0, seed 0. Reproduce with `scripts/local_model_check.py`; raw report in `probe_out/local_model/<timestamp>/report.json` (gitignored).

**Inputs were synthetic** (an invented résumé and 7 invented job texts written by me, `tests/fixtures/`). The Tavily probe has not been run, so **no real LinkedIn content has been assessed yet.** Seven jobs is a smoke test, not an accuracy measurement.

## Setup finding: `think` must be off
With `think` unset, `qwen3.5:4b` spent the whole 600-token budget on hidden reasoning and returned no JSON (54 s). The library page said it was not a thinking model; that was wrong for this build. The adapter now sends `think: false` (3 s for the same request). Any other Ollama model needs re-checking.

## Latency (warm unless stated; wall clock per request)
| Task | Wall | Prompt tokens | Generated | Generation speed |
|---|---|---|---|---|
| Cold load (model unloaded first) | +7.5 s load | | | |
| Résumé → profile (first call, includes load) | 30.6 s | 630 | 267 | 15.0 tok/s |
| Résumé → profile (warm) | 17.2 s | 630 | 267 | 16.3 tok/s |
| Job assessment, 7 jobs | 8.2 – 20.6 s (median ≈ 11 s) | 469 – 1,148 | 81 – 147 | 8 – 16 tok/s |
| Same job repeated | 12.1 s then 20.5 s | 677 | 114 | 16.2 then 9.3 tok/s |
| 9.4k-char résumé (truncated to fit) | 43.6 s | 2,592 | — | — |

Generation (~16 tok/s) and prompt processing (~150 tok/s) dominate; the first request after a prompt change pays for prompt evaluation (an identical repeated prompt was cached: 0.5 s). Rule of thumb: **≈10–20 s per job**, so 40 assessments ≈ 7–14 min, above the proposed 10-minute wall cap; the caps need tuning for this model, or fewer jobs assessed per run.

## Memory pressure (this is the main constraint)
- Loaded model: 3.54 GB, of which 2.89 GB on the GPU; **33 of 34 layers offloaded** (server log: `metal_partial_offload`, mmap disabled), `context_length` 4096.
- System free memory was **10–12 % throughout** with the model loaded and **62 % after unloading it**, so the model consumes essentially all headroom.
- Swap was already 5.8 GB (of a 7.2 GB file) *before* the run and grew to ~7.5 GB during it; page-outs rose from 110 k (before Ollama started) to 305 k (before the check) and by ~14 k over the check, with a burst of +9,157 during the slowest request (the repeat, 20.5 s at 9.3 tok/s vs 12.1 s at 16.2 tok/s for identical input). Other applications on the machine were a large share of the pressure; I did not close any.
- No request failed and no out-of-memory error appeared, but timings vary by up to ~2× when memory is contended. Expect slower and noisier runs with a browser or other apps open. Options if this hurts: close apps, `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0` (suggested by the Homebrew formula; untested here), or the smaller `qwen3.5:2b` (2.7 GB; untested quality).
- The model unloads after 10 idle minutes (`keep_alive`); I unloaded it after the check. The Ollama server process itself is idle at ~13 MB.

## Context handling
- The adapter refuses a prompt whose *estimated* size plus `num_predict` exceeds `num_ctx` (Ollama would otherwise silently drop text), and errors on `done_reason=length` or a full context. None fired in the run.
- The estimate uses 3.0 chars/token; measured ≈ **3.85**, so the estimate over-counts by ~30 % and wastes ~1,500 of 4,096 tokens (a 9.1k-char budget produced 2,592 tokens). That is deliberate safety margin; raising it to ~3.5 is a possible tuning, but non-English or symbol-heavy text tokenises worse.
- A typical résumé (3–7k chars) fits without truncation; longer ones are truncated at the head and the truncation is reported. Job text is truncated the same way. Everything after the cut is invisible to the model.

## Output quality (synthetic, n = 7 jobs + résumé)
- **Résumé extraction:** valid schema every time, all 13 extracted skills present in the résumé text, seniority and years correct, identical output on repeat (deterministic at temperature 0). Over-reach: `related_titles` and search queries included "Staff Engineer"/"Principal Engineer" for a senior résumé, and "DevOps" as a domain the résumé does not name. Search queries should be reviewed or seniority-capped before use.
- **Job matching:** all 7 outcomes fell inside my expected sets (strong/possible for the two good fits and the noisy page, `not_relevant` for the nurse, accountant and principal-engineer roles, `insufficient_evidence` for the 79-char snippet); repeat run identical. The code gates worked: a snippet-only "strong_match" was capped to `possible_match`. No quote was ungrounded in this run.
- **Weaknesses the gates do not catch:** the model over-praises ("align perfectly") and did not note that the strong-fit job asked for message-queue experience the résumé lacks; its `limitations` sometimes comment on the synthetic setting. Quotes are verified to *exist* in the job text, not to *support* the claim; one "fit quote" for the noisy page was just the job title. Treat explanations as a lead, not a verdict.

## Inadequate job content (found in the test set; real content still unknown)
- **Thin snippets** (79 and 92 chars): handled — one `insufficient_evidence`, one `not_relevant` on a quoted title/scope mismatch (allowed for snippets; arguably should need more).
- **Noisy page** (2.7k chars with sign-in text and a similar-jobs list): the model ignored the noise and picked the right posting; the page text also contained relative times ("Posted 2 days ago" plus other jobs' "1 day ago", "1 week ago") — exactly the ambiguity the date gate must reject.
- **Content-level heuristic is crude:** by length alone, a complete 1,091-char posting and a 526-char one were both `partial_description` (`full_description` needs ≥ 2,000 chars). Length is a weak proxy; revisit with real returned content.
- The synthetic texts contain no dates I could test extraction on; date evidence still depends on the probe.

## What this does and does not show
It shows the pipeline pieces run locally within a 4,096 context at roughly 10–20 s per call on this Mac, with output that is schema-valid and mostly sensible on clean synthetic input. It does not show accuracy on real LinkedIn content, which may be thinner, noisier or longer; that must be judged after the probe.
