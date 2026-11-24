"""Configuration (layer 2): frozen dataclasses with defaults from the spec."""

from __future__ import annotations

import dataclasses
import os
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml

from schemaguru.errors import ConfigError


def _check(name: str, value: object, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ConfigError(f"{name}: {value!r} is not one of {list(allowed)}")


@dataclass(frozen=True)
class Budget:
    max_bytes_per_probe: int | None = 1 << 30
    max_bytes_total: int | None = 10 << 30
    max_probes: int | None = None
    max_seconds: float | None = None
    crawl_bytes: int | None = None
    draft_budget_tokens: int | None = None
    # a guard shared across runs (the background drainer) refills its byte total once per
    # period; None = one total for the guard's lifetime
    period_s: float | None = 86400.0


@dataclass(frozen=True)
class SamplingConfig:
    llm_values: Literal["masked", "raw", "none"] = "masked"
    per_glob: Mapping[str, str] = field(default_factory=dict)
    sample_values_per_column: int = 5
    store_raw_samples: bool = False
    measurement: Literal["auto", "off", "force"] = "auto"
    sample_threshold_bytes: int = 50 << 30
    sample_threshold_rows: int = 500_000_000
    exact_key_limit: int = 5_000_000
    block_fraction_floor: float = 0.001
    block_fraction_ceiling: float = 0.10
    design_effect: float = 2.0

    def __post_init__(self) -> None:
        _check("sampling.llm_values", self.llm_values, ("masked", "raw", "none"))
        _check("sampling.measurement", self.measurement, ("auto", "off", "force"))


@dataclass(frozen=True)
class GraphConfig:
    w_support: float = 1.0
    w_fanout: float = 0.35
    w_hub: float = 0.25
    unmeasured_penalty: float = 1.0
    hop_limit: int = 6
    thin_support: float = 0.05
    attribution_fanin: float = 10.0
    k_paths: int = 3
    include_unmeasured: bool = False
    ok_threshold: float = 0.95
    weak_threshold: float = 0.05


@dataclass(frozen=True)
class ScopeConfig:
    seed_k: int = 8
    max_expansion_tables: int = 6
    max_values_shown: int = 6
    cache_entries: int = 256


@dataclass(frozen=True)
class CrawlConfig:
    page_size: int = 1000
    column_batch: int = 200
    workers: int = 4
    timeout_s: float = 60.0
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = (
        "information_schema.*",
        "pg_catalog.*",
        "sys.*",
        "*.*_staging*",
        "*.*tmp*",
    )


@dataclass(frozen=True)
class Config:
    budget: Budget = field(default_factory=Budget)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    enum_max_distinct: int = 20
    fact_ttl_days: int = 14
    measure_on_demand: Literal["queue", "background", "block"] = "queue"
    # block mode: the most a context() call may spend measuring its scoped tables, in seconds
    block_timeout_s: float = 10.0
    store_path: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    # token counter for view budgets: chars/4 heuristic, or tiktoken (extra ``tiktoken``)
    token_counter: Literal["chars", "tiktoken"] = "chars"
    # the agent tool ``check_fact`` runs claims on the warehouse. "deny" (default, spec 8.2: an
    # agent question never spends by itself) removes the tool and refuses calls; "allow"
    # admits each check through the Guru's one per-period budget guard, shared with the
    # background drainer
    agent_checks: Literal["allow", "deny"] = "deny"

    def __post_init__(self) -> None:
        _check("agent_checks", self.agent_checks, ("allow", "deny"))
        _check("measure_on_demand", self.measure_on_demand, ("queue", "background", "block"))
        _check("token_counter", self.token_counter, ("chars", "tiktoken"))

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> Config:
        return _build(cls, d, "")

    @classmethod
    def from_env(
        cls, prefix: str = "SCHEMAGURU_", *, environ: Mapping[str, str] | None = None
    ) -> Config:
        """``PREFIX_FIELD`` sets a top-level field, ``PREFIX_SECTION__FIELD`` a nested one.

        Values are parsed as YAML scalars (``7`` -> int, ``true`` -> bool, ``none`` stays text).
        """
        env = os.environ if environ is None else environ
        data: dict[str, Any] = {}
        for key, raw in env.items():
            if not key.startswith(prefix):
                continue
            path = key[len(prefix) :].lower().split("__")
            node = data
            for part in path[:-1]:
                node = node.setdefault(part, {})
            node[path[-1]] = yaml.safe_load(raw)
        try:
            return cls.from_mapping(data)
        except ConfigError as e:
            raise ConfigError(f"{e} (from environment prefix {prefix})") from None

    @classmethod
    def from_yaml(cls, path: str | Path) -> Config:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, Mapping):
            raise ConfigError(f"{path}: top level must be a mapping")
        return cls.from_mapping(loaded)


T = TypeVar("T")


def _build(cls: type[T], d: Mapping[str, Any], path: str) -> T:
    fields = {f.name: f for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for key, value in d.items():
        if key not in fields:
            raise ConfigError(f"unknown config key: {path}{key}")
        sub = _SECTIONS.get((cls.__name__, key))
        if sub is not None:
            if not isinstance(value, Mapping):
                raise ConfigError(f"{path}{key}: expected a mapping")
            kwargs[key] = _build(sub, value, f"{path}{key}.")
        else:
            kwargs[key] = _coerce(f"{path}{key}", value, hints.get(key))
    return cls(**kwargs)


def _coerce(name: str, value: Any, hint: Any) -> Any:
    """``value`` as the field's declared type; text (from the environment) is parsed.

    Tuple fields take a list or a comma-separated string; int/float/bool fields take text
    (``"7"``, ``"0.5"``, ``"true"``) and reject anything else with ``ConfigError``. An optional
    field reads ``none``/``null``/empty text as ``None``. Other types pass through unchanged.
    """
    args = typing.get_args(hint)
    optional = type(None) in args
    if optional and (
        value is None or (isinstance(value, str) and value.strip().lower() in ("", "none", "null"))
    ):
        return None
    base = next((a for a in args if a is not type(None)), hint) if optional else hint
    if base is tuple or typing.get_origin(base) is tuple:
        if isinstance(value, str):
            return tuple(p.strip() for p in value.split(",") if p.strip())
        if isinstance(value, list | tuple):
            return tuple(value)
        if value is None:
            return ()
        raise ConfigError(f"{name}: expected a list or comma-separated text, got {value!r}")
    if base is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
            return value.strip().lower() in ("true", "yes")
        raise ConfigError(f"{name}: expected true or false, got {value!r}")
    if base is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip().replace("_", ""))
            except ValueError:
                pass
        raise ConfigError(f"{name}: expected an integer, got {value!r}")
    if base is float:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ConfigError(f"{name}: expected a number, got {value!r}")
    return value


_SECTIONS: dict[tuple[str, str], type] = {
    ("Config", "budget"): Budget,
    ("Config", "sampling"): SamplingConfig,
    ("Config", "graph"): GraphConfig,
    ("Config", "scope"): ScopeConfig,
    ("Config", "crawl"): CrawlConfig,
}
