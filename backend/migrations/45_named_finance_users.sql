-- Schema 44 -> 45: opt-in named finance identities and independent personal claim review.
-- Apply ONLY after validating the exact database backup and testing on an
-- isolated Neon branch. Production remains unchanged by this file.
-- The migration must be run by a trusted database administrator.
BEGIN;
DO $workos$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version=44)
       OR EXISTS (SELECT 1 FROM public.schema_migrations WHERE version>44)
    THEN RAISE EXCEPTION 'schema 44 required before upgrading to 45'; END IF;
END $workos$;

CREATE TABLE IF NOT EXISTS finance_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','finance_clerk','finance_approver','viewer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finance_personal_advance_reviews (
    advance_id TEXT PRIMARY KEY REFERENCES personal_advances(id),
    submitted_by_user_id TEXT NOT NULL REFERENCES finance_users(id),
    submitted_at TEXT NOT NULL,
    approved_by_user_id TEXT REFERENCES finance_users(id),
    approved_at TEXT,
    CHECK(approved_by_user_id IS NULL OR approved_by_user_id != submitted_by_user_id)
);
CREATE INDEX IF NOT EXISTS ix_finance_advances_preparer
    ON finance_personal_advance_reviews(submitted_by_user_id,submitted_at);
CREATE TABLE IF NOT EXISTS finance_period_close_requests (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    period TEXT NOT NULL,
    review_checks TEXT NOT NULL,
    requested_by_user_id TEXT NOT NULL REFERENCES finance_users(id),
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','cancelled')),
    approved_by_user_id TEXT REFERENCES finance_users(id),
    approved_at TEXT,
    cancelled_by_user_id TEXT REFERENCES finance_users(id),
    cancelled_at TEXT,
    CHECK(approved_by_user_id IS NULL OR approved_by_user_id != requested_by_user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_finance_pending_period_close
    ON finance_period_close_requests(period) WHERE status='pending';

CREATE TABLE IF NOT EXISTS finance_action_reviews (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    action_type TEXT NOT NULL CHECK(action_type IN ('bank_allocation','manual_journal','invoice_tax_status')),
    payload TEXT NOT NULL,
    expected_before TEXT,
    submitted_by_user_id TEXT NOT NULL REFERENCES finance_users(id),
    submitted_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','cancelled')),
    approved_by_user_id TEXT REFERENCES finance_users(id),
    approved_at TEXT,
    cancelled_by_user_id TEXT REFERENCES finance_users(id),
    cancelled_at TEXT,
    result_payload TEXT,
    CHECK(approved_by_user_id IS NULL OR approved_by_user_id != submitted_by_user_id)
);
CREATE INDEX IF NOT EXISTS ix_finance_action_reviews_status
    ON finance_action_reviews(status,submitted_at);

INSERT INTO schema_migrations(version,applied_at)
VALUES(45,CURRENT_TIMESTAMP::text)
ON CONFLICT(version) DO NOTHING;
COMMIT;
