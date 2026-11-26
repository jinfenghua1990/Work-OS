-- Schema 45 -> 46: append-only CoffeeBear legacy source-row snapshots.
-- These are copies of rows as stored in the old CoffeeBear database; they are
-- not replacements for signed PDF/OFD/Excel/CSV originals. Apply only after an
-- authorized schema 45 backup and a tested migration rehearsal.
BEGIN;
DO $workos$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version=45)
       OR EXISTS (SELECT 1 FROM public.schema_migrations WHERE version>45)
    THEN RAISE EXCEPTION 'schema 45 required before upgrading to 46'; END IF;
END $workos$;

CREATE SCHEMA IF NOT EXISTS evidence;
CREATE TABLE IF NOT EXISTS evidence.legacy_source_records (
    source_system TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_pk TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    raw_json_text TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (source_system,source_table,source_pk,snapshot_sha256),
    CHECK(length(snapshot_sha256)=64)
);
CREATE OR REPLACE TRIGGER imm_legacy_source_records
    BEFORE UPDATE OR DELETE ON evidence.legacy_source_records
    FOR EACH ROW EXECUTE FUNCTION evidence.forbid_original_mutation();

INSERT INTO schema_migrations(version,applied_at)
VALUES(46,CURRENT_TIMESTAMP::text)
ON CONFLICT(version) DO NOTHING;
COMMIT;
