# Decisions log (DE-4)

Each entry: date, requirement(s), decision, reason. Newest last.

| # | Date | Req | Decision | Reason |
|---|---|---|---|---|
| D1 | 2026-09-29 | DE-1 | Python 3.12 + uv, Pydantic v2, httpx, FastAPI mocks, SQLite, Typer, Jinja2 prompts, pytest. No LangChain. | Approved stack proposal. Plain code keeps the chain small and inspectable. |
| D2 | 2026-09-29 | LL-2 | Default LLM: LM Studio `qwen/qwen3.8-27b` at `127.0.0.1:1234/v1`; judge `google/gemma-4-31b`. | Local model already served; judge model differs from the model under test (EV-12). |
| D3 | 2026-09-29 | FR-11 | Control-plane lifecycle rules are exclusive per cluster: CP_EOL > CP_UNSUPPORTED > CP_EOL_SOON; only the most severe is emitted. CP_EOL_SOON uses the earliest upcoming of end-of-support / end-of-life. | Avoid reporting the same control plane three times. |
| D4 | 2026-09-29 | FR-11 | A mixed node pool emits NODE_POOL_MIXED; skew and EOL rules evaluate the pool's *oldest* minor. "Nodes affected" counts only nodes on the drifting minor. | Worst case drives action; counts stay exact. |
| D5 | 2026-09-29 | SD-6 | A cluster with zero nodes gets control-plane rules only; no finding for "no nodes". | Not a drift rule in the spec. |
| D6 | 2026-09-29 | FR-16 | "Last successful run" = last run that completed drift detection, even if posting failed or fallback was used. | Change tags describe the fleet, not the channel. |
| D7 | 2026-09-29 | FR-12 | UNREACHABLE: HIGH by default (prod/staging), MEDIUM in dev. | Prod floor is FR-13; staging still matters. |
| D8 | 2026-09-29 | FR-24 | Channel = Slack incoming-webhook JSON `{"text": ...}`, Slack mrkdwn, 3000-char limit. | Most common chat webhook shape. |
| D9 | 2026-09-29 | CH-4 | Gate 2 ignores list markers and digits inside allowed versions/dates; every other number must equal a count or days-remaining value in the fact sheet. | Makes "wrong count" detectable without false rejects on formatting. |
| D10 | 2026-09-29 | FR-2 | NotReady nodes are recorded and counted in run stats but are not a drift rule. | Not in FR-11. |
| D11 | 2026-09-29 | CF-2 | Env-var layer uses `DRIFT__SECTION__KEY=value` (value parsed as YAML); layers deep-merge, so an env file overriding one per-environment severity keeps the others. | Predictable, testable layering. |
| D12 | 2026-09-29 | FR-12/13 | Default severities: see `config/defaults.yaml` `policy.severities`. FR-13 floors are hard-coded in `drift.config.models.SEVERITY_FLOORS`, not configurable. | Floors are invariants, not settings. |
| D13 | 2026-09-29 | CF-4 | Prompt files are only required when `llm.enabled` is true. | Fallback-only runs must still work. |
