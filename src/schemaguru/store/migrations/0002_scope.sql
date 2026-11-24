-- SchemaGuru store, schema version 2: support for scoping and retrieval (spec 12).

-- facts_for(subjects, kinds) filters both in SQL; one composite index serves it (and the
-- subject-only lookups, as its prefix), so the single-column index is redundant.
CREATE INDEX facts_subject_kind ON facts(subject_key, kind);
DROP INDEX IF EXISTS facts_subject;

-- Bare table names typed by agents ("orders") resolve through this column, case-insensitively.
ALTER TABLE tables ADD COLUMN name_lower TEXT;
UPDATE tables SET name_lower = lower(json_extract(data, '$.ref.table'));
CREATE INDEX tables_name_lower ON tables(name_lower);

-- Optional embedding vectors (extra `embeddings`): little-endian float32, one row per
-- (model, table). doc_hash says which table document the vector was computed from.
CREATE TABLE table_embeddings (
    model     TEXT NOT NULL,
    table_ref TEXT NOT NULL,
    doc_hash  TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    BLOB NOT NULL,
    PRIMARY KEY (model, table_ref)
) WITHOUT ROWID;
