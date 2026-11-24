-- SchemaGuru Postgres store, schema version 1 (the SQLite series' versions 1 + 2 in one step).
-- JSON payloads are the model objects' to_dict() output (compact, sorted keys), kept as TEXT so
-- they round-trip byte for byte. Keys use COLLATE "C": ORDER BY and range scans then follow
-- code-point order, exactly like SQLite and Python, whatever the database's default collation.
-- Timestamps are the same fixed-width UTC text as in SQLite, so comparisons are textual.

CREATE TABLE schemas (
    key  TEXT COLLATE "C" PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE tables (
    ref          TEXT COLLATE "C" PRIMARY KEY,   -- ObjectRef.canonical(), exact
    ref_lower    TEXT COLLATE "C" NOT NULL,      -- for case-insensitive resolution
    name_lower   TEXT COLLATE "C" NOT NULL,      -- bare table name, lowercased
    data         TEXT NOT NULL,                  -- Table.to_dict() without withdrawn_at
    withdrawn_at TEXT                            -- fixed-width UTC, NULL = live
);
CREATE INDEX tables_ref_lower ON tables(ref_lower);
CREATE INDEX tables_name_lower ON tables(name_lower);

CREATE TABLE columns (
    table_ref TEXT COLLATE "C" NOT NULL,
    name      TEXT COLLATE "C" NOT NULL,
    ordinal   INTEGER NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (table_ref, name)
);

CREATE TABLE constraints (
    table_ref TEXT COLLATE "C" NOT NULL,
    seq       INTEGER NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (table_ref, seq)
);

CREATE TABLE facts (
    id          TEXT COLLATE "C" PRIMARY KEY,
    kind        TEXT COLLATE "C" NOT NULL,
    subject_key TEXT COLLATE "C" NOT NULL,   -- ObjectRef.canonical() or JoinRef.identity_key()
    data        TEXT NOT NULL
);
CREATE INDEX facts_subject_kind ON facts(subject_key, kind);
CREATE INDEX facts_kind ON facts(kind);

-- One search document per table. `doc` is the weighted tsvector the store searches:
-- A = table-name tokens, B = column-name tokens, C = comments, descriptions, use cases.
CREATE TABLE docs (
    ref         TEXT COLLATE "C" PRIMARY KEY,
    name_tokens TEXT NOT NULL,
    columns     TEXT NOT NULL,
    text        TEXT NOT NULL,
    doc         TSVECTOR NOT NULL
);
CREATE INDEX docs_doc ON docs USING GIN (doc);

CREATE TABLE snapshots (
    seq        BIGSERIAL PRIMARY KEY,
    id         TEXT COLLATE "C" NOT NULL UNIQUE,
    label      TEXT,
    created_at TEXT NOT NULL,
    checks_run TEXT NOT NULL
);

CREATE TABLE snapshot_facts (
    snapshot_id TEXT COLLATE "C" NOT NULL,
    fact_id     TEXT COLLATE "C" NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, fact_id)
);

CREATE TABLE crawl_state (
    key   TEXT COLLATE "C" PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE ledger (
    id     BIGSERIAL PRIMARY KEY,
    kind   TEXT NOT NULL,
    unit   TEXT NOT NULL,
    amount BIGINT NOT NULL,
    note   TEXT NOT NULL,
    at     TEXT COLLATE "C" NOT NULL
);
CREATE INDEX ledger_kind ON ledger(kind, unit, at);

CREATE TABLE queue (
    table_ref TEXT COLLATE "C" NOT NULL,
    reason    TEXT COLLATE "C" NOT NULL,
    priority  DOUBLE PRECISION NOT NULL,
    seq       BIGSERIAL NOT NULL,
    PRIMARY KEY (table_ref, reason)
);
CREATE INDEX queue_order ON queue(priority DESC, seq);

CREATE TABLE feedback (
    id            BIGSERIAL PRIMARY KEY,
    table_ref     TEXT COLLATE "C" NOT NULL,
    column_name   TEXT COLLATE "C",
    failure_class TEXT COLLATE "C" NOT NULL,
    at            TEXT NOT NULL
);

-- Optional embedding vectors (extra `embeddings`): little-endian float32, one row per
-- (model, table), the same bytes SQLiteStore keeps. Similarity is computed by the retriever.
CREATE TABLE table_embeddings (
    model     TEXT COLLATE "C" NOT NULL,
    table_ref TEXT COLLATE "C" NOT NULL,
    doc_hash  TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    BYTEA NOT NULL,
    PRIMARY KEY (model, table_ref)
);
