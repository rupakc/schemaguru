-- SchemaGuru store, schema version 1.
-- JSON payloads are the model objects' to_dict() output (compact, sorted keys).

CREATE TABLE schemas (
    key  TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE tables (
    ref          TEXT PRIMARY KEY,        -- ObjectRef.canonical(), exact
    ref_lower    TEXT NOT NULL,           -- for case-insensitive resolution
    data         TEXT NOT NULL,           -- Table.to_dict() without withdrawn_at
    withdrawn_at TEXT                     -- fixed-width UTC, NULL = live
);
CREATE INDEX tables_ref_lower ON tables(ref_lower);

CREATE TABLE columns (
    table_ref TEXT NOT NULL,
    name      TEXT NOT NULL,
    ordinal   INTEGER NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (table_ref, name)
) WITHOUT ROWID;

CREATE TABLE constraints (
    table_ref TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (table_ref, seq)
) WITHOUT ROWID;

CREATE TABLE facts (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    subject_key TEXT NOT NULL,            -- ObjectRef.canonical() or JoinRef.identity_key()
    data        TEXT NOT NULL
);
CREATE INDEX facts_subject ON facts(subject_key);
CREATE INDEX facts_kind ON facts(kind);

-- LIKE-search fallback source when FTS5 is unavailable (table_docs is created at open).
CREATE TABLE docs (
    ref         TEXT PRIMARY KEY,
    name_tokens TEXT NOT NULL,
    columns     TEXT NOT NULL,
    text        TEXT NOT NULL
);

CREATE TABLE snapshots (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    label      TEXT,
    created_at TEXT NOT NULL,
    checks_run TEXT NOT NULL
);

CREATE TABLE snapshot_facts (
    snapshot_id TEXT NOT NULL,
    fact_id     TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, fact_id)
) WITHOUT ROWID;

CREATE TABLE crawl_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE ledger (
    id     INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL,
    unit   TEXT NOT NULL,
    amount INTEGER NOT NULL,
    note   TEXT NOT NULL,
    at     TEXT NOT NULL
);
CREATE INDEX ledger_kind ON ledger(kind, unit, at);

CREATE TABLE queue (
    table_ref TEXT NOT NULL,
    reason    TEXT NOT NULL,
    priority  REAL NOT NULL,
    seq       INTEGER NOT NULL,
    PRIMARY KEY (table_ref, reason)
) WITHOUT ROWID;
CREATE INDEX queue_order ON queue(priority DESC, seq);

CREATE TABLE feedback (
    id            INTEGER PRIMARY KEY,
    table_ref     TEXT NOT NULL,
    column_name   TEXT,
    failure_class TEXT NOT NULL,
    at            TEXT NOT NULL
);
