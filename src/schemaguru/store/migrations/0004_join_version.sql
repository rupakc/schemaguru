-- SchemaGuru store, schema version 4: a counter of inserted JOIN facts (0.2.0 spec 6).
--
-- A reader that keeps a per-table index of JOIN subjects (the context renderer) compares this
-- counter, an O(1) read, to know whether a JOIN subject it has not indexed may exist, also when
-- another process wrote it. Only an insert can add a subject: an upsert of an existing fact id
-- keeps its subject, and a delete only removes one (a read by subject then finds nothing).
CREATE TABLE store_counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT INTO store_counters(name, value) VALUES ('join_facts', 0);
CREATE TRIGGER facts_join_inserted AFTER INSERT ON facts WHEN NEW.kind = 'JOIN'
BEGIN
    UPDATE store_counters SET value = value + 1 WHERE name = 'join_facts';
END;
