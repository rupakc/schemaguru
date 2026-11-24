from pathlib import Path

import pytest

from schemaguru.config import Budget, Config, SamplingConfig
from schemaguru.errors import ConfigError, SchemaGuruError


def test_defaults() -> None:
    c = Config()
    assert c.budget.max_bytes_per_probe == 1 << 30
    assert c.sampling.llm_values == "masked"
    assert c.graph.ok_threshold == 0.95 and c.crawl.workers == 4
    assert c.llm_model == "claude-sonnet-5-5" and c.measure_on_demand == "queue"
    assert "*.*tmp*" in c.crawl.exclude


def test_config_unknown_key() -> None:
    with pytest.raises(ConfigError, match="bogus"):
        Config.from_mapping({"bogus": 1})
    with pytest.raises(ConfigError, match=r"budget\.nope"):
        Config.from_mapping({"budget": {"nope": 1}})
    assert issubclass(ConfigError, SchemaGuruError)


def test_from_mapping_nested() -> None:
    c = Config.from_mapping(
        {"budget": {"max_probes": 5}, "crawl": {"include": ["a.*"]}, "fact_ttl_days": 3}
    )
    assert c.budget == Budget(max_probes=5) and c.crawl.include == ("a.*",)
    assert c.fact_ttl_days == 3


def test_bad_literal() -> None:
    with pytest.raises(ConfigError, match="llm_values"):
        SamplingConfig(llm_values="all")  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        Config.from_mapping({"measure_on_demand": "maybe"})


def test_from_env() -> None:
    env = {
        "SCHEMAGURU_ENUM_MAX_DISTINCT": "7",
        "SCHEMAGURU_BUDGET__MAX_PROBES": "12",
        "SCHEMAGURU_SAMPLING__LLM_VALUES": "none",
        "SCHEMAGURU_STORE_PATH": "/tmp/x.db",
        "OTHER": "1",
    }
    c = Config.from_env(environ=env)
    assert c.enum_max_distinct == 7 and c.budget.max_probes == 12
    assert c.sampling.llm_values == "none" and c.store_path == "/tmp/x.db"
    with pytest.raises(ConfigError, match="nope"):
        Config.from_env(environ={"SCHEMAGURU_NOPE": "1"})


def test_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("scope:\n  seed_k: 3\ngraph:\n  hop_limit: 4\n", encoding="utf-8")
    c = Config.from_yaml(p)
    assert c.scope.seed_k == 3 and c.graph.hop_limit == 4
    p.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.from_yaml(p)


def test_budget_period() -> None:
    assert Budget().period_s == 86400.0
    assert Config.from_mapping({"budget": {"period_s": None}}).budget.period_s is None
    assert Config.from_mapping({"budget": {"period_s": 3600}}).budget.period_s == 3600


def test_from_env_coerces_tuple_and_scalar_fields() -> None:
    env = {
        "SCHEMAGURU_CRAWL__INCLUDE": "shop.*, sales.*",
        "SCHEMAGURU_CRAWL__EXCLUDE": "",
        "SCHEMAGURU_CRAWL__TIMEOUT_S": "5",
        "SCHEMAGURU_SCOPE__SEED_K": "9",
        "SCHEMAGURU_BUDGET__MAX_PROBES": "none",
        "SCHEMAGURU_GRAPH__INCLUDE_UNMEASURED": "yes",
        "SCHEMAGURU_SAMPLING__STORE_RAW_SAMPLES": "false",
    }
    c = Config.from_env(environ=env)
    assert c.crawl.include == ("shop.*", "sales.*")
    assert c.crawl.exclude == ()
    assert c.crawl.timeout_s == 5.0 and isinstance(c.crawl.timeout_s, float)
    assert c.scope.seed_k == 9 and c.budget.max_probes is None
    assert c.graph.include_unmeasured is True and c.sampling.store_raw_samples is False


@pytest.mark.parametrize(
    "key",
    [
        "SCHEMAGURU_SCOPE__SEED_K",
        "SCHEMAGURU_CRAWL__TIMEOUT_S",
        "SCHEMAGURU_GRAPH__INCLUDE_UNMEASURED",
        "SCHEMAGURU_BUDGET__MAX_PROBES",
    ],
)
def test_from_env_bad_value_is_config_error(key: str) -> None:
    with pytest.raises(ConfigError, match="expected"):
        Config.from_env(environ={key: "banana"})


def test_from_mapping_tuple_rejects_a_scalar_number() -> None:
    with pytest.raises(ConfigError):
        Config.from_mapping({"crawl": {"include": 5}})
