-- SchemaGuru Postgres store, schema version 3: a counter of inserted JOIN facts (the SQLite
-- series' version 4). A sequence, so concurrent writers never wait on one counter row.
--
-- A reader that keeps a per-table index of JOIN subjects (the context renderer) compares this
-- counter, an O(1) read, to know whether a JOIN subject it has not indexed may exist, also when
-- another process wrote it. Only an insert can add a subject.
CREATE SEQUENCE join_facts_version;
CREATE FUNCTION bump_join_facts_version() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN PERFORM nextval(quote_ident(TG_TABLE_SCHEMA) || ''.join_facts_version''); RETURN NULL; END;';
CREATE TRIGGER facts_join_inserted AFTER INSERT ON facts FOR EACH ROW WHEN (NEW.kind = 'JOIN') EXECUTE FUNCTION bump_join_facts_version();
