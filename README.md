# k8s-version-drift

Nightly Kubernetes version-drift summary. Code computes every fact; an LLM prompt chain only writes about them, behind deterministic gates, with a code-only fallback.

Requirements: see `CLAUDE.md`. Decisions: `docs/decisions.md`.

## Setup

```bash
uv sync
uv run pytest
uv run drift config show     # effective config, secrets/endpoints masked
uv run drift config check    # full validation (exit code 40 on error)
```

Configuration layers: `config/defaults.yaml` → `config/$DRIFT_ENV.yaml` (default `local`) → `DRIFT__SECTION__KEY=value` env vars.

_More sections (mocks, running a night, scenario time, evals, switching LLM provider) are added as milestones land._
