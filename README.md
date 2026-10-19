# Nemo-with-Jaws

Personal job-search agent.

- Phase 1: find relevant jobs across the internet from a structured search brief.
- Phase 2: tailor résumé, ATS parsing/format/alignment checks.
- Phase 3: assist with applications.

**Status: Phase 1 plan v3.3 (job profile + lookback → LinkedIn job URLs). Built: monetary guard + spend ledger, SerpApi search collector (`scripts/run_search.py`, the working discovery path), Tavily adapter and probes (Tavily was tried and rejected), local Ollama adapter, job-profile parser (`src/nemo/profile.py`) and matcher (`src/nemo/tasks.py`). Screening/ranking and a service API are proposed, not built: see [09 Phase 1 architecture](docs/phase1/09-phase1-architecture.md).** Contract and plan:

1. [Reference repo assessment](docs/phase1/01-reference-assessment.md) (written for the earlier plan; lessons still apply)
2. [Scope and contract, non-goals, acceptance criteria](docs/phase1/02-scope.md)
3. [Architecture, posting-time window, providers, SQLite, bounds](docs/phase1/03-architecture.md)
4. [Milestones and tests](docs/phase1/04-milestones-and-evaluation.md)
5. [Decisions, assumptions, open questions](docs/phase1/05-decisions-and-open-questions.md)
6. [Local model findings (Ollama qwen3.5:4b)](docs/phase1/06-local-model-findings.md)
7. [Search-provider findings (SerpApi, Tavily)](docs/phase1/07-search-provider-findings.md)
9. [Phase 1 architecture (short, current)](docs/phase1/09-phase1-architecture.md)

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                                   # offline; tests block all network access
cp .env.example .env                                         # then fill in your key/budget locally (gitignored)
.venv/bin/python scripts/probe_provider.py --dry-run         # show the probe plan and worst-case cost; no calls
.venv/bin/python scripts/probe_provider.py                   # real probe: asks for confirmation
```

Discovery run (Phase 1; SerpApi key and budget in `.env`):

```bash
.venv/bin/python scripts/run_search.py --profile job-profile.md --lookback 7d --gl in   # cities from config default_locations (Bengaluru, Hyderabad); add --dry-run to preview, --assess for optional local-model notes
```

Searches come from `config/search_terms.json` (SDE and backend families, early-career aliases, plain roles; `--search-config` to override, `--plain-roles` to search desired roles as written). `--dry-run` shows the query order, locations and which searches fit `--max-searches`; `--offset N` runs the next batch of a capped plan; `--screen` also writes `report.md` (per-query results, screening, application links). Jobs without a LinkedIn link are kept with their application links. Screen saved results (no network): `.venv/bin/python scripts/screen_results.py`.

Results go to `runs/<run-id>/` (`summary.md`, `results.csv`, `results.json`, `raw/`) and `data/nemo.sqlite`; both are gitignored. Reported posting ages are Google's unverified claims; nothing is filtered by date or fit.

Local model (Ollama, `qwen3.5:4b`, 4,096 context, one request at a time; findings in `docs/phase1/06-local-model-findings.md`):

```bash
brew install ollama && ollama pull qwen3.5:4b
OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=4096 ollama serve   # separate terminal
.venv/bin/python scripts/local_model_check.py                # synthetic check; --profile my.md / --from-probe <dir>
```

Paid external calls are refused unless `NEMO_PAID_CALLS_ENABLED=true` and `NEMO_MAX_USD_PER_RUN` are set (optional `NEMO_MONTHLY_USD_CAP`). Keys come from the environment or `.env` only. Résumés, SQLite files, `.env` and probe output are gitignored.

Collection service (stdlib, localhost, one run at a time; same flow and spend guards as `run_search.py --screen`):

```bash
.venv/bin/python scripts/serve.py                       # http://127.0.0.1:8765
curl -s -X POST localhost:8765/runs -d '{"cities": ["Bengaluru", "Hyderabad"], "max_age_hours": 24, "request_budget": 12}'
curl -s localhost:8765/runs/<run_id>                     # state, stop reason, freshness, fresh shortlist
```

A request can instead supply its own profile (`experience`, `desired_roles`, optional `must_haves`/`nice_to_haves`/`exclusions`)
instead of `titles`; it is screened against that profile, not `job-profile.md`, and capped at 10 searches per run regardless of
`request_budget`. `GET /quota` reports a shared rate limit (`NEMO_RATE_LIMIT_PER_HOUR`, default 5 runs/hour across every caller) so
a public deployment can't run up unbounded SerpApi spend; `scripts/serve.py` also serves a static frontend (`frontend/`) at `/` that
uses this — a visitor fills in their own background, the page polls the run and shows matches. Nothing a visitor submits is stored
beyond `runs/<run_id>/profile.md` for that one run (gitignored, same as everything else under `runs/`).

## Public demo deployment

`render.yaml` deploys the service (API + static frontend, one process) to [Render](https://render.com)'s free tier:

1. Push this repo to GitHub, then on Render: New → Blueprint → pick the repo (Render reads `render.yaml`).
2. Fill in the env vars Render prompts for: `SERPAPI_API_KEY`, `NEMO_MAX_USD_PER_RUN` (e.g. `0.50`), optionally
   `NEMO_MONTHLY_USD_CAP`, and `NEMO_SERVICE_TOKEN` (any random string — required because the service binds to `0.0.0.0`
   on Render, not loopback).
3. Copy that same `NEMO_SERVICE_TOKEN` value into `API_TOKEN` at the top of `frontend/app.js` before deploying. **This is not a
   real secret** — it ships to every visitor's browser and is visible via view-source. It only satisfies the service's
   non-loopback guard; the actual protections against runaway cost are the shared `/quota` rate limit and your SerpApi budget
   caps above.
4. Render's free plan spins down after idle and cold-starts on the next request (tens of seconds) — expected for a demo, not a
   bug. The in-memory rate limiter and `data/nemo.sqlite` ledger reset on every cold start/redeploy.
