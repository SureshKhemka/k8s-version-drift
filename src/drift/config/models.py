"""Typed configuration (CF-1). Every tunable lives here; code reads nothing else.

Validation in this module is structural and semantic (CF-4, FR-13). Checks that
touch the filesystem or environment live in ``loader.validate_runtime``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drift.domain.enums import Environment, OutputMode, Rule, Severity

# FR-13: floors that configuration can never go below.
SEVERITY_FLOORS: dict[Rule, tuple[Severity, tuple[Environment, ...] | None]] = {
    Rule.CP_EOL: (Severity.HIGH, None),  # None = every environment
    Rule.NODE_NEWER_THAN_CP: (Severity.HIGH, None),
    Rule.UNREACHABLE: (Severity.HIGH, (Environment.PROD,)),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- LLM


class ProviderConfig(_Strict):
    type: Literal["openai_compat", "anthropic"]
    base_url: str
    api_key_env: str | None = None  # CF-3: name of the env var, never the key
    json_mode: bool = False
    extra_headers: dict[str, str] = Field(default_factory=dict)


class StepModelConfig(_Strict):
    provider: str
    model: str
    temperature: float = Field(ge=0.0, le=2.0)
    max_tokens: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    retries: int = Field(ge=0)  # transport-level retries (distinct from gate retries)
    price_in_per_mtok: float = Field(default=0.0, ge=0.0)  # LL-5
    price_out_per_mtok: float = Field(default=0.0, ge=0.0)


class StepOverride(BaseModel):
    """Partial StepModelConfig; unset fields inherit from ``llm.default``."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: float | None = None
    retries: int | None = None
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None


StepName = Literal["group", "draft", "shorten", "judge"]


class LLMConfig(_Strict):
    enabled: bool = True
    providers: dict[str, ProviderConfig]
    default: StepModelConfig
    steps: dict[StepName, StepOverride] = Field(default_factory=dict)

    def for_step(self, step: StepName) -> StepModelConfig:
        override = self.steps.get(step)
        if override is None:
            return self.default
        merged = self.default.model_dump() | override.model_dump(exclude_none=True)
        return StepModelConfig(**merged)

    @model_validator(mode="after")
    def _providers_exist(self) -> LLMConfig:
        for step in ("group", "draft", "shorten", "judge"):
            cfg = self.for_step(step)  # also validates merged override values
            if cfg.provider not in self.providers:
                raise ValueError(f"llm step {step!r} uses unknown provider {cfg.provider!r}")
        return self


class ChainConfig(_Strict):
    max_themes: int = Field(ge=1, le=20)
    gate_retries: int = Field(ge=0, le=5)


class PromptsConfig(_Strict):
    dir: Path
    group: str
    draft: str
    shorten: str
    judge: str


# ------------------------------------------------------------------- sources


class SourceConfig(_Strict):
    type: Literal["file"]
    path: Path


class SourcesConfig(_Strict):
    inventory: SourceConfig
    matrix: SourceConfig


class ClustersConfig(_Strict):
    concurrency: int = Field(ge=1, le=256)
    timeout_s: float = Field(gt=0)
    retries: int = Field(ge=0, le=10)
    retry_backoff_s: float = Field(ge=0)
    time_budget_s: float = Field(gt=0)
    # CF-3: name of the env var holding a bearer token; None = no auth (mocks).
    token_env: str | None = None
    verify_tls: bool = True
    # CF-5: API server URL per cluster. ``{id}`` is replaced; an inventory ``api_server`` wins if present.
    endpoint_template: str | None = None
    node_pool_labels: tuple[str, ...]  # first label present on a node names its pool
    page_size: int = Field(default=100, ge=1, le=5000)


# -------------------------------------------------------------------- policy


class RuleSeverity(_Strict):
    default: Severity
    overrides: dict[Environment, Severity] = Field(default_factory=dict)

    @field_validator("default", mode="before")
    @classmethod
    def _parse_default(cls, v: object) -> Severity:
        return Severity.parse(v)  # type: ignore[arg-type]

    @field_validator("overrides", mode="before")
    @classmethod
    def _parse_overrides(cls, v: object) -> object:
        if isinstance(v, dict):
            return {k: Severity.parse(s) for k, s in v.items()}
        return v

    def for_env(self, env: Environment) -> Severity:
        return self.overrides.get(env, self.default)


class PolicyConfig(_Strict):
    skew_max_minor: int = Field(ge=0, le=5)
    warning_window_days: int = Field(ge=1)
    patch_behind_threshold: int = Field(ge=1)
    # FR-16: WORSENED when days_remaining crosses below any of these.
    worsened_days_thresholds: tuple[int, ...]
    severities: dict[Rule, RuleSeverity]

    @model_validator(mode="after")
    def _check(self) -> PolicyConfig:
        missing = set(Rule) - set(self.severities)
        if missing:
            raise ValueError(f"policy.severities missing rules: {sorted(missing)}")
        errors: list[str] = []
        for rule, (floor, envs) in SEVERITY_FLOORS.items():
            rs = self.severities[rule]
            for env in envs or tuple(Environment):
                if rs.for_env(env) < floor:
                    errors.append(f"{rule} in {env} is {rs.for_env(env).name}; FR-13 requires at least {floor.name}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def severity(self, rule: Rule, env: Environment) -> Severity:
        return self.severities[rule].for_env(env)


# --------------------------------------------------------------- run/output


class ScheduleConfig(_Strict):
    cron: str
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        if len(v.split()) != 5:
            raise ValueError(f"cron must have 5 fields, got {v!r}")
        return v


class OutputConfig(_Strict):
    mode: OutputMode = OutputMode.DRY_RUN  # FR-25
    outbox_dir: Path
    post_retries: int = Field(ge=0, le=10)
    post_retry_backoff_s: float = Field(ge=0)
    post_timeout_s: float = Field(gt=0)


class MentionsConfig(_Strict):
    on_critical: tuple[str, ...] = ()


class ChannelConfig(_Strict):
    type: Literal["slack_webhook"]
    webhook_url_env: str  # CF-3
    format: Literal["slack_mrkdwn", "plain"]
    max_length: int = Field(ge=200, le=40000)
    post_all_clear: bool
    mentions: MentionsConfig = MentionsConfig()


class StorageConfig(_Strict):
    db_path: Path
    runs_dir: Path
    retention_days: int = Field(ge=1)


class MockServiceConfig(_Strict):
    host: str
    port: int = Field(ge=1, le=65535)


class MocksConfig(_Strict):
    k8s: MockServiceConfig
    channel: MockServiceConfig
    seed_dir: Path
    state_dir: Path
    faults: Path
    timeout_sleep_s: float = Field(default=30.0, gt=0)


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class AppConfig(_Strict):
    environment: str
    llm: LLMConfig
    chain: ChainConfig
    prompts: PromptsConfig
    sources: SourcesConfig
    clusters: ClustersConfig
    policy: PolicyConfig
    schedule: ScheduleConfig
    output: OutputConfig
    channel: ChannelConfig
    deny_list: tuple[str, ...]
    storage: StorageConfig
    mocks: MocksConfig
    logging: LoggingConfig

    @field_validator("deny_list")
    @classmethod
    def _regexes_compile(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"deny_list pattern {pattern!r} is not a valid regex: {exc}") from exc
        return v
