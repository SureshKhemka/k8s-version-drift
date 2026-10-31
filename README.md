# k8s-version-drift

Nightly Kubernetes version-drift summary for a fleet of ~80 clusters (EKS, GKE, self-managed).

**Code decides the facts; the LLM only writes about them.** Collection, drift rules, severities and change tags
are deterministic. A fixed three-step prompt chain turns the computed findings into a short channel message,
with a code gate after every step and a code-built fallback when the chain cannot produce a message that passes.

- Requirements: [`CLAUDE.md`](CLAUDE.md) (IDs such as FR-11, CH-4, EV-3 refer to it)
- Decisions: [`docs/decisions.md`](docs/decisions.md)
- Recorded eval baseline: [`docs/eval-baseline.md`](docs/eval-baseline.md)

This is a local prototype: clusters and the team channel are mock services, and the LLM runs locally.

## How a night works

```
inventory + matrix (data)
        │
   collect ──── GET-only, concurrent, per-cluster timeout/retries, overall time budget   (FR-1..FR-7)
        │       failures become UNREACHABLE findings, never dropped
   detect  ──── 11 drift rules in code, severities from config with FR-13 floors          (FR-8..FR-15)
        │
   diff    ──── NEW / ONGOING / WORSENED / RESOLVED / NEW_CLUSTER vs the last run         (FR-16..FR-18)
        │
   summarise ── group → Gate 1 → draft → Gate 2 → [shorten → Gate 2]                       (CH-1..CH-9)
        │       retries with gate errors fed back; otherwise the code-built fallback
   compose ──── [mentions] + body + appendix (code) + footer (code) → final Gate 2         (FR-19..FR-24)
        │
   publish ──── outbox always; POST mode sends to the webhook, once per run date           (FR-25..FR-27)
        │
   record  ──── every step appended to SQLite + var/runs/<run_id>.jsonl, redacted         (FR-30..FR-33)
```

## Quick start

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/). No model or running services are needed for the first
three commands.

```bash
uv sync
uv run pytest                    # unit tests + hard-gate evals
uv run drift eval run            # full eval report from recorded LLM outputs (about a minute)
uv run drift config check        # validate configuration (exit 40 on error)
```

Then try a real night against the mocks:

```bash
uv run drift mocks up            # terminal 1: mock clusters (:8081) + mock channel (:8082)

uv run drift mocks reset         # terminal 2: scenario back to night 0
uv run drift run --date 2026-10-01
cat var/outbox/2026-10-01/*.txt  # the message that would have been posted
```

`drift run` uses the LLM configured in `config/defaults.yaml` (LM Studio at `127.0.0.1:1234`). If it is not
running, the run still completes with the fallback summary and exits 10, or 20 because the seed scenario always
has unreachable clusters (CH-9, FR-29).

## Project layout

```
config/            defaults.yaml, per-environment overlays (local.yaml, eval.yaml)
prompts/           versioned Jinja2 prompts: group, draft, shorten, judge
seed/              generated mock world: inventory, matrix, nights 1-4, faults (see scripts/build_seed.py)
scripts/           build_seed.py: hand-written scenario and expected findings/tags
src/drift/
  collect/         inventory/matrix sources, read-only K8s client, concurrent collector
  rules/           drift rules (control plane, node pools) and the engine
  diff/            change tagging against the baseline run
  chain/           fact sheet, prompt loading, Gate 1, Gate 2, chain runner
  llm/             provider clients (OpenAI-compatible, Anthropic), cassettes for replay
  message/         code-built message parts: fallback, appendix, footer, mentions
  publish/         outbox and webhook posting
  store/           SQLite store and the append-only run recorder
  pipeline.py      one run, end to end
  cli.py           the `drift` command
src/drift_mocks/   mock K8s API and mock channel (FastAPI), fault injection, scenario world
evals/             hard-gate evals, gate sets, expected findings/tags, cassettes, harness, human labels
tests/             unit and integration tests
docs/              decisions log, recorded eval baseline
var/               runtime state (git-ignored): mocks, outbox, run records, SQLite
```

## Starting the mocks

The clusters and the channel are separate HTTP services, so swapping in real ones is a configuration change only
(CF-5).

| Service | Default address | Surface |
|---|---|---|
| mock-k8s | `127.0.0.1:8081` | `GET /clusters/{id}/version`, `GET /clusters/{id}/api/v1/nodes` (Kubernetes-shaped, paginated) |
| mock-channel | `127.0.0.1:8082` | `POST /webhook/{token}` with `{"text": ...}` (Slack incoming-webhook style) |

```bash
uv run drift mocks up                     # both, foreground; --service k8s|channel for one
uv run drift mocks reset                  # seed night 0; clears recorded calls, messages, fault counters
uv run drift mocks status                 # current scenario night and date
uv run drift mocks calls --writes-only    # any non-GET call to a cluster (there should be none)
uv run drift mocks messages               # what the channel received
```

Both mocks record every call they receive (MS-4).

**Seed data** lives in `seed/` and is generated by `scripts/build_seed.py`, which also holds the hand-written
expected findings and tags. Edit the script, then run `uv run python scripts/build_seed.py`. `tests/test_seed.py`
fails if the generated files are stale.

**Fault injection** (MS-5): per cluster in the world (`fault:`), extra overrides in `seed/faults.yaml`, or at
runtime with `PUT /_admin/faults`. Error rate, latency, timeouts, auth failures, malformed responses and scripted
failures ("times out on the first two attempts") are supported.

## Running a night on demand

```bash
export DRIFT_CHANNEL_WEBHOOK_URL=http://127.0.0.1:8082/webhook/T-local   # only needed for --post

uv run drift run --date 2026-10-01                  # DRY_RUN (default): message written to var/outbox/<date>/
uv run drift run --date 2026-10-01 --post           # post to the channel (once per run date)
uv run drift run --date 2026-10-01 --post --force   # post again for the same date
uv run drift run -c payments-prod-11 -c c003        # subset by name or ID: always DRY_RUN, never a baseline
uv run drift schedule                               # run nightly at schedule.cron in schedule.timezone
```

Other commands, useful while debugging:

```bash
uv run drift collect [-c ID ...] [--json]   # fetch only: versions per cluster, no rules
uv run drift detect --date 2026-10-02       # collect + rules: print findings, no LLM, no posting
uv run drift history payments-prod-11       # findings over time for one cluster (FR-33)
uv run drift config show                    # effective config, secrets and endpoints masked
```

**Exit codes** (FR-29). When several apply, the most actionable one wins (D32):

| Code | Meaning |
|---|---|
| 0 | success |
| 10 | success, fallback summary used |
| 20 | partial: some clusters unreachable |
| 30 | post failed; message kept in `var/outbox/` |
| 40 | configuration error; no cluster was contacted |

**What gets posted.** Every message ends with a code-generated appendix that lists every CRITICAL finding and
every unreachable cluster. It also has a footer with the run date, clusters checked vs total, compliant count,
matrix version, whether the summary is LLM-written or fallback, and the run ID. On-call mentions are added by
code, only when CRITICAL findings exist. When nothing is at MEDIUM or above and nothing is unreachable, a short
all-clear is posted instead. Set `channel.post_all_clear: false` to stay silent.

**Run records.** `var/runs/<run_id>.jsonl` and `var/drift.db` hold the inventory snapshot, raw fetch results,
normalised versions, findings and tags, every LLM call and gate result, the final message and the post result.
Everything is redacted before it is written. History is kept for `storage.retention_days`.

## Advancing scenario time

The mock world has five scripted nights (MS-6):

| Night | Date |
|---|---|
| 0 | 2026-10-01 |
| 1 | 2026-10-02 |
| 2 | 2026-10-03 |
| 3 | 2026-10-05 |
| 4 | 2026-10-08 |

```bash
uv run drift mocks night 2                 # jump to night 2 (cumulative changes from night 0)
uv run drift run --date 2026-10-03
```

Each night applies upgrades, regressions, outages and a new cluster. It also rewrites the effective inventory at
`var/mocks/inventory.yaml`, which the default config reads. The multi-night sequences, and which change tags each
one exercises, are listed in `seed/scenario.yaml`. Run the nights in order so that each one has the previous one
as its baseline.

## The prompt chain

| Step | Prompt | Output | Gate |
|---|---|---|---|
| 1. group | `prompts/group_v1.md.j2` | JSON themes over finding aliases `f1..fN` | **Gate 1**: strict schema, IDs exist, no duplicates, every CRITICAL/HIGH and NEW/WORSENED ≥ MEDIUM finding covered, theme limit |
| 2. draft | `prompts/draft_v1.md.j2` | message body | **Gate 2**: every cluster name, version, date and count checked against the fact sheet and against the clusters it is written next to; required clusters named; length; deny-list |
| 3. shorten | `prompts/shorten_v1.md.j2` | shorter body (only if the draft failed on length alone) | Gate 2 in full |

A failed gate is retried up to `chain.gate_retries` times, with the exact gate errors fed back to the model. If
the gate still fails, or the provider is unreachable, the code-built fallback is used. The composed message
(mentions + body + appendix + footer) goes through Gate 2 once more before posting. The model only ever sees the
fact sheet, never raw cluster responses. Prompt versions are recorded on every call (CH-8).

## Switching LLM provider

Providers and per-step models are configuration only (`llm.*` in `config/defaults.yaml`, LL-1/LL-2). Config is
layered: `config/defaults.yaml` → `config/$DRIFT_ENV.yaml` (default `local`) → `DRIFT__SECTION__KEY=value`
environment variables. API keys come only from environment variables (CF-3).

```bash
# another local server (Ollama)
export DRIFT__LLM__DEFAULT__PROVIDER=ollama DRIFT__LLM__DEFAULT__MODEL=gemma4:31b

# OpenRouter (OpenAI-compatible)
export DRIFT__LLM__DEFAULT__PROVIDER=openrouter DRIFT__LLM__DEFAULT__MODEL=qwen/qwen3-235b-a22b
export OPENROUTER_API_KEY=...

# Anthropic API (native provider type)
export DRIFT__LLM__DEFAULT__PROVIDER=anthropic DRIFT__LLM__DEFAULT__MODEL=claude-sonnet-5-5
export ANTHROPIC_API_KEY=...

# a different model for one step only
export DRIFT__LLM__STEPS__DRAFT__MODEL=google/gemma-4-31b

# no LLM at all: always the code-built fallback
export DRIFT__LLM__ENABLED=false
```

A new OpenAI-compatible server is a new entry under `llm.providers` with `type: openai_compat`, a `base_url`,
an optional `api_key_env` and a `json_mode` (`off`, `json_object` or `json_schema`, D34). Model-specific request
switches go in the provider's `extra_body`; the local Qwen needs `reasoning_effort: "none"` (D36). Set
`price_in_per_mtok` and `price_out_per_mtok` to get cost estimates in the run record (LL-5).

## Running evals

```bash
uv run pytest evals                 # hard gates only (EV-1..EV-10), fast, no model
uv run drift eval run               # everything, replaying recorded LLM outputs (EV-15)
```

`drift eval run` writes `reports/<timestamp>/report.{json,md}`; `reports/latest` points at the newest. It exits
0 only when the verdict is PASS.

```bash
uv run drift eval run --mode auto            # call the model only where nothing is recorded, and record it
uv run drift eval run --mode record          # re-record everything (after changing a prompt or model)
uv run drift eval run --mode live            # call the model, record nothing
uv run drift eval run -m qwen-27b --nights 0,1 --no-judge
uv run drift eval compare reports/<A> reports/<B>      # two reports side by side with deltas
uv run drift eval export-labels reports/latest         # messages for human scoring (EV-13)
```

| Report section | Requirement | Affects verdict |
|---|---|---|
| Hard gates (`evals/hard`): findings, tags, run guarantees, gate sets | EV-1..EV-10, EV-17 | yes, must be 100% |
| Every posted message re-checked by Gate 2 | EV-3 | yes, must be 0 failures |
| Cassette misses in replay mode | EV-15 | yes |
| Per chain step: first-attempt pass %, pass after retries %, attempts, latency, tokens; fallback % | EV-11 | no |
| LLM-as-judge scores per rubric criterion, with the fallback as a baseline | EV-12 | no |
| Judge vs human agreement | EV-13 | no |
| Message length distribution vs the limit | EV-14 | no |
| Model profiles side by side | EV-16 | no |

**Gate sets.** `evals/gate_sets/gate2_drafts.yaml` holds 48 bad drafts, each one a good draft with one targeted
error (wrong version, look-alike name, wrong count, missing critical cluster, leaked endpoint, …), and 21 good
drafts. `gate1_outputs.yaml` holds 18 bad Step-1 outputs and the good ones.

**Replay.** Each LLM call is keyed by provider, model, temperature, max tokens, messages and schema, and stored
under `evals/cassettes/` (D40). Re-running in replay re-evaluates code and gate changes against the same model
answers. A prompt change changes the key, so it shows up as a cassette miss, never as a stale answer.

**Comparing models** (EV-16). Add a profile under `evals.models` (any provider and model) and run
`drift eval run -m <a> -m <b>`.

**Human labels** (EV-13). `export-labels` copies up to 15 messages into `evals/human_labels/labels.yaml`. Fill in
each `human_score` (1–5, using the rubric in `prompts/judge_v1.md.j2`). The next `drift eval run` then reports
the mean absolute error, exact and within-one agreement, and the Spearman correlation between judge and human.

**Repeatable** (EV-17). Evals use in-process mocks, fixed scenario dates, fixed seeds and deterministic run IDs.
Replay makes the LLM part deterministic too.

## Status and known gaps

All milestones (M0–M7) are complete and every hard gate passes. Still open:

- **EV-13 human labels**: 15 messages are exported to `evals/human_labels/labels.yaml`, but none are scored yet,
  so the judge is not validated. The report says so.
- **Independent judge**: the judge (`google/gemma-4-31b`) is also one of the two models under test. The report
  warns about this (D45). Point `llm.steps.judge` at a third model for a fair comparison.
- **Step 1 on qwen3.8-27b** passes Gate 1 on its first attempt only 20% of the time (see the baseline). A
  `group_v2` prompt aimed at its most common gate errors is the next prompt iteration.
- Out of scope by design (spec §8): real clusters and channel, remediation, add-on/OS drift, web UI, tracing
  vendor, production deployment.
