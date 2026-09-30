# Nemo-with-Jaws

Personal job-search agent.

- Phase 1: find relevant jobs across the internet from a structured search brief.
- Phase 2: tailor résumé, ATS parsing/format/alignment checks.
- Phase 3: assist with applications.

**Status: Phase 1 plan v3.2 (résumé + lookback → LinkedIn job URLs). Built: corrected monetary guard + spend ledger, Tavily adapter, and a provider feasibility probe (ready to run, not yet run). The search service itself is not started; the v2 modules were removed (recoverable from tag `checkpoint-before-prune`).** Contract and plan:

1. [Reference repo assessment](docs/phase1/01-reference-assessment.md) (written for the earlier plan; lessons still apply)
2. [Scope and contract, non-goals, acceptance criteria](docs/phase1/02-scope.md)
3. [Architecture, posting-time window, providers, SQLite, bounds](docs/phase1/03-architecture.md)
4. [Milestones and tests](docs/phase1/04-milestones-and-evaluation.md)
5. [Decisions, assumptions, open questions](docs/phase1/05-decisions-and-open-questions.md)
6. [Local model findings (Ollama qwen3.5:4b)](docs/phase1/06-local-model-findings.md)

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                                   # offline; tests block all network access
cp .env.example .env                                         # then fill in your key/budget locally (gitignored)
.venv/bin/python scripts/probe_provider.py --dry-run         # show the probe plan and worst-case cost; no calls
.venv/bin/python scripts/probe_provider.py                   # real probe: asks for confirmation
```

Local model (Ollama, `qwen3.5:4b`, 4,096 context, one request at a time; findings in `docs/phase1/06-local-model-findings.md`):

```bash
brew install ollama && ollama pull qwen3.5:4b
OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=4096 ollama serve   # separate terminal
.venv/bin/python scripts/local_model_check.py                # synthetic check; --from-probe <dir> for real content
```

Paid external calls are refused unless `NEMO_PAID_CALLS_ENABLED=true` and `NEMO_MAX_USD_PER_RUN` are set (optional `NEMO_MONTHLY_USD_CAP`). Keys come from the environment or `.env` only. Résumés, SQLite files, `.env` and probe output are gitignored.
