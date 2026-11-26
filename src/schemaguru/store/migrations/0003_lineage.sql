-- SchemaGuru store, schema version 3: the lineage index (0.2.0 spec 4.5).

-- EFFECTIVE lineage only (the human-wins resolution of LINEAGE / COLUMN_LINEAGE facts), one row
-- per (input, fact) of every effective link. The store recomputes a subject's rows in the same
-- transaction as any write to a lineage fact of that subject, so downstream questions are index
-- lookups here, never a scan of the facts.
CREATE TABLE lineage_edges (
    warehouse      TEXT NOT NULL DEFAULT '',   -- reserved: one store holds one warehouse in 0.2.0
    upstream_key   TEXT NOT NULL,
    downstream_key TEXT NOT NULL,
    level          TEXT NOT NULL CHECK (level IN ('table', 'column')),
    fact_id        TEXT NOT NULL,
    PRIMARY KEY (warehouse, downstream_key, level, upstream_key, fact_id)
) WITHOUT ROWID;
CREATE INDEX lineage_edges_upstream ON lineage_edges(upstream_key, level);
CREATE INDEX lineage_edges_downstream ON lineage_edges(downstream_key, level);

-- The sha256 of the view definition each table's lineage was last parsed from.
CREATE TABLE lineage_state (
    table_ref         TEXT PRIMARY KEY,
    definition_sha256 TEXT NOT NULL
) WITHOUT ROWID;
