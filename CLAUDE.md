# Kubernetes Version Drift Summary — Requirements Specification

This document defines **what** the system must do. It deliberately contains no design or architecture. Before writing code, the implementer (Claude Code) must propose a tech stack, project layout and architecture to the user and wait for approval. Every requirement has an ID so code, tests and evals can refer back to it.

Guiding principle for every requirement below: **code decides the facts, the LLM only writes about them.** Fetching versions, comparing them to the supported-version matrix, and assigning severity are deterministic. The LLM turns already-computed findings into a short, readable summary through a fixed chain of prompts with a code check after every step.

---

## 1. Context and goals

A platform team runs about 80 Kubernetes clusters across AWS (EKS), GCP (GKE) and self-managed installs. Every night the team wants to know which clusters have drifted from the supported-version matrix: control planes that are unsupported or close to end of support, node pools lagging behind their control plane, and clusters that could not be checked. Today this is done by hand, irregularly.

The system must, once a night:

1. Fetch the control-plane version and every node's kubelet version for each cluster in the inventory.
2. Compare them to the supported-version matrix and to the version skew policy.
3. Compare tonight's findings with the previous run to show what is new, worse, or resolved.
4. Use an LLM, through a prompt chain, to write a short drift summary.
5. Post that summary to the team channel.

Goals, in priority order:

1. Never post a wrong fact: a wrong version, a wrong cluster name, a wrong count, or a wrong date.
2. Never silently leave out a critical finding or a cluster that could not be checked.
3. Post something every night, even when the LLM is down or misbehaves.
4. Keep the message short enough that people actually read it.
5. Prove, with evals, that the above holds for any model plugged in.

This is a learning and prototype build. It runs locally, all clusters and the team channel are mocked, and the LLM runs locally.

---

## 2. Glossary

- **Cluster**: one Kubernetes cluster in the inventory, with a unique ID, display name, provider (eks, gke, self-managed), environment (prod, staging, dev), region, and owning team.
- **Control-plane version**: the API server version reported by the cluster (e.g., `v1.31.4`).
- **Node version**: the kubelet version reported by one node. Nodes belong to node pools.
- **Minor version**: the `major.minor` part (e.g., `1.31`). **Patch**: the third part.
- **Supported-version matrix**: data describing, per provider, which minor versions are supported, their latest patch, and their end-of-standard-support and end-of-life dates.
- **Skew policy**: the maximum allowed gap between node and control-plane minor versions, and the rule that nodes must not be newer than the control plane.
- **Finding**: one deterministic, structured fact about drift for one cluster (see FR-10).
- **Run**: one execution of the whole workflow, identified by a run ID and a run date.
- **Chain step**: one LLM call in the prompt chain, followed by a code gate.
- **Gate**: a deterministic code check on a chain step's output. A failed gate leads to a retry or the fallback; never to posting unchecked output.
- **Fallback message**: a message generated entirely by code from the findings, used when the LLM chain cannot produce a message that passes every gate.

---

## 3. Functional requirements

### 3.1 Inventory and collection

- **FR-1** The cluster inventory is data, loaded from a configured source. Adding or removing a cluster must not require a code change.
- **FR-2** For each cluster, fetch the control-plane version and the list of nodes with their node pool, kubelet version, and ready status.
- **FR-3** Access to clusters is read-only. The system must never create, modify or delete anything in any cluster. This is an invariant that configuration cannot override.
- **FR-4** Clusters are fetched concurrently up to a configured limit, with a configured per-cluster timeout and retry count.
- **FR-5** A cluster that cannot be reached, times out, returns an auth error, or returns malformed data after retries is recorded as **UNREACHABLE** with the reason. It is never dropped from the run.
- **FR-6** The whole collection phase must finish within a configured time budget. Clusters not fetched in time are recorded as UNREACHABLE with reason "time budget exceeded".
- **FR-7** Version strings are normalised (strip leading `v`, strip provider suffixes such as `-eks-abc123` or `-gke.1200`) while keeping the raw string for the record. A version that cannot be parsed produces an UNPARSEABLE_VERSION finding.

### 3.2 Drift detection (deterministic, no LLM)

- **FR-8** The supported-version matrix is data, loaded from a configured source, with a version/date stamp. The run records which matrix version it used.
- **FR-9** Every rule below is evaluated in code. The LLM is never asked whether something is drift, how severe it is, or what the correct version is.
- **FR-10** Each finding has: finding ID (stable across runs for the same cluster + rule + subject), cluster ID, rule, severity, subject (control plane or node pool name), observed version(s), expected/limit value, relevant date if any, days remaining if any, and number of nodes affected if any.
- **FR-11** Drift rules:

| Rule | Condition |
|---|---|
| CP_EOL | Control-plane minor is past end of life for its provider |
| CP_UNSUPPORTED | Control-plane minor is past end of standard support (e.g., in paid extended support) |
| CP_EOL_SOON | End of standard support or end of life is within the configured warning window |
| CP_UNKNOWN_VERSION | Control-plane minor is not in the matrix at all (newer or unknown) |
| CP_PATCH_BEHIND | Control-plane patch is behind the latest patch for its minor by at least the configured number of patches |
| NODE_SKEW_EXCEEDED | A node pool's minor is older than the control plane by more than the allowed skew |
| NODE_NEWER_THAN_CP | A node pool's minor is newer than the control plane |
| NODE_POOL_MIXED | Nodes in one node pool report different minor versions (e.g., mid-upgrade or stuck) |
| NODE_EOL | A node pool's minor is past end of life |
| UNREACHABLE | Cluster could not be checked (FR-5, FR-6) |
| UNPARSEABLE_VERSION | A reported version could not be parsed |

- **FR-12** Severity per rule is set in configuration, with optional overrides by environment (e.g., CP_EOL_SOON is HIGH in prod, MEDIUM in dev). Severities are CRITICAL, HIGH, MEDIUM, LOW, INFO.
- **FR-13** The following cannot be configured below HIGH: CP_EOL, NODE_NEWER_THAN_CP, and UNREACHABLE for prod clusters. Configuration that tries to do so is rejected at startup.
- **FR-14** A cluster with no findings is COMPLIANT. The run records the compliant count.
- **FR-15** "Now" and the run date are injectable so that date-based rules can be tested at exact boundaries.

### 3.3 Change since last run

- **FR-16** Tonight's findings are compared with the last successful run and each finding is tagged NEW, ONGOING, WORSENED (severity went up, or days remaining crossed a configured threshold), or RESOLVED (present last run, gone tonight).
- **FR-17** A cluster that was reachable last run and is UNREACHABLE tonight is tagged NEW. A cluster present in the inventory for the first time is tagged NEW_CLUSTER, not treated as a set of new findings.
- **FR-18** If there is no previous run, all findings are tagged NEW and the summary says this is the first run.

### 3.4 Summary via prompt chaining

The LLM part of the workflow is a fixed chain. Each step has one job, a versioned prompt, a structured or bounded output, and a code gate. The input to the chain is the structured findings and run statistics only — never raw cluster API responses.

- **CH-1 Step 1 — Group and prioritise.** Input: findings with change tags, run statistics. Output: structured data grouping findings into a small number of themes (e.g., "clusters on one minor version reaching end of support"), each with a title, the finding IDs it covers, and a priority rank. Maximum number of themes is configured.
- **CH-2 Gate 1.** Output parses against the schema; every referenced finding ID exists; no finding ID appears in more than one theme; every CRITICAL and HIGH finding and every NEW or WORSENED finding at MEDIUM or above is covered; theme count is within the limit. LOW and INFO findings may be left out.
- **CH-3 Step 2 — Draft the message.** Input: the themes from Step 1 plus the facts for every finding referenced. Output: the channel message text, starting with a one-line overall status, then the themes in priority order, then what was resolved.
- **CH-4 Gate 2 (faithfulness).** Code extracts every cluster name, version string, number, and date from the draft and checks each against the findings and run statistics. The draft fails if it contains any value not present in the facts, names a cluster in a context that doesn't match its finding, misstates a count, omits any cluster that has a CRITICAL finding or is UNREACHABLE in prod, exceeds the configured length, or contains anything on the configured deny-list (e.g., API server endpoints, account IDs, tokens).
- **CH-5 Step 3 — Shorten (conditional).** Runs only if the draft passed every Gate 2 check except length. Input: the draft plus the limit. Output: a shorter version. Gate 2 is re-run in full on the result.
- **CH-6** On any gate failure, the step is retried up to a configured count, with the specific gate errors fed back to the model. After the last retry, the run uses the fallback message (FR-22). A step never proceeds on output that failed its gate.
- **CH-7** Each step can use a different model, temperature, max tokens and timeout, set in configuration. By default all steps use the same local model.
- **CH-8** Prompts live in files outside code, each with a version identifier recorded on every call.
- **CH-9** If the LLM provider is unreachable, the chain is skipped and the fallback message is used; this is not a run failure.

### 3.5 The posted message

- **FR-19** Every posted message, whether LLM-written or fallback, ends with a code-generated appendix listing every CRITICAL finding and every UNREACHABLE cluster, one line each, with cluster name, rule, observed value and limit. This appendix is never written or edited by the LLM.
- **FR-20** The message footer states: run date, clusters checked vs total, compliant count, matrix version, whether the summary was LLM-written or fallback, and the run ID.
- **FR-21** If there are no findings at MEDIUM or above and nothing unreachable, a short "all clear" message is posted (configurable to post or stay silent).
- **FR-22** The fallback message is built entirely from a template and the findings: status line, counts by severity, NEW/WORSENED/RESOLVED counts, and the appendix. It must itself pass Gate 2.
- **FR-23** Mentions (e.g., notifying the on-call group) are added by code, only when CRITICAL findings exist, based on configuration. The LLM does not decide who to notify.
- **FR-24** Channel formatting (e.g., Slack markdown) and message length limit come from configuration.

### 3.6 Posting and run behaviour

- **FR-25** Output mode is configured: DRY_RUN (write the message locally, post nothing) or POST. Default is DRY_RUN.
- **FR-26** At most one message is posted per run date. Re-running the same date updates the local record but does not post again unless an explicit force option is given.
- **FR-27** Posting failures are retried with a configured count; if posting still fails, the message is saved locally and the run is marked POST_FAILED.
- **FR-28** The workflow runs on a configured schedule and time zone (default nightly, Asia/Kolkata) and can also be run on demand for a given date, a subset of clusters, or in DRY_RUN.
- **FR-29** The run exits with a status code that distinguishes: success, success with fallback, partial (some clusters unreachable), post failed, and configuration error.

### 3.7 Audit and observability

- **FR-30** Every run has a complete, append-only structured record: inventory snapshot, matrix version, raw fetch results per cluster (with timing and errors), normalised versions, all findings with change tags, every LLM call (step, model, prompt version, input, output, tokens, latency), every gate result with the specific errors, which message was posted, and the posting result.
- **FR-31** All records share a run ID and step IDs so that a tracing tool (e.g., LangSmith) can be added later without changing business logic. No tracing vendor is used now.
- **FR-32** Credentials, tokens and API server endpoints never appear in logs or run records.
- **FR-33** Findings history is kept for a configured number of days and can be queried per cluster.

---

## 4. Mock services

Clusters and the team channel are mocks that run locally. They must behave like separate services reached over a network interface, not functions called directly, so they can be swapped for real ones by changing configuration only.

- **MS-1 Mock cluster API**: one endpoint set per mocked cluster that returns the control-plane version and the node list in the same shape a real Kubernetes API returns them (version info; nodes with labels for node pool, kubelet version and ready condition).
- **MS-2 Provider-style version strings**: EKS-style and GKE-style suffixes must appear in mock data so normalisation is exercised.
- **MS-3 Mock team channel**: accepts posted messages the way a chat webhook does and stores them locally for inspection and evals.
- **MS-4** All mocks load from seed files, can be reset to seed state, and record every call they receive, so evals can assert what was and wasn't called (e.g., that no write call was ever made to a cluster).
- **MS-5 Fault injection**, configured per cluster and per service: error rate, added latency, timeouts, auth failures, malformed responses, and scripted failures (e.g., "cluster X times out on the first two attempts").
- **MS-6 Scenario time travel**: seed data can be advanced to a later "night" to simulate upgrades, regressions and resolutions across consecutive runs.

### 4.1 Seed data

- **SD-1** 80 clusters across eks, gke and self-managed, and across prod, staging and dev, with realistic names and owning teams.
- **SD-2** A supported-version matrix with at least five minor versions per provider, covering supported, extended support and end-of-life states. The seed matrix is illustrative test data, not an authoritative statement of any provider's real support dates, and must say so in the file.
- **SD-3** Every rule in FR-11 is triggered by at least three clusters, and at least 30 clusters are fully compliant.
- **SD-4** Date boundaries are exercised on both sides for every date rule (e.g., end of support at warning window −1, exactly at the window, +1 day; end of life yesterday, today, tomorrow).
- **SD-5** Skew boundaries are exercised: node pools at exactly the allowed skew, one beyond it, and one minor newer than the control plane.
- **SD-6** Clusters with multiple node pools where only one pool drifts, a node pool mid-upgrade with mixed versions, a cluster with zero nodes, and a cluster with several hundred nodes.
- **SD-7** At least five scripted multi-night sequences exercising NEW, ONGOING, WORSENED, RESOLVED, NEW_CLUSTER and "was reachable, now unreachable".
- **SD-8** Cluster names that look alike (e.g., `payments-prod-1` and `payments-prod-11`) and version strings that look alike (`1.3` vs `1.30`), to test the faithfulness gate.

---

## 5. Configuration

- **CF-1** Everything that can change without changing behaviour lives in configuration, not code. This includes at least: LLM provider settings, per-step model settings, prompt file paths, inventory source, matrix source, skew policy, severity per rule and environment, warning windows, patch-behind threshold, WORSENED thresholds, concurrency, timeouts, retries, time budget, schedule and time zone, output mode, channel settings, message length limit, mention rules, deny-list, all-clear behaviour, retention, mock endpoints and fault injection.
- **CF-2** Configuration is layered: defaults, then an environment-specific file, then environment variables. Effective configuration (with secrets masked) is logged at startup.
- **CF-3** Secrets (API keys, cluster credentials, channel webhook URLs) come from environment variables only. Configuration files hold only the name of the variable to read.
- **CF-4** Configuration is validated at startup, including FR-13. Invalid configuration stops the run with a clear error before any cluster is contacted.
- **CF-5** Switching between mock clusters and real clusters, or between the mock channel and a real channel, is a configuration change only.

### 5.1 LLM provider

- **LL-1** The system talks to LLMs through an OpenAI-compatible chat completions interface, configured by base URL, model name, API-key environment variable, temperature, max tokens, timeout and retries.
- **LL-2** Default configuration points to the local model (currently a Qwen 27B-class model served locally). Switching to another local server, OpenRouter, or the Anthropic API must be a configuration change only; any provider that isn't OpenAI-compatible is supported through a configured provider type, not code edits at call sites.
- **LL-3** The system must work with models that are unreliable at structured output. Malformed output is a gate failure (CH-6), never something to guess around. Where the provider supports a structured-output or JSON mode, it is used if enabled in configuration.
- **LL-4** Any "thinking" or reasoning text a model emits is stripped before gates run and is never posted.
- **LL-5** Token usage and latency are recorded per call; cost is estimated from configured per-model prices (zero for local).

---

## 6. Evals

Evals are a first-class deliverable, runnable with one command, producing a report that can be compared across runs, models and prompt versions.

### 6.1 Hard gates (must be 100%, or the eval run fails)

- **EV-1** Drift detection: every seeded scenario produces exactly the expected findings (rule, severity, cluster, subject, values). No LLM involved.
- **EV-2** Change tagging: every multi-night sequence produces the expected NEW/ONGOING/WORSENED/RESOLVED/NEW_CLUSTER tags.
- **EV-3** No posted message (LLM or fallback) contains a fact not in the findings, across all scenarios.
- **EV-4** Every CRITICAL finding and every prod UNREACHABLE cluster appears in every posted message.
- **EV-5** No write call is ever made to any mock cluster.
- **EV-6** No secret, endpoint, or deny-listed string appears in any message or log.
- **EV-7** A message is posted for every run when the channel is available, including runs where the LLM is down or every chain step fails.
- **EV-8** No run date is posted twice without the force option.

### 6.2 Gate effectiveness

- **EV-9** A set of at least 40 hand-written bad drafts (wrong version, invented cluster, wrong count, look-alike names, missing critical cluster, over length, leaked endpoint) must each be rejected by Gate 2. A set of at least 20 good drafts must each pass. Report false rejects and false accepts.
- **EV-10** A set of bad Step 1 outputs (invented IDs, duplicated IDs, missing critical, too many themes, invalid schema) must each be rejected by Gate 1.

### 6.3 Chain quality (per model and prompt version)

- **EV-11** Per step: first-attempt gate pass rate, pass rate after retries, fallback rate, and latency.
- **EV-12** Summary quality via LLM-as-judge with a written rubric: is the most important thing first, is it understandable in under 30 seconds, are themes sensible, is the tone neutral, are next actions clear. The judge model is configured separately from the models under test. Judge scores are reported alongside, never instead of, the hard gates.
- **EV-13** A small human-labelled set (at least 15 runs) scoring summaries 1–5, used to check the judge agrees with a human.
- **EV-14** Length distribution of posted messages against the limit.

### 6.4 Eval operation

- **EV-15** Replay mode: recorded LLM outputs are reused so code and gate changes can be evaluated without calling the model. Live mode calls the configured model.
- **EV-16** Model comparison: the same scenarios run against two or more configured models produce a side-by-side report.
- **EV-17** Evals run against the mocks with fixed seeds and a fixed "now", and are repeatable.

---

## 7. Delivery expectations

- **DE-1** Before writing code, propose the tech stack, project layout and architecture, and wait for approval.
- **DE-2** Build in small milestones, each ending with working, tested code. Suggested order: config and mocks → collection → deterministic drift rules with EV-1 → change tagging → fallback message and posting → prompt chain with gates → evals and reports.
- **DE-3** Keep a short README covering: starting the mocks, running a night on demand, advancing scenario time, running evals, and switching LLM provider.
- **DE-4** When a requirement is unclear, ask rather than assume, and record the decision in a decisions log.

---

## 8. Out of scope (for now)

Real clusters and real cloud credentials, the real team channel, automatic upgrades or any remediation, fetching the matrix from provider websites, add-on and Helm chart version drift, OS/kernel/container-runtime drift, a web UI, an LLM gateway, a tracing vendor (LangSmith later), production deployment and scheduling infrastructure.
