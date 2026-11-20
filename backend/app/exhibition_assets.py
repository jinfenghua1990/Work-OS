"""Exhibition samples held outside main warehouse remain company assets.

New sample dispatches are balance-sheet transfers, not instant expenses.
Historical already-posted dispatches remain marked legacy_expensed; the
migration intentionally does not rewrite closed financial statements.
"""
from __future__ import annotations

from decimal import Decimal


def migrate(conn) -> None:
    for table, columns in {
        "sample_dispatches":{
            "accounting_treatment":"TEXT NOT NULL DEFAULT 'legacy_expensed'",
            "disposed_quantity":"INTEGER NOT NULL DEFAULT 0",
        },
        "sample_dispatch_layers":{
            "disposed_quantity":"INTEGER NOT NULL DEFAULT 0",
        },
    }.items():
        available = {item["name"] for item in conn.execute(f"PRAGMA table_info({table})")}
        for column, declaration in columns.items():
            if column not in available:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sample_dispositions (
            id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL UNIQUE,
            dispatch_id TEXT NOT NULL REFERENCES sample_dispatches(id),
            disposed_on TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity>0),
            reason TEXT NOT NULL CHECK(reason IN ('consumed','gifted','damaged')),
            total_cost TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sample_disposition_layers (
            id TEXT PRIMARY KEY,
            disposition_id TEXT NOT NULL REFERENCES sample_dispositions(id),
            dispatch_layer_id TEXT NOT NULL REFERENCES sample_dispatch_layers(id),
            sku TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity>0),
            cost_cny TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_sample_dispositions_dispatch ON sample_dispositions(dispatch_id,disposed_on);
    """)
    conn.execute(
        "INSERT OR IGNORE INTO chart_of_accounts "
        "(account_code,account_name,account_type,normal_side,statement) "
        "VALUES ('inventory:exhibition','展会在外样品','asset','debit','balance')"
    )


def outstanding_asset_value(conn, as_of: str) -> Decimal:
    """Reconstruct outside inventory from physical movements, not GL amounts."""
    total=Decimal(0)
    dispatches=conn.execute(
        "SELECT id,total_cost FROM sample_dispatches "
        "WHERE accounting_treatment='asset_transfer' AND dispatched_on<=?",
        (as_of,),
    ).fetchall()
    for dispatch in dispatches:
        returned=conn.execute(
            "SELECT total_cost FROM sample_returns WHERE dispatch_id=? AND returned_on<=?",
            (dispatch["id"],as_of),
        ).fetchall()
        disposed=conn.execute(
            "SELECT total_cost FROM sample_dispositions WHERE dispatch_id=? AND disposed_on<=?",
            (dispatch["id"],as_of),
        ).fetchall()
        total+=Decimal(dispatch["total_cost"])
        total-=sum((Decimal(x["total_cost"]) for x in returned),Decimal(0))
        total-=sum((Decimal(x["total_cost"]) for x in disposed),Decimal(0))
    return total.quantize(Decimal("0.01"))
