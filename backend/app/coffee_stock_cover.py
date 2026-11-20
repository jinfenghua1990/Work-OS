"""Coffee finished-goods and packaging cover, based on confirmed stock movements.

This measures *estimated days of cover*, NOT accounting inventory-turnover
days. The latter requires reliable historical average stock value and COGS.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


def stock_cover(conn, today: date | None = None) -> dict:
    today = today or date.today()
    window_start = (today - timedelta(days=29)).isoformat()
    through = today.isoformat()

    balances = conn.execute(
        "SELECT sku,SUM(remaining_qty) AS quantity FROM stock_layers "
        "WHERE remaining_qty>0 GROUP BY sku ORDER BY sku"
    ).fetchall()
    movements = conn.execute(
        "SELECT sku,SUM(CASE WHEN movement_type='sale' THEN -quantity "
        "WHEN movement_type='sale_return' THEN -quantity ELSE 0 END) AS net_sold "
        "FROM inventory_movements "
        "WHERE movement_type IN ('sale','sale_return') AND effective_on>=? "
        "AND effective_on<=? GROUP BY sku",
        (window_start, through),
    ).fetchall()
    net_sales = {row["sku"]: max(0, int(row["net_sold"] or 0)) for row in movements}
    thresholds = {row["sku"]: int(row["safety_quantity"])
                  for row in conn.execute("SELECT sku,safety_quantity FROM inventory_safety_stocks")}
    sku_types = {}
    for row in conn.execute("SELECT sku,item_type FROM purchases ORDER BY created_at"):
        sku_types[row["sku"]] = row["item_type"]

    on_hand_by_sku = {row["sku"]: int(row["quantity"]) for row in balances}
    # Include zero-stock SKUs, otherwise a sold-out item disappears from alerts.
    all_skus = sorted(set(on_hand_by_sku) | set(net_sales) | set(thresholds) | set(sku_types))
    items = []
    for sku in all_skus:
        on_hand = on_hand_by_sku.get(sku, 0)
        last_30 = net_sales.get(sku, 0)
        threshold = thresholds.get(sku)
        daily = (Decimal(last_30) / Decimal(30)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        cover = (Decimal(on_hand) * Decimal(30) / Decimal(last_30)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ) if last_30 > 0 else None
        if threshold is not None and on_hand <= threshold:
            status = "below_safety_stock"
        elif cover is not None and cover < 30:
            status = "less_than_30_days"
        elif cover is None:
            status = "no_recent_sales"
        else:
            status = "normal"
        items.append({
            "sku": sku,
            "item_type": sku_types.get(sku, "unknown"),
            "on_hand": on_hand,
            "last_30_day_net_sales": last_30,
            "avg_daily_net_sales": str(daily),
            "estimated_cover_days": str(cover) if cover is not None else None,
            "safety_stock": threshold,
            "status": status,
        })
    return {
        "as_of": through, "sales_window_start": window_start,
        "metric": "days_of_cover_not_inventory_turnover",
        "items": items,
        "note": "使用当前结存与最近30天销售/实物退货净出库预测可售天数；无销量不计算周转，不等于按平均存货成本计算的财务周转天数。"
    }
