"""Choosing how to sample a probe on a large table (spec 8.6, owner decision 2026-11-24).

* Small tables, and every table with ``measurement="off"``, are measured exactly.
* Freshness and row counts are never sampled.
* Key and join metrics use key-hash sampling, only when the key's distinct count exceeds
  ``exact_key_limit``; the fraction targets ``exact_key_limit`` sampled keys and the same
  fraction and seed are used on both sides of a join.
* Column profiles and pattern rates (single-read FactLang primitives) use block (``system``)
  sampling, or row-level ``bernoulli`` where the backend has no block sampling, at
  ``max_bytes_per_probe / est_bytes`` clamped to the configured floor and ceiling.
* Top values (FactLang ``ValueMembership``) and joins (``KeyOverlap``) read more than once, so
  FactLang refuses ``system``/``bernoulli`` for them (FL405). Top values use a deterministic row
  sample instead: ``keyhash`` on the table's primary key at the block fraction above. With no
  known primary key they are measured exactly (the per-probe budget then refuses an
  over-budget probe with SG301).
* ``measurement="force"`` samples every table: key requests by key hash (the fraction above,
  or the floor when the key is small), everything else at the floor.

``LIMIT`` is never a sampling method.
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_CEILING, Decimal

from schemaguru.config import SamplingConfig
from schemaguru.measure.requests import (
    NEVER_SAMPLED,
    KeyOverlapReq,
    PatternRate,
    ProfileColumns,
    Request,
    SampleChoice,
    SampleMethod,
    TopValues,
    Uniqueness,
)
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Table

# keyhash buckets are 1e-6 wide (FactLang B2); every fraction is a whole number of buckets
QUANTUM = Decimal("0.000001")


SEED_MAX = 2**31 - 1  # inside every FactLang dialect's sample_seed_range


def seed_for(table: ObjectRef, snapshot_id: str) -> int:
    """Deterministic per (table, snapshot), in ``[0, SEED_MAX]``: sha256 folded into the range
    every FactLang dialect accepts (Snowflake/Databricks: 31 bits; DuckDB 1.5 cannot parse a
    64-bit seed). The fact, its evidence and the executed sample all carry this one seed."""
    digest = hashlib.sha256(f"{table.table_ref().canonical()}|{snapshot_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (SEED_MAX + 1)


def is_large(table: Table, cfg: SamplingConfig) -> bool:
    """Catalog bytes decide when known; otherwise the row count; nothing known is not large."""
    if table.bytes is not None:
        return table.bytes >= cfg.sample_threshold_bytes
    if table.row_count is not None:
        return table.row_count >= cfg.sample_threshold_rows
    return False


def _fraction(value: Decimal) -> Decimal:
    q = value.quantize(QUANTUM, rounding=ROUND_CEILING)
    return min(Decimal(1), max(QUANTUM, q)).normalize()


def _key_columns(req: Request) -> tuple[str, ...] | None:
    if isinstance(req, KeyOverlapReq):
        return tuple(c.column for c in req.join.left if c.column is not None)
    if isinstance(req, Uniqueness):
        return req.columns
    return None


def choose_sample(
    table: Table,
    req: Request,
    cfg: SamplingConfig,
    *,
    key_distinct: int | None,
    est_bytes: int | None,
    max_bytes_per_probe: int | None,
    seed: int,
    block_sampling: bool = True,
    pk: tuple[str, ...] | None = None,
) -> SampleChoice | None:
    """``None`` means measure exactly. ``key_distinct`` unknown falls back to the catalog row
    count, an upper bound on the number of keys (a smaller fraction, never a bigger one).
    ``pk`` is the table's known primary key, the only way top values can be sampled."""
    if isinstance(req, NEVER_SAMPLED) or cfg.measurement == "off":
        return None
    force = cfg.measurement == "force"
    if not force and not is_large(table, cfg):
        return None
    floor = Decimal(str(cfg.block_fraction_floor))
    key = _key_columns(req)
    if key is not None:
        keys = key_distinct if key_distinct is not None else table.row_count
        if keys is not None and keys > cfg.exact_key_limit:
            return SampleChoice(
                "keyhash", _fraction(Decimal(cfg.exact_key_limit) / Decimal(keys)), seed, key
            )
        if force:
            return SampleChoice("keyhash", _fraction(floor), seed, key)
        return None  # a small key is cheap to measure exactly (only key columns are read)
    if isinstance(req, TopValues):
        if not pk:
            return None  # no deterministic row key: exact, or refused over budget (SG301)
        return SampleChoice("keyhash", _row_fraction(cfg, est_bytes, max_bytes_per_probe), seed, pk)
    if not isinstance(req, ProfileColumns | PatternRate):
        return None  # only single-read primitives may take a block or row sample (FL405)
    method: SampleMethod = "system" if block_sampling else "bernoulli"
    return SampleChoice(method, _row_fraction(cfg, est_bytes, max_bytes_per_probe), seed)


def _row_fraction(
    cfg: SamplingConfig, est_bytes: int | None, max_bytes_per_probe: int | None
) -> Decimal:
    floor = Decimal(str(cfg.block_fraction_floor))
    if cfg.measurement == "force":
        return _fraction(floor)
    ceiling = Decimal(str(cfg.block_fraction_ceiling))
    if max_bytes_per_probe is None:
        frac = ceiling
    elif est_bytes is None or est_bytes <= 0:
        frac = floor  # unknown cost: the cheapest representative sample
    else:
        frac = min(ceiling, max(floor, Decimal(max_bytes_per_probe) / Decimal(est_bytes)))
    return _fraction(frac)
