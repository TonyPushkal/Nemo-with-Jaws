# Phase 1 — Reference Repo Assessment

Status: DRAFT for review. Facts below come from reading the code (2026-09-30). Nothing was executed; behaviour claims marked *(by reading)* are inferred from source, not run. Reference repos were not modified.

No `CLAUDE.md` or `AGENTS.md` exists in either repo or in `Hunt/`. Neither repo has a `LICENSE` file.

## 1. `job-search-agent` (Kalyanmadhunala-labs, ~2.6k lines incl. images; LangGraph + Tavily + gpt-4o-mini)

| Observation | Verdict | Evidence |
|---|---|---|
| Matching is mainly fixed-list skill overlap | **Confirmed** | `src/utils/resume_parser.py:22-45` hard-codes `KNOWN_SKILLS` (DS/ML/SWE terms only). `nodes.py:252-259` scores `len(resume ∩ required)/len(required)`. Without a résumé it scores `len(required)/20` (`nodes.py:256`), i.e. "more listed skills = better". |
| Truncates search snippets | **Confirmed** | `settings.py:26` `MAX_SNIPPET_CHARS=500`, applied at `nodes.py:153`. Only `raw[:12]` results ever reach the LLM (`nodes.py:154`). Tavily itself returns ≤500-char chunks. |
| Repeats fixed queries | **Confirmed** | `plan_searches` (`nodes.py:91-95`) builds 3 templated strings and never reads `iteration`, so a re-plan yields identical queries. |
| Iteration counter never incremented | **Confirmed** | `increment_iteration` (`nodes.py:289`) is defined but referenced nowhere (grep) and not added in `graph.py:61-87`. `iteration` stays 0, so `should_continue` (`nodes.py:281-286`) never hits the max-iterations exit; with <3 jobs the graph loops until LangGraph's own recursion limit *(by reading)*. README claims it "always terminates" (README:309) — not true as written. Also on re-loop, `execute_searches` de-dups against seen URLs, so the second pass adds ~0 new results, and `extract_jobs` overwrites `jobs` from the same `raw[:12]`. |
| Other defects | — | `.strip("```json")` (`nodes.py:54,173`) is a character-set strip, not a fence removal. Tavily restricted to `JOB_DOMAINS` (`settings.py:29-32`): no employer career pages. Only search snippets are read — job pages are never fetched. No persistence between runs. `.env.example`/README mention MIT but no `LICENSE` file exists. |
| Good ideas | — | Clean tool boundary (`tools/search.py`), tests that run without keys, per-run `agent_log`. |

## 2. `JobSearchAgent` (TonyPushkal fork of RamyaPasupuleti/JobSearchAgent, ~10k lines; JobSpy + Azure OpenAI + SQLite)

| Observation | Verdict | Evidence |
|---|---|---|
| Scheduling, history, reporting | **Confirmed** | APScheduler cron in `main.py:395-424`; SQLite `seen_jobs` + `jobs` + `search_runs` (`storage/*.py`); CSV/Excel/email in `output/`. |
| Hard-coded USA / Seattle / Power Platform | **Confirmed** | `config.py:25-31` defaults (keywords, `Redmond WA…`), `config.py:33` `country="US"`, `:142-153` primary skills; `job_discovery_agent.py:20-35` prompt; `job_analyzer_agent.py:73-96,224` ("Seattle area"); `jobspy_scraper.py:56` `country="USA"`. |
| Hard-coded role exclusions | **Confirmed** | `config.py:188-240` excludes Data Scientist/ML/Python/Java/.NET/DevOps/Salesforce/SAP…; `job_analyzer_agent.py:29-33` regex title disqualifiers (Principal/Director/"Machine Learning"…) applied *before* any reading of the description. Title-only hard filters are a recall risk. |
| "AI-powered" scoring is really keywords | **Found** | `analyze_jobs` (`:337-396`) calls `_keyword_evaluate` only; `_semantic_evaluate` (`:234`) is never referenced (grep). `create_agent()` on all agents is never called, so the Microsoft Agent Framework "multi-agent" layer is nominal. Score is an arbitrary additive number (title +≤30, must-have +≤30, …) displayed as "%". |
| Job descriptions truncated | **Found** | `jobspy_scraper.py:302` keeps `description[:500]`. |
| Failures invisible | **Found** | Per-search exceptions are logged and `continue`d (`jobspy_scraper.py:215-217`); discovery returns `[]` on error (`job_discovery_agent.py:100-102`); `main.py:267` then prints "No jobs found" — a blocked source and an empty result look identical. `search_runs.errors` only captures orchestrator-level exceptions. |
| Dedup is weak | **Found** | Key = hash(title\|company\|location) (`models/job_listing.py:71`), no URL canonicalisation, no employer-domain link; `seen_jobs` PK is that hash (`deduplication_store.py:24`), so reposts and same-title roles in one city collapse. Source priority hard-coded (`deduplication_agent.py:162-167`). |
| Other | — | Old reports deleted on every run (`main.py:179-192`). Analysis runs before dedup (wasted work, `main.py:275-286`). Blocking sync `scrape_jobs` inside `async`. Individual `scrapers/*_scraper.py` (~1.9k lines) are exported but not used by discovery. Depends on a pinned beta (`agent-framework==1.0.0b251105`). |
| Someone else's data | **Confirmed** | Committed: `data/input/Ramya_Bhargavi_P_Resume.docx`, `data/resume_context.json` (cached profile), `data/job_history.db`, `data/output/jobs_2026-01-19.csv`. Origin is the upstream author; **none of this is Tony's information** and nothing from it will be copied. (I only listed the JSON's top-level keys; I did not read the résumé content.) |
| Good ideas | — | Persisted `search_runs`, `first_seen/last_seen`, `is_new` flag, salary normalisation, retry/backoff helper concept, per-board rate limit config. |

## 3. Licensing

- GitHub reports `license: null` for both repos (checked via public API).
- `job-search-agent`: README says MIT, but there is no LICENSE file → legally ambiguous.
- `JobSearchAgent`: no license anywhere; it is a fork of another person's repo → default copyright applies.
- **Conclusion: copy no code from either repo.** Reuse *ideas* only (schema shape, run-log concept), reimplemented from scratch. Third-party libraries (e.g. JobSpy) have their own licences and are judged separately.
- LinkedIn's User Agreement §8.2 prohibits scraping/bots (checked 2026-09-30). JobSpy-style LinkedIn scraping conflicts with that; Phase 1 should not scrape LinkedIn directly.

## 4. Reuse / adapt / build fresh

| Item | Decision |
|---|---|
| LangGraph state machine, MS Agent Framework | **Build fresh, no framework.** A plain Python pipeline with a hard step budget is sufficient and easier to bound. |
| Search tool wrapper concept | **Adapt idea**, rewrite. Provider behind a small interface. |
| Keyword/regex skill matching | **Do not reuse.** Use as at most a cheap pre-filter, never as the score. |
| Hard-coded preferences/exclusions | **Do not reuse.** All preferences come from the user's brief. |
| SQLite run/history tables | **Adapt idea**, new schema (see architecture doc). |
| JobSpy / board scrapers | **Do not use for LinkedIn/Glassdoor.** Revisit others only if a user-approved source needs it. |
| CSV/Excel/email output | **Build fresh, Markdown + JSON first.** |
| Tests without live keys | **Adopt the practice.** |
