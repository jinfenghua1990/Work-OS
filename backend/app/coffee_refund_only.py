"""Coffee refund-only accounting: refund money without fabricating stock returns.

'Refund without return' is a financial credit. Returned physical goods still
use the separate traced FIFO return flow, possibly after a prior refund.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENT=Decimal("0.01")


def ensure_refund_table(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sales_refunds (
            id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL UNIQUE,
            sale_line_id TEXT NOT NULL REFERENCES sale_lines(id),
            refunded_on TEXT NOT NULL,
            amount_cny TEXT NOT NULL,
            receivable_credit_cny TEXT NOT NULL,
            customer_refund_due_cny TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_sales_refunds_line ON sales_refunds(sale_line_id,refunded_on);
    """)


def remaining_revenue(conn, sale_line) -> Decimal:
    """Maximum new revenue reversal across both physical returns and refunds."""
    gross=(Decimal(sale_line["unit_price"])*int(sale_line["quantity"])).quantize(CENT,rounding=ROUND_HALF_UP)
    physical=conn.execute(
        "SELECT revenue_cny FROM sales_returns WHERE sale_line_id=?",
        (sale_line["id"],)
    ).fetchall()
    refunded=conn.execute(
        "SELECT amount_cny FROM sales_refunds WHERE sale_line_id=?",
        (sale_line["id"],)
    ).fetchall()
    reversed_total=sum((Decimal(x["revenue_cny"]) for x in physical),Decimal(0))
    reversed_total+=sum((Decimal(x["amount_cny"]) for x in refunded),Decimal(0))
    remaining=(gross-reversed_total).quantize(CENT,rounding=ROUND_HALF_UP)
    if remaining<0:
        raise ValueError("sale revenue reversal exceeds original sale amount; finance reconciliation required")
    return remaining
