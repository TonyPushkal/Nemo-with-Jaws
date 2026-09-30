# Nemo-with-Jaws

Personal job-search agent.

- Phase 1: find relevant jobs across the internet from a structured search brief.
- Phase 2: tailor résumé, ATS parsing/format/alignment checks.
- Phase 3: assist with applications.

**Status: Phase 1 plan v3.1 (résumé + lookback → LinkedIn job URLs). Built so far: corrected monetary guard + spend ledger, and a provider feasibility probe (prepared, not yet run). The search service itself is not started.** Contract and plan:

1. [Reference repo assessment](docs/phase1/01-reference-assessment.md) (written for the earlier plan; lessons still apply)
2. [Scope and contract, non-goals, acceptance criteria](docs/phase1/02-scope.md)
3. [Architecture, posting-time window, providers, SQLite, bounds](docs/phase1/03-architecture.md)
4. [Milestones and tests](docs/phase1/04-milestones-and-evaluation.md)
5. [Decisions, assumptions, open questions](docs/phase1/05-decisions-and-open-questions.md)

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest            # offline; tests block all network access
.venv/bin/python scripts/probe_provider.py --dry-run   # show the probe plan and worst-case cost; no calls
```

Paid external calls are refused unless `NEMO_PAID_CALLS_ENABLED=true` and `NEMO_MAX_USD_PER_RUN` are set (optional `NEMO_MONTHLY_USD_CAP`). Keys come from the environment only. Résumés, SQLite files and probe output are gitignored.
