-- SchemaGuru Postgres store, schema version 2: the lineage index (the SQLite series' version 3).

-- EFFECTIVE lineage only (the human-wins resolution of LINEAGE / COLUMN_LINEAGE facts), one row
-- per (input, fact) of every effective link. The store recomputes a subject's rows in the same
-- transaction as any write to a lineage fact of that subject, so downstream questions are index
-- lookups here, never a scan of the facts.
CREATE TABLE lineage_edges (
    warehouse      TEXT COLLATE "C" NOT NULL DEFAULT '',   -- reserved: one warehouse per store
    upstream_key   TEXT COLLATE "C" NOT NULL,
    downstream_key TEXT COLLATE "C" NOT NULL,
    level          TEXT COLLATE "C" NOT NULL CHECK (level IN ('table', 'column')),
    fact_id        TEXT COLLATE "C" NOT NULL,
    PRIMARY KEY (warehouse, downstream_key, level, upstream_key, fact_id)
);
CREATE INDEX lineage_edges_upstream ON lineage_edges(upstream_key, level);
CREATE INDEX lineage_edges_downstream ON lineage_edges(downstream_key, level);

-- The sha256 of the view definition each table's lineage was last parsed from.
CREATE TABLE lineage_state (
    table_ref         TEXT COLLATE "C" PRIMARY KEY,
    definition_sha256 TEXT NOT NULL
);
