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

## Mock services

Two separate local services stand in for the clusters and the team channel (swap for real ones by config only):

| Service | Default address | Surface |
|---|---|---|
| mock-k8s | `127.0.0.1:8081` | `GET /clusters/{id}/version`, `GET /clusters/{id}/api/v1/nodes` (Kubernetes-shaped, paginated) |
| mock-channel | `127.0.0.1:8082` | `POST /webhook/{token}` with `{"text": ...}` (Slack-webhook style) |

```bash
uv run drift mocks up                 # start both (foreground); --service k8s|channel for one
uv run drift mocks reset              # back to seed night 0; clears calls, messages, fault counters
uv run drift mocks status             # current scenario night/date
uv run drift mocks calls --writes-only  # any non-GET call to a cluster (should be none)
uv run drift mocks messages           # what the channel received
export DRIFT_CHANNEL_WEBHOOK_URL=http://127.0.0.1:8082/webhook/T-local   # needed for POST mode
```

Seed data lives in `seed/` and is **generated** from `scripts/build_seed.py` (the hand-written scenario + expected
findings). Edit the script, then `uv run python scripts/build_seed.py`. `tests/test_seed.py` fails if files are stale.
Fault injection: per cluster in the world (`fault:`), extra overrides in `seed/faults.yaml`, or at runtime via
`PUT /_admin/faults`.

## Scenario time travel

```bash
uv run drift mocks night 2            # nights 0-4: 2026-10-01, -02, -03, -05, -08
```

Each night applies cumulative changes (upgrades, regressions, outages, a new cluster) and rewrites the effective
inventory at `var/mocks/inventory.yaml`. Sequences are listed in `seed/scenario.yaml`.

## Running a night on demand

```bash
uv run drift mocks up &                                   # in another terminal, or backgrounded
export DRIFT_CHANNEL_WEBHOOK_URL=http://127.0.0.1:8082/webhook/T-local
uv run drift mocks night 0
uv run drift run --date 2026-10-01                         # DRY_RUN (default): message written to var/outbox/
uv run drift run --date 2026-10-01 --post                  # post to the channel (once per run date)
uv run drift run --date 2026-10-01 --post --force          # post again for the same date
uv run drift run -c payments-prod-11 -c c003               # subset: always DRY_RUN, never a baseline
uv run drift history payments-prod-11                      # findings over time for one cluster
uv run drift schedule                                      # nightly at schedule.cron in schedule.timezone
```

Exit codes: `0` success · `10` success with fallback summary · `20` partial (some clusters unreachable) ·
`30` post failed (message kept in `var/outbox/`) · `40` configuration error.

Every message ends with a code-generated appendix (every CRITICAL finding and unreachable cluster) and a footer
(run date, clusters checked, compliant count, matrix version, summary source, run ID). Run records:
`var/runs/<run_id>.jsonl` and `var/drift.db`.

## Collecting versions (no rules yet)

```bash
uv run drift collect                  # all clusters in the inventory, one line each
uv run drift collect -c c003 -c payments-prod-11 --json   # subset, raw fetch records
```

Access is GET-only, enforced by the client and again by the HTTP transport. Concurrency, per-attempt timeout,
retries and the overall time budget come from `clusters.*` in config.

## Drift findings (deterministic)

```bash
uv run drift detect --date 2026-10-02     # collect + evaluate every FR-11 rule, print findings
```

Rules live in `src/drift/rules/` (no LLM). Severities per rule and environment come from `policy.severities`.

## Evals

```bash
uv run pytest evals          # hard gates: must be 100%
```

| Gate | What it checks |
|---|---|
| EV-1 | Each scenario night: every finding field equals the hand-written expectation in `evals/expected/` |
| EV-2 | Nights 0→4 in order: every NEW/ONGOING/WORSENED/RESOLVED/NEW_CLUSTER tag as expected |
| EV-4 | Every CRITICAL finding and prod UNREACHABLE cluster is in every posted message |
| EV-5 | No non-GET call ever reached a mock cluster |
| EV-6 | No secret value, endpoint or deny-listed string in messages, outbox, run records, SQLite or logs |
| EV-7 | A message is posted when the LLM is down, when every chain step fails, and on a flaky channel |
| EV-8 | A run date is posted once unless `--force`; a failed post does not block a retry |

_More sections (running a night, evals, switching LLM provider) are added as milestones land._
