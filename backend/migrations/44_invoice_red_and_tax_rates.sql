-- Work OS schema 43 -> 44
-- First verify a current database backup and that schema_migrations MAX(version)=43.
-- Run on an isolated Neon branch and reconcile before changing production.
-- Does not alter invoices, close history, existing journal entries or tax statuses.
BEGIN;
DO $workos$ BEGIN
    IF COALESCE((SELECT MAX(version) FROM public.schema_migrations),0) <> 43
    THEN RAISE EXCEPTION 'schema 43 required before upgrading to 44'; END IF;
END $workos$;

CREATE TABLE IF NOT EXISTS invoice_credit_notes (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    blue_invoice_id TEXT NOT NULL REFERENCES invoices(id),
    invoice_number TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('sales','purchase')),
    counterparty TEXT NOT NULL,
    issued_on TEXT NOT NULL,
    total_amount TEXT NOT NULL,
    tax_amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('return','discount','reissue','correction')),
    archive_file_id TEXT NOT NULL REFERENCES archive_files(id),
    created_at TEXT NOT NULL,
    UNIQUE(direction,counterparty,invoice_number)
);

CREATE INDEX IF NOT EXISTS ix_invoice_credit_blue
    ON invoice_credit_notes(blue_invoice_id,issued_on);

CREATE TABLE IF NOT EXISTS tax_rate_rules (
    rate_code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    rate_percent TEXT NOT NULL,
    description TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(rate_code,valid_from),
    CHECK(valid_to IS NULL OR valid_to >= valid_from)
);

INSERT INTO schema_migrations(version,applied_at)
SELECT 44, CURRENT_TIMESTAMP::text
WHERE EXISTS (SELECT 1 FROM schema_migrations WHERE version=43)
ON CONFLICT(version) DO NOTHING;

COMMIT;
