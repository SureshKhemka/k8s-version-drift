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
| EV-3 | Every posted message passes Gate 2 against that run's own findings |
| EV-9 | 48 hand-written bad drafts are all rejected by Gate 2 (with the right reason); 21 good drafts all pass |
| EV-10 | 18 bad Step-1 outputs are all rejected by Gate 1; good ones pass |

The gate sets live in `evals/gate_sets/`. A bad draft is usually a good draft with one targeted edit, so each
case tests exactly one failure (wrong version, look-alike name, wrong count, missing critical cluster, …).

## The LLM summary (prompt chain)

`drift run` summarises with a fixed three-step chain; code checks every step and nothing unchecked is posted:

1. **group** (`prompts/group_v1.md.j2`) → JSON themes → **Gate 1**: schema, IDs exist, no duplicates, every
   CRITICAL/HIGH and NEW/WORSENED ≥ MEDIUM finding covered, theme limit.
2. **draft** (`prompts/draft_v1.md.j2`) → message text → **Gate 2**: every cluster name, version, date and
   count is checked against the findings (and against the clusters it is written next to), required clusters
   named, length, deny-list.
3. **shorten** (`prompts/shorten_v1.md.j2`) only if the draft failed on length alone; Gate 2 re-runs in full.

Failed gates are retried (`chain.gate_retries`) with the exact errors fed back. After that, or if the LLM is
unreachable, the code-built fallback is posted. Every call, prompt version, output and gate result is in the
run record.

## Switching LLM provider

Providers and per-step models are configuration only (`llm.*` in `config/defaults.yaml`):

```bash
# another local server (Ollama)
export DRIFT__LLM__DEFAULT__PROVIDER=ollama DRIFT__LLM__DEFAULT__MODEL=gemma4:31b
# OpenRouter (OpenAI-compatible)
export DRIFT__LLM__DEFAULT__PROVIDER=openrouter DRIFT__LLM__DEFAULT__MODEL=qwen/qwen3-235b-a22b OPENROUTER_API_KEY=...
# Anthropic (native provider type)
export DRIFT__LLM__DEFAULT__PROVIDER=anthropic DRIFT__LLM__DEFAULT__MODEL=claude-sonnet-5-5 ANTHROPIC_API_KEY=...
# a different model for one step only
export DRIFT__LLM__STEPS__DRAFT__MODEL=google/gemma-4-31b
# no LLM at all (fallback only)
export DRIFT__LLM__ENABLED=false
```

Model-specific request switches go in the provider's `extra_body` (the local Qwen needs
`reasoning_effort: "none"`, see D36). Set `price_in_per_mtok`/`price_out_per_mtok` to get cost estimates.

## Running evals (`drift eval`)

One command runs everything and writes `reports/<timestamp>/report.{json,md}` (`reports/latest` points at the
newest). Exit code 0 only when the verdict is PASS.

```bash
uv run drift eval run                       # replay mode: recorded LLM outputs, no model needed (EV-15)
uv run drift eval run --mode auto           # call the model only where nothing is recorded, and record it
uv run drift eval run --mode record         # re-record everything (after changing a prompt or model)
uv run drift eval run --mode live           # call the model, record nothing
uv run drift eval run -m qwen-27b --nights 0,1 --no-judge   # one profile, two nights, no judge
uv run drift eval compare reports/<A> reports/<B>           # metrics side by side with deltas
```

What a report contains:

| Section | Requirement |
|---|---|
| Hard gates (pytest `evals/hard`) — must be 100% | EV-1..EV-10 |
| Every posted message re-checked by Gate 2 — must be 0 failures | EV-3 |
| Per chain step: first-attempt pass %, pass after retries %, mean attempts, latency, tokens; fallback % and reasons | EV-11 |
| Judge scores per rubric criterion (`prompts/judge_v1.md.j2`), with the code-only fallback as a baseline | EV-12 |
| Judge vs human agreement on `evals/human_labels/labels.yaml` | EV-13 |
| Message length min / median / max and count over the limit | EV-14 |
| Model profiles side by side (`evals.models` in config) | EV-16 |

Verdict FAIL means: a hard gate failed, a posted message failed Gate 2, or (in replay) an LLM call had no
recording — re-record with `--mode auto`. Judge scores never change the verdict.

**Replay (EV-15).** Every LLM call is keyed by provider, model, temperature, max tokens, messages and schema
and stored under `evals/cassettes/`. Changing code or gates and re-running in replay re-evaluates the same
model answers; changing a prompt changes the key and shows up as a cassette miss, never as a stale answer.

**Model comparison (EV-16).** Add a profile under `evals.models` (any provider/model; config only) and run
`drift eval run -m <a> -m <b>`.

**Human labels (EV-13).** `drift eval export-labels reports/latest` copies up to 15 messages (LLM-written and
fallback) into `evals/human_labels/labels.yaml`. Score each `human_score` 1–5 using the judge rubric; the next
`drift eval run` reports mean absolute error, exact / within-one agreement and Spearman correlation between
judge and human. Until 15 items are scored the section says "pending".

**Repeatable (EV-17).** Evals use the in-process mocks, fixed scenario dates and fixed seeds; replay makes
the LLM part deterministic too.
