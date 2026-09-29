from pathlib import Path

import pytest
import yaml

from drift.config import ConfigError, load_config, masked_dump, validate_runtime
from drift.domain.enums import Environment, OutputMode, Rule, Severity

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / "config"


def _cfg_dir(tmp_path: Path, overlay: dict | None = None, env: str = "local") -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "defaults.yaml").write_text((CONFIG_DIR / "defaults.yaml").read_text())
    if overlay is not None:
        (d / f"{env}.yaml").write_text(yaml.safe_dump(overlay))
    return d


def test_defaults_load_and_are_dry_run():
    cfg = load_config(CONFIG_DIR, environ={})
    assert cfg.environment == "local"
    assert cfg.output.mode is OutputMode.DRY_RUN
    assert cfg.schedule.timezone == "Asia/Kolkata"
    assert set(cfg.policy.severities) == set(Rule)
    assert cfg.sources.inventory.path.is_absolute()


def test_severity_env_overrides():
    cfg = load_config(CONFIG_DIR, environ={})
    assert cfg.policy.severity(Rule.CP_EOL_SOON, Environment.PROD) is Severity.HIGH
    assert cfg.policy.severity(Rule.CP_EOL_SOON, Environment.DEV) is Severity.LOW
    assert cfg.policy.severity(Rule.CP_EOL_SOON, Environment.STAGING) is Severity.MEDIUM


def test_env_file_layer_overrides_defaults(tmp_path):
    d = _cfg_dir(tmp_path, {"clusters": {"concurrency": 3}}, env="staging")
    cfg = load_config(d, env="staging", environ={})
    assert cfg.clusters.concurrency == 3
    assert cfg.environment == "staging"


def test_env_var_layer_wins(tmp_path):
    d = _cfg_dir(tmp_path, {"clusters": {"concurrency": 3}})
    cfg = load_config(
        d,
        environ={
            "DRIFT__CLUSTERS__CONCURRENCY": "7",
            "DRIFT__OUTPUT__MODE": "POST",
            "DRIFT__POLICY__SEVERITIES__CP_PATCH_BEHIND__DEFAULT": "info",
            "DRIFT__LLM__STEPS__DRAFT__TEMPERATURE": "0.7",
        },
    )
    assert cfg.clusters.concurrency == 7
    assert cfg.output.mode is OutputMode.POST
    assert cfg.policy.severities[Rule.CP_PATCH_BEHIND].default is Severity.INFO
    assert cfg.llm.for_step("draft").temperature == 0.7
    assert cfg.llm.for_step("group").temperature == 0.2  # CH-7 inheritance


@pytest.mark.parametrize(
    "overlay",
    [
        {"policy": {"severities": {"CP_EOL": {"default": "MEDIUM"}}}},
        {"policy": {"severities": {"CP_EOL": {"default": "CRITICAL", "overrides": {"dev": "LOW"}}}}},
        {"policy": {"severities": {"NODE_NEWER_THAN_CP": {"default": "HIGH", "overrides": {"staging": "MEDIUM"}}}}},
        {"policy": {"severities": {"UNREACHABLE": {"default": "HIGH", "overrides": {"prod": "MEDIUM"}}}}},
        {"policy": {"severities": {"UNREACHABLE": {"default": "LOW"}}}},  # prod inherits LOW
    ],
)
def test_fr13_floors_rejected(tmp_path, overlay):
    with pytest.raises(ConfigError, match="FR-13"):
        load_config(_cfg_dir(tmp_path, overlay), environ={})


def test_fr13_allows_nonprod_unreachable_below_high(tmp_path):
    overlay = {"policy": {"severities": {"UNREACHABLE": {"default": "LOW", "overrides": {"prod": "CRITICAL"}}}}}
    cfg = load_config(_cfg_dir(tmp_path, overlay), environ={})
    assert cfg.policy.severity(Rule.UNREACHABLE, Environment.STAGING) is Severity.LOW
    assert cfg.policy.severity(Rule.UNREACHABLE, Environment.DEV) is Severity.MEDIUM  # deep-merged default


@pytest.mark.parametrize(
    "overlay,needle",
    [
        ({"policy": {"severities": {"CP_EOL": {"default": "URGENT"}}}}, "unknown severity"),
        ({"schedule": {"timezone": "Mars/Olympus"}}, "timezone"),
        ({"schedule": {"cron": "every night"}}, "cron"),
        ({"llm": {"default": {"provider": "nope"}}}, "unknown provider"),
        ({"clusters": {"concurrency": 0}}, "concurrency"),
        ({"deny_list": ["("]}, "regex"),
        ({"clusters": {"surprise": 1}}, "surprise"),
        ({"output": {"mode": "YOLO"}}, "output.mode"),
    ],
)
def test_invalid_config_rejected_with_clear_error(tmp_path, overlay, needle):
    with pytest.raises(ConfigError) as exc:
        load_config(_cfg_dir(tmp_path, overlay), environ={})
    assert needle in str(exc.value)


def test_missing_rule_severity_rejected(tmp_path):
    d = _cfg_dir(tmp_path)
    raw = yaml.safe_load((d / "defaults.yaml").read_text())
    del raw["policy"]["severities"]["NODE_EOL"]
    (d / "defaults.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="NODE_EOL"):
        load_config(d, environ={})


def test_validate_runtime_reports_all_problems(tmp_path):
    d = _cfg_dir(
        tmp_path, {"output": {"mode": "POST"}, "clusters": {"token_env": "K8S_TOKEN"}, "llm": {"enabled": True}}
    )
    cfg = load_config(d, environ={})
    with pytest.raises(ConfigError) as exc:
        validate_runtime(cfg, environ={})
    msg = str(exc.value)
    assert "sources.inventory" in msg
    assert "DRIFT_CHANNEL_WEBHOOK_URL" in msg
    assert "K8S_TOKEN" in msg
    assert "prompts.group" in msg


def test_masked_dump_hides_endpoints_and_secrets_but_keeps_env_var_names():
    cfg = load_config(CONFIG_DIR, environ={})
    dumped = masked_dump(cfg)
    assert dumped["llm"]["providers"]["lmstudio"]["base_url"] == "***"
    assert dumped["channel"]["webhook_url_env"] == "DRIFT_CHANNEL_WEBHOOK_URL"
    assert "127.0.0.1:1234" not in yaml.safe_dump(dumped)
    assert "openrouter.ai" not in yaml.safe_dump(dumped)
