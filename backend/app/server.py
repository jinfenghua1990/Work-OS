#!/usr/bin/env python3
"""Local Work OS API using Python's standard library only."""
from __future__ import annotations
import json
import os
import hashlib
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from urllib.parse import unquote

import original_archive
import local_auth
import xlsx_reader
import exhibition_assets
from coffee_stock_cover import stock_cover

from coffee_source_identity import normalize_sale_source
from bank_source_identity import bank_reference_identity

ROOT = Path(__file__).resolve().parents[1]


def load_local_env() -> None:
    """Load simple KEY=VALUE entries for local runs without a dotenv dependency."""
    env_file = ROOT.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


if os.environ.get("WORKOS_LOAD_ENV_FILE", "1").strip().lower() not in {"0", "false", "no"}:
    load_local_env()
DB_BACKEND = os.environ.get("WORKOS_DB_BACKEND", "sqlite").strip().lower()
if DB_BACKEND not in {"sqlite", "postgres"}:
    raise RuntimeError("WORKOS_DB_BACKEND must be 'sqlite' or 'postgres'")
DATABASE_URL = os.environ.get("WORKOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
DB_PATH = Path(os.environ.get("WORKOS_DB_PATH", ROOT / "data" / "workos.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
LATEST_SCHEMA_VERSION = 39
try:
    import psycopg as _psycopg
except ImportError:
    _psycopg = None
DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + ((_psycopg.IntegrityError,) if _psycopg else ())
DB_ERRORS = (sqlite3.Error,) + ((_psycopg.Error,) if _psycopg else ())
CENT = Decimal("0.01")
EXPENSE_CATEGORIES = {"marketing", "rent", "logistics", "software", "travel", "utilities", "professional_services", "booth_rental", "event_fee", "sample_cost", "decoration", "accommodation", "other"}
CHART_OF_ACCOUNTS = {
    "bank": ("银行存款", "asset", "debit", "balance"),
    "accounts_receivable": ("应收账款", "asset", "debit", "balance"),
    "inventory": ("存货", "asset", "debit", "balance"),
    "inventory:bike": ("自行车存货", "asset", "debit", "balance"),
    "inventory:exhibition": ("展会在外样品", "asset", "debit", "balance"),
    "supplier_advances": ("供应商预付款", "asset", "debit", "balance"),
    "unclassified_payments": ("待分类付款", "suspense", "debit", "balance"),
    "unclassified_receipts": ("待分类收款", "suspense", "credit", "balance"),
    "accounts_payable": ("应付账款", "liability", "credit", "balance"),
    "retained_earnings": ("未分配利润 / 期初权益平衡项", "equity", "credit", "balance"),
    "sales_revenue": ("销售收入", "revenue", "credit", "income"),
    "cost_of_goods_sold": ("主营业务成本", "expense", "debit", "income"),
    "realized_fx_gain": ("已实现汇兑收益", "revenue", "credit", "income"),
    "realized_fx_loss": ("已实现汇兑损失", "expense", "debit", "income"),
    "inventory_adjustment_gain": ("盘点盘盈", "revenue", "credit", "income"),
    "inventory_adjustment_loss": ("盘点盘亏", "expense", "debit", "income"),
}
for category in EXPENSE_CATEGORIES:
    CHART_OF_ACCOUNTS[f"operating_expense:{category}"] = (f"经营费用 · {category}", "expense", "debit", "income")


def amount(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a valid decimal amount")
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def whole_quantity(value: object, field: str) -> int:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a whole number")
    if not result.is_finite() or result != result.to_integral_value():
        raise ValueError(f"{field} must be a whole number")
    return int(result)


def exchange_rate(value: object, field: str = "exchange_rate") -> Decimal:
    try:
        result = Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a valid decimal rate")
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def record_open_item_fx(conn: sqlite3.Connection, item_id: str, original_amount_cny: Decimal, rate: Decimal, rate_date: str, rate_source: str) -> None:
    conn.execute("INSERT INTO open_item_fx (item_id,original_amount_cny,settled_amount_cny,exchange_rate,rate_date,rate_source) VALUES(?,?, '0.00',?,?,?) ON CONFLICT(item_id) DO UPDATE SET original_amount_cny=excluded.original_amount_cny,exchange_rate=excluded.exchange_rate,rate_date=excluded.rate_date,rate_source=excluded.rate_source",(item_id,str(original_amount_cny.quantize(CENT,rounding=ROUND_HALF_UP)),str(rate),rate_date,rate_source))


def require_same_source_payload(existing: sqlite3.Row, fields: dict[str, object]) -> None:
    if any(str(existing[key] if existing[key] is not None else "") != str(value if value is not None else "") for key,value in fields.items()):
        raise ValueError("source_key is already recorded with a different payload; reconcile the source record instead of retrying")


class PostgresRow(dict):
    """Mapping row that retains SQLite's existing row[0] call sites."""
    def __init__(self, names: list[str], values: tuple[object, ...]):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PostgresCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class PostgresConnection:
    """Small adapter for the shared SQL used by the SQLite and PostgreSQL backends."""
    is_postgres = True

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.connection.info.transaction_status:
            self.connection.rollback()
        self.connection.close()

    def execute(self, sql: str, parameters=()):
        pragma = re.fullmatch(r"\s*PRAGMA\s+(?:foreign_keys\s*=\s*ON|journal_mode\s*=\s*WAL)\s*", sql, re.I)
        if pragma:
            return PostgresCursor(self.connection.execute("SELECT 1 WHERE FALSE"))
        info = re.fullmatch(r"\s*PRAGMA\s+table_info\((\w+)\)\s*", sql, re.I)
        if info:
            cursor = self.connection.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position",
                (info.group(1),),
            )
            return PostgresCursor(cursor)
        sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
        ignore = re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\s", sql, re.I)
        if ignore:
            sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, count=1, flags=re.I).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        sql = sql.replace("?", "%s")
        return PostgresCursor(self.connection.execute(sql, parameters))

    def executescript(self, script: str):
        script = re.sub(r"CREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS\s+tr_open_item_fx_cny[\s\S]*?END\s*;", "", script, flags=re.I)
        statements = [statement.strip() for statement in script.split(";") if statement.strip()]
        table_statements = [statement for statement in statements if re.match(r"CREATE\s+TABLE\s", statement, re.I)]
        other_statements = [statement for statement in statements if statement not in table_statements]
        pending = table_statements[:]
        ordered = []
        while pending:
            progressed = False
            for statement in pending[:]:
                match = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", statement, re.I)
                name = match.group(1).lower() if match else ""
                dependencies = {value.lower() for value in re.findall(r"REFERENCES\s+(\w+)", statement, re.I)}
                if dependencies.issubset({name.lower() for name in ordered} | {""}):
                    ordered.append(name)
                    self.execute(statement)
                    pending.remove(statement)
                    progressed = True
            if not progressed:
                statement = pending.pop(0)
                match = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", statement, re.I)
                if match:
                    ordered.append(match.group(1).lower())
                self.execute(statement)
        for statement in other_statements:
            self.execute(statement)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()


def db():
    if DB_BACKEND == "postgres":
        if not DATABASE_URL or not DATABASE_URL.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("WORKOS_DB_BACKEND=postgres requires WORKOS_DATABASE_URL or DATABASE_URL")
        if _psycopg is None:
            raise RuntimeError("PostgreSQL support requires psycopg[binary]; install backend/requirements.txt")
        connection = _psycopg.connect(DATABASE_URL, autocommit=True, row_factory=lambda cursor: lambda values: PostgresRow([column.name for column in cursor.description], values))
        return PostgresConnection(connection)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_postgres_triggers(conn: PostgresConnection) -> None:
    trigger_exists = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='tr_open_item_fx_cny' AND NOT tgisinternal) AS present"
    ).fetchone()["present"]
    function_exists = conn.execute(
        "SELECT to_regprocedure('workos_open_item_fx_default()') AS signature"
    ).fetchone()["signature"]
    if trigger_exists and function_exists:
        return
    if not function_exists:
        conn.execute("""CREATE FUNCTION workos_open_item_fx_default() RETURNS trigger AS $$
        BEGIN
            IF NEW.currency='CNY' THEN
                INSERT INTO open_item_fx (item_id,original_amount_cny,settled_amount_cny,exchange_rate,rate_date,rate_source)
                VALUES (NEW.id,NEW.original_amount,NEW.settled_amount,'1',NEW.due_on,'functional_currency')
                ON CONFLICT (item_id) DO NOTHING;
            END IF;
            RETURN NEW;
        END;
    $$ LANGUAGE plpgsql""")
    if not trigger_exists:
        conn.execute("CREATE TRIGGER tr_open_item_fx_cny AFTER INSERT ON receivables_payables FOR EACH ROW EXECUTE FUNCTION workos_open_item_fx_default()")


def ensure_tax_history_period_columns(conn) -> None:
    # Old tax history is preserved as written. Dates for historic rows remain
    # UNKNOWN rather than being guessed from issue date or change timestamp.
    cols={row["name"] for row in conn.execute("PRAGMA table_info(invoice_tax_history)")}
    if "effective_on" not in cols:
        conn.execute("ALTER TABLE invoice_tax_history ADD COLUMN effective_on TEXT")
    if "tax_period" not in cols:
        conn.execute("ALTER TABLE invoice_tax_history ADD COLUMN tax_period TEXT")
    bike_cols={row["name"] for row in conn.execute("PRAGMA table_info(bike_landed_costs)")}
    if "cost_treatment" not in bike_cols:
        # Preserve historical already-posted capitalization; do NOT rewrite
        # previously closed books or silently convert old warehouse entries.
        conn.execute("ALTER TABLE bike_landed_costs ADD COLUMN cost_treatment TEXT NOT NULL DEFAULT 'capitalized'")
    if "capitalization_basis" not in bike_cols:
        conn.execute("ALTER TABLE bike_landed_costs ADD COLUMN capitalization_basis TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE bike_landed_costs SET cost_treatment='tax_pending' WHERE cost_type='import_vat' AND cost_treatment='capitalized'")


def init_db() -> None:
    with db() as conn:
        if getattr(conn, "is_postgres", False):
            existing_schema = conn.execute("SELECT to_regclass('public.schema_migrations') AS table_name").fetchone()["table_name"]
            if existing_schema:
                version = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"]
                if version is not None and int(version) >= LATEST_SCHEMA_VERSION:
                    ensure_postgres_triggers(conn)
                    return
                if version is not None and int(version)==38:
                    conn.execute("BEGIN")
                    exhibition_assets.migrate(conn)
                    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(39,?)",(now(),))
                    ensure_postgres_triggers(conn)
                    conn.commit()
                    return
                if version is not None and int(version) in (36,37):
                    conn.execute("BEGIN")
                    if int(version)==36:
                        original_archive.ensure_tables(conn)
                        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(37,?)", (now(),))
                    ensure_tax_history_period_columns(conn)
                    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(38,?)", (now(),))
                    exhibition_assets.migrate(conn)
                    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(39,?)",(now(),))
                    ensure_postgres_triggers(conn)
                    conn.commit()
                    return
                if version is not None and int(version) in (32,33,34,35):
                    conn.execute("BEGIN")
                    if int(version)==32:
                        conn.execute("CREATE TABLE IF NOT EXISTS inventory_safety_stocks (sku TEXT PRIMARY KEY, safety_quantity INTEGER NOT NULL CHECK(safety_quantity >= 0), updated_at TEXT NOT NULL)")
                        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(33,?)", (datetime.now(timezone.utc).isoformat(),))
                    if int(version)<=33:
                        columns={row["name"] for row in conn.execute("PRAGMA table_info(sale_lines)")}
                        if "channel" not in columns:conn.execute("ALTER TABLE sale_lines ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
                        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(34,?)", (datetime.now(timezone.utc).isoformat(),))
                    columns={row["name"] for row in conn.execute("PRAGMA table_info(journal_entries)")}
                    if "memo" not in columns:conn.execute("ALTER TABLE journal_entries ADD COLUMN memo TEXT NOT NULL DEFAULT ''")
                    conn.execute("CREATE TABLE IF NOT EXISTS manual_journal_requests (source_key TEXT PRIMARY KEY,payload TEXT NOT NULL,journal_entry_id TEXT NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)")
                    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(35,?)", (datetime.now(timezone.utc).isoformat(),))
                    conn.execute("CREATE TABLE IF NOT EXISTS inventory_adjustments (id TEXT PRIMARY KEY,source_key TEXT NOT NULL UNIQUE,sku TEXT NOT NULL,item_type TEXT NOT NULL CHECK(item_type IN ('finished_goods','packaging')),direction TEXT NOT NULL CHECK(direction IN ('gain','loss')),quantity INTEGER NOT NULL CHECK(quantity>0),unit_cost TEXT NOT NULL,currency TEXT NOT NULL,exchange_rate TEXT NOT NULL,adjusted_on TEXT NOT NULL,reason TEXT NOT NULL,business_line TEXT NOT NULL,entity_code TEXT NOT NULL,project_code TEXT,stock_layer_id TEXT REFERENCES stock_layers(id),journal_entry_id TEXT REFERENCES journal_entries(id),created_at TEXT NOT NULL)")
                    layer_columns={row["name"] for row in conn.execute("PRAGMA table_info(stock_layers)")}
                    for column,declaration in {"adjustment_id":"TEXT REFERENCES inventory_adjustments(id)","currency":"TEXT NOT NULL DEFAULT 'UNKNOWN'","exchange_rate":"TEXT NOT NULL DEFAULT '1'"}.items():
                        if column not in layer_columns:conn.execute(f"ALTER TABLE stock_layers ADD COLUMN {column} {declaration}")
                    conn.execute("UPDATE stock_layers SET currency=COALESCE((SELECT currency FROM purchases WHERE purchases.id=stock_layers.purchase_id),currency)")
                    conn.execute("UPDATE stock_layers SET exchange_rate=COALESCE((SELECT exchange_rate FROM purchases WHERE purchases.id=stock_layers.purchase_id),exchange_rate)")
                    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(36,?)", (datetime.now(timezone.utc).isoformat(),))
                    original_archive.ensure_tables(conn)
                    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(37,?)", (now(),))
                    ensure_tax_history_period_columns(conn)
                    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(38,?)", (now(),))
                    exhibition_assets.migrate(conn)
                    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(39,?)",(now(),))
                    ensure_postgres_triggers(conn)
                    conn.commit()
                    return
            conn.execute("BEGIN")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS purchases (
            id TEXT PRIMARY KEY, number TEXT NOT NULL UNIQUE, supplier TEXT NOT NULL,
            sku TEXT NOT NULL, item_type TEXT NOT NULL CHECK(item_type IN ('finished_goods','packaging')),
            ordered_qty INTEGER NOT NULL CHECK(ordered_qty > 0), unit_cost TEXT NOT NULL,
            received_qty INTEGER NOT NULL DEFAULT 0 CHECK(received_qty >= 0),
            currency TEXT NOT NULL DEFAULT 'CNY', exchange_rate TEXT NOT NULL DEFAULT '1', rate_date TEXT,
            created_at TEXT NOT NULL, business_line TEXT NOT NULL DEFAULT 'coffee',
            entity_code TEXT NOT NULL DEFAULT 'main', project_code TEXT
        );
        CREATE TABLE IF NOT EXISTS purchase_receipts (
            id TEXT PRIMARY KEY, purchase_id TEXT NOT NULL REFERENCES purchases(id),
            source_key TEXT NOT NULL UNIQUE, quantity INTEGER NOT NULL CHECK(quantity>0),
            received_on TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_layers (
            id TEXT PRIMARY KEY, purchase_id TEXT REFERENCES purchases(id), adjustment_id TEXT, sku TEXT NOT NULL,
            received_qty INTEGER NOT NULL CHECK(received_qty > 0), remaining_qty INTEGER NOT NULL CHECK(remaining_qty >= 0),
            unit_cost TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'UNKNOWN', exchange_rate TEXT NOT NULL DEFAULT '1', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sales_imports (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
            sold_on TEXT NOT NULL, created_at TEXT NOT NULL, business_line TEXT NOT NULL DEFAULT 'coffee',
            entity_code TEXT NOT NULL DEFAULT 'main', project_code TEXT
        );
        CREATE TABLE IF NOT EXISTS sale_lines (
            id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES sales_imports(id), source_row_key TEXT NOT NULL UNIQUE, sku TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0), unit_price TEXT NOT NULL,
            packaging_sku TEXT, packaging_per_unit INTEGER NOT NULL DEFAULT 0 CHECK(packaging_per_unit >= 0),
            packaging_included INTEGER NOT NULL DEFAULT 0 CHECK(packaging_included IN (0,1)), channel TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sku_packaging_rules (
            sku TEXT PRIMARY KEY, packaging_sku TEXT, packaging_per_unit INTEGER NOT NULL DEFAULT 0 CHECK(packaging_per_unit >= 0),
            packaging_included INTEGER NOT NULL DEFAULT 0 CHECK(packaging_included IN (0,1)), updated_at TEXT NOT NULL,
            CHECK(packaging_included=0 OR packaging_per_unit=0)
        );
        CREATE TABLE IF NOT EXISTS inventory_safety_stocks (
            sku TEXT PRIMARY KEY, safety_quantity INTEGER NOT NULL CHECK(safety_quantity >= 0),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_rows (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('posted','rejected')), result TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inventory_movements (
            id TEXT PRIMARY KEY, sku TEXT NOT NULL, movement_type TEXT NOT NULL,
            quantity INTEGER NOT NULL, unit_cost TEXT NOT NULL, source_type TEXT NOT NULL,
            source_id TEXT NOT NULL, created_at TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'UNKNOWN',
            effective_on TEXT, base_unit_cost_cny TEXT
        );
        CREATE TABLE IF NOT EXISTS journal_entries (
            id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
            posted_on TEXT NOT NULL, created_at TEXT NOT NULL, business_line TEXT NOT NULL DEFAULT 'unassigned',
            entity_code TEXT NOT NULL DEFAULT 'unassigned', project_code TEXT, memo TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS manual_journal_requests (
            source_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
            journal_entry_id TEXT NOT NULL UNIQUE REFERENCES journal_entries(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inventory_adjustments (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, sku TEXT NOT NULL,
            item_type TEXT NOT NULL CHECK(item_type IN ('finished_goods','packaging')),
            direction TEXT NOT NULL CHECK(direction IN ('gain','loss')), quantity INTEGER NOT NULL CHECK(quantity>0),
            unit_cost TEXT NOT NULL, currency TEXT NOT NULL, exchange_rate TEXT NOT NULL,
            adjusted_on TEXT NOT NULL, reason TEXT NOT NULL, business_line TEXT NOT NULL,
            entity_code TEXT NOT NULL, project_code TEXT, stock_layer_id TEXT REFERENCES stock_layers(id),
            journal_entry_id TEXT REFERENCES journal_entries(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS journal_lines (
            id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES journal_entries(id),
            account TEXT NOT NULL, debit TEXT NOT NULL, credit TEXT NOT NULL,
            business_line TEXT, entity_code TEXT, project_code TEXT,
            CHECK(NOT (CAST(debit AS REAL) > 0 AND CAST(credit AS REAL) > 0))
        );
        CREATE TABLE IF NOT EXISTS opening_balance_imports (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, as_of TEXT NOT NULL,
            payload TEXT NOT NULL, journal_entry_id TEXT NOT NULL UNIQUE REFERENCES journal_entries(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chart_of_accounts (
            account_code TEXT PRIMARY KEY, account_name TEXT NOT NULL,
            account_type TEXT NOT NULL CHECK(account_type IN ('asset','liability','equity','revenue','expense','suspense','unclassified')),
            normal_side TEXT NOT NULL CHECK(normal_side IN ('debit','credit')),
            statement TEXT NOT NULL CHECK(statement IN ('balance','income','unmapped')),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS period_locks (
            period TEXT PRIMARY KEY, closed_at TEXT NOT NULL, closed_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS receivables_payables (
            id TEXT PRIMARY KEY, direction TEXT NOT NULL CHECK(direction IN ('receivable','payable')),
            source_type TEXT NOT NULL, source_id TEXT NOT NULL, counterparty TEXT NOT NULL,
            original_amount TEXT NOT NULL, settled_amount TEXT NOT NULL DEFAULT '0.00',
            currency TEXT NOT NULL DEFAULT 'CNY', due_on TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS open_item_fx (
            item_id TEXT PRIMARY KEY REFERENCES receivables_payables(id),
            original_amount_cny TEXT NOT NULL, settled_amount_cny TEXT NOT NULL DEFAULT '0.00',
            exchange_rate TEXT NOT NULL, rate_date TEXT, rate_source TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS tr_open_item_fx_cny AFTER INSERT ON receivables_payables
        WHEN NEW.currency='CNY' BEGIN
            INSERT OR IGNORE INTO open_item_fx (item_id,original_amount_cny,settled_amount_cny,exchange_rate,rate_date,rate_source)
            VALUES(NEW.id,NEW.original_amount,NEW.settled_amount,'1',NEW.due_on,'functional_currency');
        END;
        CREATE TABLE IF NOT EXISTS expenses (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, incurred_on TEXT NOT NULL,
            category TEXT NOT NULL, counterparty TEXT NOT NULL, amount TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'), memo TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            business_line TEXT NOT NULL DEFAULT 'corporate', entity_code TEXT NOT NULL DEFAULT 'main', project_code TEXT
        );
        CREATE TABLE IF NOT EXISTS expense_project_allocations (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
            expense_id TEXT NOT NULL REFERENCES expenses(id), project_code TEXT NOT NULL REFERENCES projects(code),
            business_line TEXT NOT NULL, amount TEXT NOT NULL, allocated_on TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            entity_code TEXT NOT NULL, starts_on TEXT NOT NULL, ends_on TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_budgets (
            id TEXT PRIMARY KEY, project_code TEXT NOT NULL REFERENCES projects(code),
            category TEXT NOT NULL, budget_amount TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(project_code,category)
        );
        CREATE TABLE IF NOT EXISTS sample_dispatches (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, project_code TEXT NOT NULL REFERENCES projects(code),
            sku TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0), returned_quantity INTEGER NOT NULL DEFAULT 0,
            packaging_sku TEXT, packaging_per_unit INTEGER NOT NULL DEFAULT 0, packaging_included INTEGER NOT NULL DEFAULT 0,
            dispatched_on TEXT NOT NULL, total_cost TEXT NOT NULL DEFAULT '0.00', currency TEXT NOT NULL DEFAULT 'CNY',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sample_dispatch_layers (
            id TEXT PRIMARY KEY, dispatch_id TEXT NOT NULL REFERENCES sample_dispatches(id), item_role TEXT NOT NULL CHECK(item_role IN ('item','packaging')),
            sku TEXT NOT NULL, stock_layer_id TEXT NOT NULL REFERENCES stock_layers(id), quantity INTEGER NOT NULL CHECK(quantity>0),
            returned_quantity INTEGER NOT NULL DEFAULT 0, unit_cost TEXT NOT NULL, currency TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sample_returns (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, dispatch_id TEXT NOT NULL REFERENCES sample_dispatches(id),
            returned_on TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0), total_cost TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bike_orders (
            id TEXT PRIMARY KEY, alsvid_order_no TEXT NOT NULL UNIQUE, supplier TEXT NOT NULL,
            contract_amount TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency IN ('CNY','EUR')),
            exchange_rate TEXT NOT NULL, rate_date TEXT NOT NULL, ordered_on TEXT NOT NULL,
            entity_code TEXT NOT NULL, created_at TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity>0)
        );
        CREATE TABLE IF NOT EXISTS bike_milestones (
            id TEXT PRIMARY KEY, bike_order_id TEXT NOT NULL REFERENCES bike_orders(id), name TEXT NOT NULL,
            percentage TEXT NOT NULL, amount TEXT NOT NULL, due_on TEXT NOT NULL,
            payable_id TEXT, recognized_on TEXT, created_at TEXT NOT NULL, UNIQUE(bike_order_id,name)
        );
        CREATE TABLE IF NOT EXISTS bike_landed_costs (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
            bike_order_id TEXT NOT NULL REFERENCES bike_orders(id), cost_type TEXT NOT NULL CHECK(cost_type IN ('freight','duty','warehouse','other','import_vat')),
            counterparty TEXT NOT NULL, reference_no TEXT NOT NULL DEFAULT '', incurred_on TEXT NOT NULL,
            amount TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency IN ('CNY','EUR')),
            exchange_rate TEXT NOT NULL, rate_date TEXT NOT NULL, base_amount_cny TEXT NOT NULL,
            payable_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bike_sales (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
            bike_order_id TEXT NOT NULL REFERENCES bike_orders(id), source TEXT NOT NULL,
            external_order_no TEXT NOT NULL, sold_on TEXT NOT NULL, channel TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity>0), currency TEXT NOT NULL CHECK(currency IN ('CNY','EUR')),
            net_revenue TEXT NOT NULL, exchange_rate TEXT NOT NULL, rate_date TEXT NOT NULL,
            revenue_cny TEXT NOT NULL, unit_landed_cost_cny TEXT NOT NULL, cogs_cny TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bike_receipts (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
            bike_order_id TEXT NOT NULL REFERENCES bike_orders(id), received_on TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity>0), contract_cost_cny TEXT NOT NULL,
            advance_cleared_cny TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_transactions (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, transaction_on TEXT NOT NULL,
            account_name TEXT NOT NULL, bank_reference TEXT NOT NULL, description TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('in','out')), amount TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'CNY', created_at TEXT NOT NULL,
            exchange_rate TEXT, rate_date TEXT, rate_source TEXT, base_amount_cny TEXT,
            cash_flow_category TEXT NOT NULL DEFAULT 'unclassified', cash_flow_note TEXT, cash_flow_classified_at TEXT
        );
        CREATE TABLE IF NOT EXISTS bank_allocations (
            id TEXT PRIMARY KEY, bank_transaction_id TEXT NOT NULL REFERENCES bank_transactions(id),
            target_type TEXT NOT NULL CHECK(target_type IN ('receivable','payable','unclassified')),
            target_id TEXT, amount TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_allocation_fx (
            allocation_id TEXT PRIMARY KEY REFERENCES bank_allocations(id),
            bank_amount_cny TEXT NOT NULL, carrying_amount_cny TEXT NOT NULL,
            realized_fx_cny TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_cash_flow_history (
            id TEXT PRIMARY KEY, bank_transaction_id TEXT NOT NULL REFERENCES bank_transactions(id),
            old_category TEXT NOT NULL, new_category TEXT NOT NULL, note TEXT NOT NULL,
            changed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id TEXT PRIMARY KEY, account_name TEXT NOT NULL UNIQUE,
            currency TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_statement_reconciliations (
            id TEXT PRIMARY KEY, bank_account_id TEXT NOT NULL REFERENCES bank_accounts(id),
            period TEXT NOT NULL, opening_balance TEXT NOT NULL, closing_balance TEXT NOT NULL,
            statement_reference TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', submitted_at TEXT NOT NULL,
            UNIQUE(bank_account_id,period)
        );
        CREATE TABLE IF NOT EXISTS bank_reconciliation_history (
            id TEXT PRIMARY KEY, reconciliation_id TEXT NOT NULL REFERENCES bank_statement_reconciliations(id),
            opening_balance TEXT NOT NULL, closing_balance TEXT NOT NULL, statement_reference TEXT NOT NULL,
            note TEXT NOT NULL, changed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_account_coverage (
            period TEXT PRIMARY KEY, account_count INTEGER NOT NULL,
            note TEXT NOT NULL, confirmed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY, invoice_number TEXT NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('sales','purchase')),
            counterparty TEXT NOT NULL, issued_on TEXT NOT NULL, total_amount TEXT NOT NULL,
            tax_amount TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'CNY', source_key TEXT NOT NULL UNIQUE,
            tax_status TEXT NOT NULL CHECK(tax_status IN ('pending','eligible','certified_deductible','non_deductible','void')),
            tax_evidence_ref TEXT, exchange_rate TEXT NOT NULL DEFAULT '1', rate_date TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoice_links (
            id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id),
            target_type TEXT NOT NULL, target_id TEXT NOT NULL, linked_amount TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(invoice_id,target_type,target_id)
        );
        CREATE TABLE IF NOT EXISTS invoice_tax_history (
            id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id),
            old_status TEXT NOT NULL, new_status TEXT NOT NULL, evidence_ref TEXT,
            changed_at TEXT NOT NULL, changed_by TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_stock_fifo ON stock_layers(sku, created_at, id);
        CREATE INDEX IF NOT EXISTS ix_movements_sku ON inventory_movements(sku, created_at);
        """)
        purchase_columns = {row["name"] for row in conn.execute("PRAGMA table_info(purchases)")}
        if "currency" not in purchase_columns:
            conn.execute("ALTER TABLE purchases ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'")
        if "exchange_rate" not in purchase_columns:
            conn.execute("ALTER TABLE purchases ADD COLUMN exchange_rate TEXT NOT NULL DEFAULT '1'")
        if "rate_date" not in purchase_columns:
            conn.execute("ALTER TABLE purchases ADD COLUMN rate_date TEXT")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(invoices)")}
        if "tax_evidence_ref" not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN tax_evidence_ref TEXT")
        if "exchange_rate" not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN exchange_rate TEXT NOT NULL DEFAULT '1'")
        if "rate_date" not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN rate_date TEXT")
        movement_columns = {row["name"] for row in conn.execute("PRAGMA table_info(inventory_movements)")}
        if "currency" not in movement_columns:
            conn.execute("ALTER TABLE inventory_movements ADD COLUMN currency TEXT NOT NULL DEFAULT 'UNKNOWN'")
        for table, columns in {
            "purchases": {"business_line": "TEXT NOT NULL DEFAULT 'coffee'", "entity_code": "TEXT NOT NULL DEFAULT 'main'", "project_code": "TEXT"},
            "sales_imports": {"business_line": "TEXT NOT NULL DEFAULT 'coffee'", "entity_code": "TEXT NOT NULL DEFAULT 'main'", "project_code": "TEXT"},
            "journal_entries": {"business_line": "TEXT NOT NULL DEFAULT 'unassigned'", "entity_code": "TEXT NOT NULL DEFAULT 'unassigned'", "project_code": "TEXT", "memo": "TEXT NOT NULL DEFAULT ''"},
            "expenses": {"business_line": "TEXT NOT NULL DEFAULT 'corporate'", "entity_code": "TEXT NOT NULL DEFAULT 'main'", "project_code": "TEXT"},
        }.items():
            present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, declaration in columns.items():
                if column not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", (datetime.now(timezone.utc).isoformat(),))
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
            for row in conn.execute("SELECT id,supplier,received_qty,unit_cost FROM purchases WHERE received_qty>0"):
                expected = Decimal(row["received_qty"]) * Decimal(row["unit_cost"])
                exists = conn.execute("SELECT 1 FROM receivables_payables WHERE source_type='legacy_purchase' AND source_id=?", (row["id"],)).fetchone()
                if not exists:
                    conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00','CNY',NULL,?)", (str(uuid4()), "payable", "legacy_purchase", row["id"], row["supplier"], str(expected), datetime.now(timezone.utc).isoformat()))
            for row in conn.execute("SELECT id,source,source_key,sold_on,created_at FROM sales_imports"):
                exists = conn.execute("SELECT 1 FROM receivables_payables WHERE source_type='legacy_sale' AND source_id=?", (row["id"],)).fetchone()
                if not exists:
                    revenue = sum((Decimal(x[0]) * Decimal(x[1]) for x in conn.execute("SELECT quantity,unit_price FROM sale_lines WHERE import_id=?", (row["id"],))), Decimal(0)).quantize(CENT)
                    if revenue > 0:
                        conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00','CNY',NULL,?)", (str(uuid4()), "receivable", "legacy_sale", row["id"], "未指定客户", str(revenue), datetime.now(timezone.utc).isoformat()))
            conn.execute("INSERT INTO schema_migrations VALUES(2,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(3,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(4,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(5,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(6,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(7,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(8,?)", (datetime.now(timezone.utc).isoformat(),))
        journal_columns = {row["name"] for row in conn.execute("PRAGMA table_info(journal_entries)")}
        if "currency" not in journal_columns:
            conn.execute("ALTER TABLE journal_entries ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'")
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=9").fetchone():
            conn.execute("UPDATE journal_entries SET currency=COALESCE((SELECT m.currency FROM inventory_movements m WHERE m.source_type='purchase_receipt' AND m.source_id=journal_entries.source_id LIMIT 1),'UNKNOWN') WHERE source_type='purchase_receipt'")
            conn.execute("UPDATE journal_entries SET currency=COALESCE((SELECT b.currency FROM bank_transactions b WHERE b.id=journal_entries.source_id),'UNKNOWN') WHERE source_type='bank_allocation'")
            conn.execute("INSERT INTO schema_migrations VALUES(9,?)", (datetime.now(timezone.utc).isoformat(),))
        bike_order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(bike_orders)")}
        if "quantity" not in bike_order_columns:
            conn.execute("ALTER TABLE bike_orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(10,?)", (datetime.now(timezone.utc).isoformat(),))
        if "sku" not in bike_order_columns:
            conn.execute("ALTER TABLE bike_orders ADD COLUMN sku TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(11,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(12,?)", (datetime.now(timezone.utc).isoformat(),))
        bank_columns={row["name"] for row in conn.execute("PRAGMA table_info(bank_transactions)")}
        for column,declaration in {"exchange_rate":"TEXT","rate_date":"TEXT","rate_source":"TEXT","base_amount_cny":"TEXT"}.items():
            if column not in bank_columns:
                conn.execute(f"ALTER TABLE bank_transactions ADD COLUMN {column} {declaration}")
        conn.execute("UPDATE bank_transactions SET exchange_rate='1',rate_date=transaction_on,rate_source='functional_currency',base_amount_cny=amount WHERE currency='CNY' AND base_amount_cny IS NULL")
        conn.execute("INSERT OR IGNORE INTO bank_allocation_fx (allocation_id,bank_amount_cny,carrying_amount_cny,realized_fx_cny) SELECT a.id,a.amount,a.amount,'0.00' FROM bank_allocations a JOIN bank_transactions b ON b.id=a.bank_transaction_id WHERE b.currency='CNY'")
        conn.execute("INSERT OR IGNORE INTO open_item_fx (item_id,original_amount_cny,settled_amount_cny,exchange_rate,rate_date,rate_source) SELECT id,original_amount,settled_amount,'1',due_on,'functional_currency' FROM receivables_payables WHERE currency='CNY'")
        for item in conn.execute("SELECT * FROM receivables_payables WHERE currency!='CNY'").fetchall():
            if conn.execute("SELECT 1 FROM open_item_fx WHERE item_id=?",(item["id"],)).fetchone():
                continue
            source=item["source_type"]
            base=None; rate=None; rate_day=None; rate_source="legacy_unverified"
            if source=="bike_sale":
                record=conn.execute("SELECT revenue_cny,exchange_rate,rate_date FROM bike_sales WHERE id=?",(item["source_id"],)).fetchone()
                if record: base,rate,rate_day,rate_source=record["revenue_cny"],record["exchange_rate"],record["rate_date"],"bike_sales_import"
            elif source=="bike_landed_cost":
                record=conn.execute("SELECT base_amount_cny,exchange_rate,rate_date FROM bike_landed_costs WHERE id=?",(item["source_id"],)).fetchone()
                if record: base,rate,rate_day,rate_source=record["base_amount_cny"],record["exchange_rate"],record["rate_date"],"landed_cost_entry"
            elif source=="bike_milestone":
                record=conn.execute("SELECT m.amount,o.exchange_rate,o.rate_date FROM bike_milestones m JOIN bike_orders o ON o.id=m.bike_order_id WHERE m.id=?",(item["source_id"],)).fetchone()
                if record:
                    rate=record["exchange_rate"];rate_day=record["rate_date"];base=str((Decimal(record["amount"])*Decimal(rate)).quantize(CENT,rounding=ROUND_HALF_UP));rate_source="bike_contract_rate"
            if base is not None:
                conn.execute("INSERT OR IGNORE INTO open_item_fx VALUES(?,?, '0.00',?,?,?)",(item["id"],base,rate,rate_day,rate_source))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(13,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(14,?)", (datetime.now(timezone.utc).isoformat(),))
        milestone_columns={row["name"] for row in conn.execute("PRAGMA table_info(bike_milestones)")}
        if "recognized_on" not in milestone_columns:
            conn.execute("ALTER TABLE bike_milestones ADD COLUMN recognized_on TEXT")
        conn.execute("UPDATE bike_milestones SET recognized_on=(SELECT e.posted_on FROM journal_entries e WHERE e.source_type='bike_milestone_due' AND e.source_id=bike_milestones.id LIMIT 1) WHERE payable_id IS NOT NULL AND recognized_on IS NULL")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(15,?)", (datetime.now(timezone.utc).isoformat(),))
        journal_line_columns={row["name"] for row in conn.execute("PRAGMA table_info(journal_lines)")}
        for column in ("business_line", "entity_code", "project_code"):
            if column not in journal_line_columns:
                conn.execute(f"ALTER TABLE journal_lines ADD COLUMN {column} TEXT")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(16,?)", (datetime.now(timezone.utc).isoformat(),))
        period_columns={row["name"] for row in conn.execute("PRAGMA table_info(period_locks)")}
        if "reviewed_checks" not in period_columns:
            conn.execute("ALTER TABLE period_locks ADD COLUMN reviewed_checks TEXT NOT NULL DEFAULT '[]'")
        if "journal_count" not in period_columns:
            conn.execute("ALTER TABLE period_locks ADD COLUMN journal_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(17,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(18,?)", (datetime.now(timezone.utc).isoformat(),))
        for code,(name,account_type,normal_side,statement) in CHART_OF_ACCOUNTS.items():
            conn.execute("INSERT OR IGNORE INTO chart_of_accounts (account_code,account_name,account_type,normal_side,statement) VALUES(?,?,?,?,?)",(code,name,account_type,normal_side,statement))
        for row in conn.execute("SELECT DISTINCT account FROM journal_lines").fetchall():
            code=row["account"]
            if code not in CHART_OF_ACCOUNTS:
                conn.execute("INSERT OR IGNORE INTO chart_of_accounts (account_code,account_name,account_type,normal_side,statement) VALUES(?,?, 'unclassified','debit','unmapped')",(code,code))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(19,?)", (datetime.now(timezone.utc).isoformat(),))
        movement_columns={row["name"] for row in conn.execute("PRAGMA table_info(inventory_movements)")}
        for column in ("effective_on","base_unit_cost_cny"):
            if column not in movement_columns:
                conn.execute(f"ALTER TABLE inventory_movements ADD COLUMN {column} TEXT")
        conn.execute("UPDATE inventory_movements SET effective_on=(SELECT e.posted_on FROM journal_entries e WHERE e.source_id=inventory_movements.source_id AND e.source_type=inventory_movements.source_type ORDER BY e.posted_on LIMIT 1) WHERE effective_on IS NULL")
        conn.execute("UPDATE inventory_movements SET base_unit_cost_cny=unit_cost WHERE base_unit_cost_cny IS NULL AND currency='CNY'")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(20,?)", (datetime.now(timezone.utc).isoformat(),))
        bank_columns={row["name"] for row in conn.execute("PRAGMA table_info(bank_transactions)")}
        for column,declaration in {"cash_flow_category":"TEXT NOT NULL DEFAULT 'unclassified'","cash_flow_note":"TEXT","cash_flow_classified_at":"TEXT"}.items():
            if column not in bank_columns:conn.execute(f"ALTER TABLE bank_transactions ADD COLUMN {column} {declaration}")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(21,?)", (datetime.now(timezone.utc).isoformat(),))
        for row in conn.execute("SELECT DISTINCT account_name,currency FROM bank_transactions").fetchall():
            conn.execute("INSERT OR IGNORE INTO bank_accounts (id,account_name,currency,created_at) VALUES(?,?,?,?)",(str(uuid4()),row["account_name"],row["currency"],now()))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(22,?)", (datetime.now(timezone.utc).isoformat(),))
        layer_columns={row["name"] for row in conn.execute("PRAGMA table_info(stock_layers)")}
        if "purchase_receipt_id" not in layer_columns:
            conn.execute("ALTER TABLE stock_layers ADD COLUMN purchase_receipt_id TEXT")
        for purchase in conn.execute("SELECT DISTINCT purchase_id FROM stock_layers WHERE purchase_receipt_id IS NULL").fetchall():
            layers=conn.execute("SELECT id,received_qty,unit_cost FROM stock_layers WHERE purchase_id=? AND purchase_receipt_id IS NULL ORDER BY created_at,id",(purchase["purchase_id"],)).fetchall()
            receipts=conn.execute("SELECT id,quantity FROM purchase_receipts WHERE purchase_id=? ORDER BY created_at,id",(purchase["purchase_id"],)).fetchall()
            if len(layers)==len(receipts) and all(int(layer["received_qty"])==int(receipt["quantity"]) for layer,receipt in zip(layers,receipts)):
                for layer,receipt in zip(layers,receipts):
                    conn.execute("UPDATE stock_layers SET purchase_receipt_id=? WHERE id=?",(receipt["id"],layer["id"]))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(23,?)", (datetime.now(timezone.utc).isoformat(),))
        for table,columns in {
            "sale_lines":{"cogs_cny":"TEXT"},
            "inventory_movements":{"sale_line_id":"TEXT","stock_layer_id":"TEXT","cogs_cny":"TEXT"},
        }.items():
            present={row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column,declaration in columns.items():
                if column not in present:conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sales_returns (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
            sale_line_id TEXT NOT NULL REFERENCES sale_lines(id), quantity INTEGER NOT NULL CHECK(quantity>0),
            packaging_quantity INTEGER NOT NULL DEFAULT 0 CHECK(packaging_quantity>=0), returned_on TEXT NOT NULL,
            revenue_cny TEXT NOT NULL, cogs_cny TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sales_return_layers (
            id TEXT PRIMARY KEY, sales_return_id TEXT NOT NULL REFERENCES sales_returns(id),
            sale_line_id TEXT NOT NULL REFERENCES sale_lines(id), source_movement_id TEXT NOT NULL REFERENCES inventory_movements(id),
            stock_layer_id TEXT NOT NULL REFERENCES stock_layers(id), sku TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
            cogs_cny TEXT NOT NULL, UNIQUE(sales_return_id,source_movement_id)
        );
        CREATE INDEX IF NOT EXISTS ix_sales_returns_line ON sales_returns(sale_line_id);
        CREATE INDEX IF NOT EXISTS ix_sales_return_layers_source ON sales_return_layers(source_movement_id);
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(24,?)", (datetime.now(timezone.utc).isoformat(),))
        sale_line_columns={row["name"] for row in conn.execute("PRAGMA table_info(sale_lines)")}
        if "source_row_key" not in sale_line_columns:
            conn.execute("ALTER TABLE sale_lines ADD COLUMN source_row_key TEXT")
        for line in conn.execute("SELECT l.id,i.source_key FROM sale_lines l JOIN sales_imports i ON i.id=l.import_id WHERE l.source_row_key IS NULL OR l.source_row_key='' ").fetchall():
            conn.execute("UPDATE sale_lines SET source_row_key=? WHERE id=?",(f"{line['source_key']}:legacy:{line['id']}",line["id"]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_sale_lines_source_row_key ON sale_lines(source_row_key)")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(25,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS expense_reconciliation_sources (
            id TEXT PRIMARY KEY, period TEXT NOT NULL, source_key TEXT NOT NULL, source_name TEXT NOT NULL,
            source_total TEXT NOT NULL, evidence_ref TEXT NOT NULL, scope_note TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            UNIQUE(period,source_key)
        );
        CREATE TABLE IF NOT EXISTS expense_source_links (
            expense_id TEXT PRIMARY KEY REFERENCES expenses(id), source_id TEXT NOT NULL REFERENCES expense_reconciliation_sources(id),
            amount TEXT NOT NULL, linked_at TEXT NOT NULL, linked_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS expense_reconciliation_requests (
            source_key TEXT PRIMARY KEY, action_type TEXT NOT NULL, payload TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS expense_reconciliation_source_history (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES expense_reconciliation_sources(id),
            old_payload TEXT NOT NULL, new_payload TEXT NOT NULL, action_key TEXT NOT NULL,
            changed_at TEXT NOT NULL, changed_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS expense_source_link_history (
            id TEXT PRIMARY KEY, expense_id TEXT NOT NULL REFERENCES expenses(id), old_source_id TEXT,
            new_source_id TEXT NOT NULL, action_key TEXT NOT NULL, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS expense_reconciliation_coverage (
            period TEXT PRIMARY KEY, source_count INTEGER NOT NULL, expense_count INTEGER NOT NULL,
            note TEXT NOT NULL, confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS expense_reconciliation_coverage_history (
            id TEXT PRIMARY KEY, period TEXT NOT NULL, old_payload TEXT NOT NULL, new_payload TEXT NOT NULL,
            action_key TEXT NOT NULL, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_expense_recon_source_period ON expense_reconciliation_sources(period);
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(26,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("UPDATE journal_entries SET business_line='coffee',entity_code='main' WHERE source_type='sale' AND business_line='unassigned'")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS platform_income_reconciliations (
            id TEXT PRIMARY KEY, period TEXT NOT NULL, platform TEXT NOT NULL, store_name TEXT NOT NULL,
            gross_amount TEXT NOT NULL, refund_amount TEXT NOT NULL, subsidy_amount TEXT NOT NULL,
            platform_fee_amount TEXT NOT NULL, settlement_amount TEXT NOT NULL,
            bank_received_amount TEXT, declared_sales_amount TEXT, evidence_ref TEXT NOT NULL,
            note TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            UNIQUE(period,platform,store_name)
        );
        CREATE TABLE IF NOT EXISTS platform_income_reconciliation_history (
            id TEXT PRIMARY KEY, reconciliation_id TEXT NOT NULL REFERENCES platform_income_reconciliations(id),
            old_payload TEXT NOT NULL, new_payload TEXT NOT NULL, action_key TEXT NOT NULL UNIQUE,
            changed_at TEXT NOT NULL, changed_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS platform_income_reconciliation_requests (
            action_key TEXT PRIMARY KEY, payload TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_platform_income_period ON platform_income_reconciliations(period);
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(27,?)", (datetime.now(timezone.utc).isoformat(),))
        purchase_columns={row["name"] for row in conn.execute("PRAGMA table_info(purchases)")}
        if "document_amount" not in purchase_columns:
            conn.execute("ALTER TABLE purchases ADD COLUMN document_amount TEXT NOT NULL DEFAULT '0.00'")
            for row in conn.execute("SELECT id,ordered_qty,unit_cost FROM purchases"):
                doc_amount=(Decimal(row["unit_cost"])*int(row["ordered_qty"])).quantize(CENT,rounding=ROUND_HALF_UP)
                conn.execute("UPDATE purchases SET document_amount=? WHERE id=?",(str(doc_amount),row["id"]))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(28,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS purchase_receipt_edit_history (
            id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL REFERENCES purchase_receipts(id),
            old_quantity INTEGER NOT NULL, new_quantity INTEGER NOT NULL,
            action_key TEXT NOT NULL UNIQUE, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL
        );
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(29,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS expense_project_allocation_reversals (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
            allocation_id TEXT NOT NULL REFERENCES expense_project_allocations(id),
            amount TEXT NOT NULL, reversed_on TEXT NOT NULL, changed_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_expense_allocation_reversals_allocation ON expense_project_allocation_reversals(allocation_id);
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(30,?)", (datetime.now(timezone.utc).isoformat(),))
        for table in ("invoices", "bank_transactions"):
            columns={row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "source_system" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN source_system TEXT NOT NULL DEFAULT ''")
            if "source_document_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN source_document_id TEXT NOT NULL DEFAULT ''")
        conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_external_source ON invoices(source_system,source_document_id) WHERE source_system!='' AND source_document_id!='';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_bank_external_source ON bank_transactions(source_system,source_document_id) WHERE source_system!='' AND source_document_id!='';
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(31,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS financial_action_requests (
            action_key TEXT PRIMARY KEY, action_type TEXT NOT NULL,
            payload TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(32,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(33,?)", (datetime.now(timezone.utc).isoformat(),))
        sale_columns={row["name"] for row in conn.execute("PRAGMA table_info(sale_lines)")}
        if "channel" not in sale_columns:conn.execute("ALTER TABLE sale_lines ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(34,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("CREATE TABLE IF NOT EXISTS manual_journal_requests (source_key TEXT PRIMARY KEY,payload TEXT NOT NULL,journal_entry_id TEXT NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)")
        journal_columns={row["name"] for row in conn.execute("PRAGMA table_info(journal_entries)")}
        if "memo" not in journal_columns:conn.execute("ALTER TABLE journal_entries ADD COLUMN memo TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(35,?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("CREATE TABLE IF NOT EXISTS inventory_adjustments (id TEXT PRIMARY KEY,source_key TEXT NOT NULL UNIQUE,sku TEXT NOT NULL,item_type TEXT NOT NULL CHECK(item_type IN ('finished_goods','packaging')),direction TEXT NOT NULL CHECK(direction IN ('gain','loss')),quantity INTEGER NOT NULL CHECK(quantity>0),unit_cost TEXT NOT NULL,currency TEXT NOT NULL,exchange_rate TEXT NOT NULL,adjusted_on TEXT NOT NULL,reason TEXT NOT NULL,business_line TEXT NOT NULL,entity_code TEXT NOT NULL,project_code TEXT,stock_layer_id TEXT REFERENCES stock_layers(id),journal_entry_id TEXT REFERENCES journal_entries(id),created_at TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(36,?)", (datetime.now(timezone.utc).isoformat(),))
        layer_columns={row["name"] for row in conn.execute("PRAGMA table_info(stock_layers)")}
        for column,declaration in {"adjustment_id":"TEXT REFERENCES inventory_adjustments(id)","currency":"TEXT NOT NULL DEFAULT 'UNKNOWN'","exchange_rate":"TEXT NOT NULL DEFAULT '1'"}.items():
            if column not in layer_columns:conn.execute(f"ALTER TABLE stock_layers ADD COLUMN {column} {declaration}")
        conn.execute("UPDATE stock_layers SET currency=COALESCE((SELECT currency FROM purchases WHERE purchases.id=stock_layers.purchase_id),currency)")
        conn.execute("UPDATE stock_layers SET exchange_rate=COALESCE((SELECT exchange_rate FROM purchases WHERE purchases.id=stock_layers.purchase_id),exchange_rate)")
        original_archive.ensure_tables(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(37,?)", (now(),))
        ensure_tax_history_period_columns(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(38,?)", (now(),))
        exhibition_assets.migrate(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(39,?)", (now(),))
        if getattr(conn, "is_postgres", False):
            ensure_postgres_triggers(conn)
            conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def journal(conn: sqlite3.Connection, source_type: str, source_id: str, posted_on: str, lines: list[tuple[str, Decimal, Decimal]], business_line: str = "unassigned", entity_code: str = "unassigned", project_code: str | None = None, currency: str = "CNY", line_dimensions: list[tuple[str, str, str | None]] | None = None) -> None:
    if sum((d for _, d, _ in lines), Decimal(0)) != sum((c for _, _, c in lines), Decimal(0)):
        raise ValueError("journal entry is not balanced")
    if line_dimensions is not None and len(line_dimensions) != len(lines):
        raise ValueError("journal line dimensions must match the number of lines")
    entry_id = str(uuid4())
    conn.execute("INSERT INTO journal_entries (id,source_type,source_id,posted_on,created_at,business_line,entity_code,project_code,currency) VALUES(?,?,?,?,?,?,?,?,?)", (entry_id, source_type, source_id, posted_on, now(), business_line, entity_code, project_code, currency))
    for index, (account, debit, credit) in enumerate(lines):
        line_business, line_entity, line_project = line_dimensions[index] if line_dimensions is not None else (business_line, entity_code, project_code)
        conn.execute("INSERT INTO journal_lines (id,entry_id,account,debit,credit,business_line,entity_code,project_code) VALUES(?,?,?,?,?,?,?,?)", (str(uuid4()), entry_id, account, str(debit), str(credit), line_business, line_entity, line_project))


def ensure_open(conn: sqlite3.Connection, day: str) -> None:
    try:
        if len(day) != 10:
            raise ValueError
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError("business date must use YYYY-MM-DD format")
    opening=conn.execute("SELECT as_of FROM opening_balance_imports LIMIT 1").fetchone()
    if opening and day<=opening["as_of"]:
        raise ValueError(f"business date must be after the imported opening balance date ({opening['as_of']})")
    period = day[:7]
    if conn.execute("SELECT 1 FROM period_locks WHERE period=?", (period,)).fetchone():
        raise ValueError(f"accounting period {period} is closed")


def expense_reconciliation_status(conn:sqlite3.Connection,period:str)->dict:
    try:
        if len(period)!=7:raise ValueError
        datetime.strptime(period,"%Y-%m")
    except ValueError:raise ValueError("period must use YYYY-MM format")
    expense_rows=conn.execute("SELECT e.id,e.incurred_on,e.category,e.counterparty,e.amount,e.memo,e.business_line,e.entity_code,e.project_code,l.source_id,s.source_name FROM expenses e LEFT JOIN expense_source_links l ON l.expense_id=e.id LEFT JOIN expense_reconciliation_sources s ON s.id=l.source_id WHERE e.incurred_on LIKE ? ORDER BY e.incurred_on,e.created_at,e.id",(period+"-%",)).fetchall()
    expenses=[dict(row) for row in expense_rows]
    source_rows=conn.execute("SELECT * FROM expense_reconciliation_sources WHERE period=? ORDER BY source_name,source_key",(period,)).fetchall()
    sources=[];mismatched=0
    for source in source_rows:
        linked=sum((Decimal(row[0]) for row in conn.execute("SELECT l.amount FROM expense_source_links l JOIN expenses e ON e.id=l.expense_id WHERE l.source_id=? AND e.incurred_on LIKE ?",(source["id"],period+"-%"))),Decimal(0)).quantize(CENT)
        expected=Decimal(source["source_total"]).quantize(CENT);difference=(expected-linked).quantize(CENT)
        reconciled=difference==0
        if not reconciled:mismatched+=1
        sources.append({**dict(source),"linked_total":str(linked),"difference":str(difference),"reconciled":reconciled})
    unlinked=[row for row in expenses if not row["source_id"]]
    invalid_links=sum(1 for row in conn.execute("SELECT l.amount AS linked_amount,e.amount AS expense_amount FROM expenses e JOIN expense_source_links l ON l.expense_id=e.id WHERE e.incurred_on LIKE ?",(period+"-%",)) if Decimal(row["linked_amount"])!=Decimal(row["expense_amount"]))
    # Expense amounts are immutable after posting; this count protects against stale link snapshots if that policy changes.
    coverage=conn.execute("SELECT * FROM expense_reconciliation_coverage WHERE period=?",(period,)).fetchone()
    source_count=len(source_rows);expense_count=len(expense_rows)
    source_changed_after_confirmation=bool(coverage and conn.execute("SELECT 1 FROM expense_reconciliation_sources WHERE period=? AND updated_at>? LIMIT 1",(period,coverage["confirmed_at"])).fetchone())
    coverage_current=bool(coverage and int(coverage["source_count"])==source_count and int(coverage["expense_count"])==expense_count and not source_changed_after_confirmation)
    complete=coverage_current and not unlinked and mismatched==0 and invalid_links==0
    return {"period":period,"source_count":source_count,"sources":sources,"expense_count":expense_count,"expenses":expenses,"unlinked_expense_count":len(unlinked),"unlinked_expenses":unlinked,"mismatched_source_count":mismatched,"coverage_confirmed":coverage_current,"coverage_stale":bool(coverage and not coverage_current),"coverage_note":coverage["note"] if coverage else None,"confirmed_by":coverage["confirmed_by"] if coverage_current else None,"confirmed_at":coverage["confirmed_at"] if coverage_current else None,"complete":bool(complete),"authenticated":False,"scope":"按手工登记的外部来源清单与逐笔费用关联核对；来源清单完整性及确认人由操作者自行声明，未接入身份认证或外部费用系统。"}


def platform_income_status(conn:sqlite3.Connection,period:str)->dict:
    try:
        if len(period)!=7:raise ValueError
        datetime.strptime(period,"%Y-%m")
    except ValueError:raise ValueError("period must use YYYY-MM format")
    rows=conn.execute("SELECT * FROM platform_income_reconciliations WHERE period=? ORDER BY platform,store_name",(period,)).fetchall()
    records=[]
    totals={key:Decimal(0) for key in ("gross_amount","refund_amount","subsidy_amount","platform_fee_amount","settlement_amount","bank_received_amount","declared_sales_amount","platform_net_amount","expected_settlement_amount","settlement_difference","bank_difference","declaration_difference")};bank_received_count=0;declared_sales_count=0
    for row in rows:
        gross=Decimal(row["gross_amount"]);refund=Decimal(row["refund_amount"]);subsidy=Decimal(row["subsidy_amount"]);fee=Decimal(row["platform_fee_amount"]);settlement=Decimal(row["settlement_amount"])
        net=(gross+subsidy-refund).quantize(CENT);expected=(net-fee).quantize(CENT)
        bank=Decimal(row["bank_received_amount"]) if row["bank_received_amount"] is not None else None
        declared=Decimal(row["declared_sales_amount"]) if row["declared_sales_amount"] is not None else None
        enriched={**dict(row),"platform_net_amount":str(net),"expected_settlement_amount":str(expected),"settlement_difference":str((expected-settlement).quantize(CENT)),"bank_difference":str((settlement-bank).quantize(CENT)) if bank is not None else None,"declaration_difference":str((net-declared).quantize(CENT)) if declared is not None else None}
        records.append(enriched)
        for key in ("gross_amount","refund_amount","subsidy_amount","platform_fee_amount","settlement_amount"):
            totals[key]+=Decimal(row[key])
        totals["bank_received_amount"]+=bank or Decimal(0);totals["declared_sales_amount"]+=declared or Decimal(0);bank_received_count+=int(bank is not None);declared_sales_count+=int(declared is not None)
        for key,value in (("platform_net_amount",net),("expected_settlement_amount",expected),("settlement_difference",expected-settlement),("bank_difference",settlement-bank if bank is not None else Decimal(0)),("declaration_difference",net-declared if declared is not None else Decimal(0))):totals[key]+=value
    accounting_rows=conn.execute("SELECT l.debit,l.credit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.posted_on LIKE ? AND l.account='sales_revenue'",(period+"-%",)).fetchall()
    accounting_revenue=sum((Decimal(row["credit"])-Decimal(row["debit"]) for row in accounting_rows),Decimal(0)).quantize(CENT)
    return {"period":period,"records":records,"totals":{key:str(value.quantize(CENT)) for key,value in totals.items()},"accounting_sales_revenue_cny":str(accounting_revenue),"record_count":len(records),"bank_received_count":bank_received_count,"declared_sales_count":declared_sales_count,"scope":"按手工录入的平台账单汇总核对；收入总额、退款、补贴、手续费与结算应分别对照平台原始报表。平台净额、账内收入和税务申报口径可能因含税/不含税及确认期间不同而存在差异，本表不自动判定申报正确性。"}


def replay_expense_reconciliation_request(conn:sqlite3.Connection,source_key:str,action_type:str,payload:dict)->dict|None:
    prior=conn.execute("SELECT action_type,payload,response FROM expense_reconciliation_requests WHERE source_key=?",(source_key,)).fetchone()
    if not prior:return None
    serialized=json.dumps(payload,ensure_ascii=False,sort_keys=True)
    if prior["action_type"]!=action_type or prior["payload"]!=serialized:
        raise ValueError("source_key is already recorded with a different expense reconciliation action")
    return {**json.loads(prior["response"]),"duplicate":True}


def save_expense_reconciliation_request(conn:sqlite3.Connection,source_key:str,action_type:str,payload:dict,response:dict)->None:
    conn.execute("INSERT INTO expense_reconciliation_requests VALUES(?,?,?,?,?)",(source_key,action_type,json.dumps(payload,ensure_ascii=False,sort_keys=True),json.dumps(response,ensure_ascii=False),now()))


PERIOD_REVIEW_CHECKS = [
    {"key":"bank_reconciled","label":"银行流水","detail":"已确认企业银行账户清单完整，逐户核对本期银行对账单、未匹配余额和暂挂项目"},
    {"key":"inventory_reconciled","label":"存货核对","detail":"已核对实物库存、系统数量与期末存货金额"},
    {"key":"open_items_reviewed","label":"应收应付","detail":"已核对期末往来余额、账龄及外币估值"},
    {"key":"expenses_reviewed","label":"费用完整性","detail":"已核对报销、合同、费用归属与项目分摊"},
    {"key":"tax_reviewed","label":"发票税务","detail":"已复核本期发票及税务用途状态"},
    {"key":"fx_reviewed","label":"外币与汇兑","detail":"已复核外币账面值、结算汇兑及期间汇率"},
]


def period_close_readiness(conn: sqlite3.Connection, period: str) -> dict:
    entries=conn.execute("SELECT e.id,e.currency,l.debit,l.credit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.posted_on LIKE ?",(f"{period}-%",)).fetchall()
    totals:dict[str,list[Decimal]]={}
    for row in entries:
        total=totals.setdefault(row["id"],[Decimal(0),Decimal(0)])
        total[0]+=Decimal(row["debit"]);total[1]+=Decimal(row["credit"])
    unbalanced_ids=[entry_id for entry_id,(debit,credit) in totals.items() if debit!=credit]
    unbalanced=len(unbalanced_ids)
    bank_rows=conn.execute("SELECT * FROM bank_transactions WHERE transaction_on LIKE ?",(f"{period}-%",)).fetchall()
    unallocated=0
    unclassified=0
    unallocated_details=[]
    for bank in bank_rows:
        allocations=conn.execute("SELECT amount,target_type FROM bank_allocations WHERE bank_transaction_id=?",(bank["id"],)).fetchall()
        allocated=sum((Decimal(row["amount"]) for row in allocations),Decimal(0))
        remaining=Decimal(bank["amount"])-allocated
        if remaining>0:
            unallocated+=1
            if len(unallocated_details)<10:
                unallocated_details.append({"date":bank["transaction_on"],"reference":bank["bank_reference"],"description":bank["description"],"currency":bank["currency"],"remaining":str(remaining)})
        unclassified+=sum(1 for row in allocations if row["target_type"]=="unclassified")
    unvalued_bank=sum(1 for row in bank_rows if row["currency"]!="CNY" and not row["base_amount_cny"])
    unvalued_open=0
    unvalued_open_details=[]
    for row in conn.execute("SELECT r.id,r.currency,r.source_type,r.counterparty,r.original_amount,r.settled_amount,f.item_id FROM receivables_payables r LEFT JOIN open_item_fx f ON f.item_id=r.id WHERE r.currency!='CNY'"):
        if Decimal(row["original_amount"])>Decimal(row["settled_amount"]) and row["item_id"] is None:
            unvalued_open+=1
            if len(unvalued_open_details)<10:
                unvalued_open_details.append({"counterparty":row["counterparty"],"source_type":row["source_type"],"currency":row["currency"],"outstanding":str((Decimal(row["original_amount"])-Decimal(row["settled_amount"])).quantize(CENT))})
    invoice_rows=conn.execute("SELECT tax_status FROM invoices WHERE issued_on LIKE ?",(f"{period}-%",)).fetchall()
    pending_tax=sum(1 for row in invoice_rows if row["tax_status"]=="pending")
    expense_rows=conn.execute("SELECT id,amount,project_code FROM expenses WHERE incurred_on LIKE ?",(f"{period}-%",)).fetchall()
    expense_reconciliation=expense_reconciliation_status(conn,period)
    expense_total=sum((Decimal(row["amount"]) for row in expense_rows),Decimal(0)).quantize(CENT)
    allocated_by_expense:dict[str,Decimal]={}
    for row in conn.execute("SELECT a.expense_id,a.amount,COALESCE(SUM(r.amount),'0.00') AS reversed_amount FROM expense_project_allocations a LEFT JOIN expense_project_allocation_reversals r ON r.allocation_id=a.id GROUP BY a.id"):
        net_amount=Decimal(row["amount"])-Decimal(str(row["reversed_amount"]))
        allocated_by_expense[row["expense_id"]]=allocated_by_expense.get(row["expense_id"],Decimal(0))+net_amount
    unassigned_expenses=sum(1 for row in expense_rows if not row["project_code"] and Decimal(row["amount"])>allocated_by_expense.get(row["id"],Decimal(0)))
    stock_rows=conn.execute("SELECT COALESCE(p.currency,s.currency) AS currency,s.remaining_qty,s.unit_cost FROM stock_layers s LEFT JOIN purchases p ON p.id=s.purchase_id WHERE s.remaining_qty>0").fetchall()
    stock_by_currency:dict[str,Decimal]={}
    for row in stock_rows:
        stock_by_currency[row["currency"]]=stock_by_currency.get(row["currency"],Decimal(0))+Decimal(row["remaining_qty"])*Decimal(row["unit_cost"])
    active_open_items=sum(1 for row in conn.execute("SELECT original_amount,settled_amount FROM receivables_payables") if Decimal(row["original_amount"])>Decimal(row["settled_amount"]))
    fx_rows=conn.execute("SELECT l.account,l.debit,l.credit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.posted_on LIKE ? AND l.account IN ('realized_fx_gain','realized_fx_loss')",(f"{period}-%",)).fetchall()
    fx_gain=sum((Decimal(row["credit"])-Decimal(row["debit"]) for row in fx_rows if row["account"]=="realized_fx_gain"),Decimal(0))
    fx_loss=sum((Decimal(row["debit"])-Decimal(row["credit"]) for row in fx_rows if row["account"]=="realized_fx_loss"),Decimal(0))
    bank_accounts=conn.execute("SELECT * FROM bank_accounts WHERE active=1").fetchall()
    unreconciled_bank_accounts=0
    for account in bank_accounts:
        statement=conn.execute("SELECT * FROM bank_statement_reconciliations WHERE bank_account_id=? AND period=?",(account["id"],period)).fetchone()
        if not statement:
            unreconciled_bank_accounts+=1;continue
        account_rows=conn.execute("SELECT direction,amount FROM bank_transactions WHERE account_name=? AND transaction_on LIKE ?",(account["account_name"],f"{period}-%")).fetchall()
        inflow=sum((Decimal(row["amount"]) for row in account_rows if row["direction"]=="in"),Decimal(0))
        outflow=sum((Decimal(row["amount"]) for row in account_rows if row["direction"]=="out"),Decimal(0))
        if (Decimal(statement["opening_balance"])+inflow-outflow-Decimal(statement["closing_balance"])).quantize(CENT)!=0:
            unreconciled_bank_accounts+=1
    coverage=conn.execute("SELECT account_count FROM bank_account_coverage WHERE period=?",(period,)).fetchone()
    bank_coverage_required=bool(bank_accounts or bank_rows)
    bank_account_list_unconfirmed=bank_coverage_required and (coverage is None or coverage["account_count"]!=len(bank_accounts))
    blockers=[]
    for key,label,count,action in (("unbalanced_journals","借贷不平凭证",unbalanced,"journals"),("unallocated_bank_rows","未分配银行流水",unallocated,"bank"),("unvalued_foreign_bank_rows","缺少人民币估值的外币流水",unvalued_bank,"bank"),("unvalued_foreign_open_items","缺少人民币账面值的未结外币往来",unvalued_open,"items")):
        if count:
            blockers.append({"key":key,"label":label,"count":count,"action":action})
    if unreconciled_bank_accounts:
        blockers.append({"key":"unreconciled_bank_accounts","label":"银行账户未核平","count":unreconciled_bank_accounts,"action":"bank"})
    if bank_account_list_unconfirmed:
        blockers.append({"key":"bank_account_list_unconfirmed","label":"企业银行账户清单未确认完整","count":1,"action":"bank"})
    if not expense_reconciliation["complete"]:
        expense_gap=max(1,expense_reconciliation["unlinked_expense_count"]+expense_reconciliation["mismatched_source_count"]+int(not expense_reconciliation["coverage_confirmed"]))
        blockers.append({"key":"expense_reconciliation_incomplete","label":"费用来源清单或金额尚未核平","count":expense_gap,"action":"expenses"})
    review_items=[
        {"key":"bank_reconciled","label":"银行流水","action":"bank","action_label":"打开银行流水","status":"blocking" if unallocated or unreconciled_bank_accounts or bank_account_list_unconfirmed else "attention" if unclassified else "clear" if bank_coverage_required else "manual","summary":f"本期导入 {len(bank_rows)} 笔；未分配 {unallocated} 笔；暂挂待分类 {unclassified} 笔；账户未核平 {unreconciled_bank_accounts} 个；完整性清单{'已确认' if not bank_account_list_unconfirmed and bank_coverage_required else '未确认'}。","details":unallocated_details},
        {"key":"inventory_reconciled","label":"存货核对","action":"inventory","action_label":"打开库存管理","status":"manual","summary":"系统当前库存账面余额（按币种分列）；实物盘点差异需人工核对。","balances_by_currency":{currency:str(value.quantize(CENT)) for currency,value in sorted(stock_by_currency.items())}},
        {"key":"open_items_reviewed","label":"应收应付","action":"items","action_label":"打开应收应付","status":"blocking" if unvalued_open else "manual","summary":f"尚有未结往来 {active_open_items} 笔；缺人民币账面值的外币往来 {unvalued_open} 笔。","details":unvalued_open_details},
        {"key":"expenses_reviewed","label":"费用完整性","action":"expenses","action_label":"打开经营费用","status":"blocking" if not expense_reconciliation["complete"] else "attention" if unassigned_expenses else "manual","summary":f"外部来源 {expense_reconciliation['source_count']} 个；已登记费用 {expense_reconciliation['expense_count']} 笔，其中未关联 {expense_reconciliation['unlinked_expense_count']} 笔、来源差额 {expense_reconciliation['mismatched_source_count']} 个；来源清单{'已确认' if expense_reconciliation['coverage_confirmed'] else '未确认或已过期'}。本期费用 CNY {expense_total}；未直接指定项目 {unassigned_expenses} 笔。"},
        {"key":"tax_reviewed","label":"发票税务","action":"invoices","action_label":"打开发票税务","status":"attention" if pending_tax else "clear","summary":f"本期发票 {len(invoice_rows)} 张；税务状态待复核 {pending_tax} 张。发票录入不代表税局用途确认。"},
        {"key":"fx_reviewed","label":"外币与汇兑","action":"items","action_label":"打开汇率复核","status":"blocking" if unvalued_bank or unvalued_open else "attention" if fx_gain or fx_loss else "manual","summary":f"缺人民币估值外币流水 {unvalued_bank} 笔；缺估值未结外币往来 {unvalued_open} 笔；本期已实现汇兑损益 CNY {(fx_gain-fx_loss).quantize(CENT)}。"},
    ]
    return {"period":period,"journal_count":len(totals),"unbalanced_journals":unbalanced,"unbalanced_journal_ids":unbalanced_ids[:10],"bank_rows":len(bank_rows),"unallocated_bank_rows":unallocated,"unclassified_bank_allocations":unclassified,"unvalued_foreign_bank_rows":unvalued_bank,"unvalued_foreign_open_items":unvalued_open,"registered_bank_accounts":len(bank_accounts),"unreconciled_bank_accounts":unreconciled_bank_accounts,"bank_account_list_unconfirmed":bank_account_list_unconfirmed,"expense_reconciliation_complete":expense_reconciliation["complete"],"expense_reconciliation":expense_reconciliation,"pending_tax_invoices":pending_tax,"blockers":blockers,"can_close":not blockers,"checks_required":PERIOD_REVIEW_CHECKS,"review_items":review_items}


class Api(BaseHTTPRequestHandler):
    server_version = "WorkOSLocal/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def reply(self, status: int, payload: object, cookie: str | None = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:5175")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Idempotency-Key")
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        if cookie:
            self.send_header("Set-Cookie",cookie)
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 2_000_000:
            raise ValueError("request body is missing or too large")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def _session_valid(self) -> bool:
        # A malicious web page must not be able to POST to a localhost financial API.
        host_header=self.headers.get("Host","").split(":")[0].strip().lower()
        if host_header not in ("127.0.0.1","localhost"):
            self.reply(403,{"error":"invalid local request host"})
            return False
        if not local_auth.authenticated(self.headers):
            self.reply(401,{"error":"login required","code":"WORKOS_AUTH_REQUIRED"})
            return False
        return True

    def do_OPTIONS(self) -> None:
        if not local_auth.origin_allowed(self.headers.get("Origin")):
            return self.reply(403,{"error":"request origin not allowed"})
        self.reply(204, {})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path=="/api/auth/status":
            return self.reply(200,{"requires_login":local_auth.enabled(),
                "authenticated":local_auth.authenticated(self.headers),
                "role":"owner" if local_auth.authenticated(self.headers) else None,
                "local_only":True})
        if not self._session_valid():
            return
        path = parsed.path
        if path == "/api/health":
            return self.reply(200, {"status": "ok", "database_backend": DB_BACKEND, "database": str(DB_PATH) if DB_BACKEND == "sqlite" else "postgres"})
        with db() as conn:
            if path == "/api/coffee/stock-cover":
                return self.reply(200,stock_cover(conn))
            if path == "/api/archive/files":
                rows=conn.execute(
                    "SELECT f.id,f.sha256,f.size_bytes,f.created_at,u.original_name,u.category,u.period "
                    "FROM archive_files f JOIN archive_uploads u ON u.file_id=f.id "
                    "ORDER BY u.uploaded_at DESC LIMIT 500"
                ).fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path == "/api/archive/links":
                source=parse_qs(parsed.query).get("source_system",[""])[0]
                source_id=parse_qs(parsed.query).get("source_document_id",[""])[0]
                if not source or not source_id:
                    return self.reply(422,{"error":"source_system and source_document_id required"})
                rows=conn.execute(
                    "SELECT l.file_id,l.linked_at,f.size_bytes,u.original_name "
                    "FROM archive_links l JOIN archive_files f ON f.id=l.file_id "
                    "JOIN archive_uploads u ON u.file_id=f.id "
                    "WHERE l.source_system=? AND l.source_document_id=? "
                    "GROUP BY l.file_id,l.linked_at,f.size_bytes,u.original_name",
                    (source,source_id)).fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path.startswith("/api/archive/files/") and path.endswith("/download"):
                file_id=path.split("/")[4]
                metadata=original_archive.fetch_file_metadata(conn,file_id)
                if not metadata: return self.reply(404,{"error":"archive file not found"})
                try:
                    archive_path=original_archive.open_verified_file(file_id, metadata["storage_key"])
                except (ValueError,OSError) as exc:
                    return self.reply(422,{"error":str(exc)})
                self.send_response(200)
                self.send_header("Content-Type","application/octet-stream")
                self.send_header("Content-Disposition",original_archive.download_disposition(metadata["original_name"]))
                self.send_header("Content-Length",str(metadata["size_bytes"]))
                self.send_header("Cache-Control","no-store")
                self.send_header("X-Content-Type-Options","nosniff")
                self.end_headers()
                with archive_path.open("rb") as original:
                    while chunk:=original.read(1024*1024):
                        self.wfile.write(chunk)
                return
            if path == "/api/accounts":
                rows=conn.execute("SELECT account_code,account_name,account_type,normal_side,statement,active FROM chart_of_accounts ORDER BY account_code").fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path == "/api/journals/adjustments":
                rows=conn.execute("SELECT r.source_key,e.id,e.posted_on,e.business_line,e.entity_code,e.project_code,e.currency FROM manual_journal_requests r JOIN journal_entries e ON e.id=r.journal_entry_id ORDER BY e.posted_on DESC,e.created_at DESC").fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path == "/api/reports/balance-sheet":
                as_of=parse_qs(parsed.query).get("as_of",[date.today().isoformat()])[0]
                try:
                    if len(as_of)!=10: raise ValueError
                    datetime.strptime(as_of,"%Y-%m-%d")
                except ValueError:
                    return self.reply(422,{"error":"as_of must use YYYY-MM-DD format"})
                balances:dict[str,list[Decimal]]={}
                for row in conn.execute("SELECT l.account,l.debit,l.credit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.posted_on<=? AND e.currency='CNY'",(as_of,)):
                    pair=balances.setdefault(row["account"],[Decimal(0),Decimal(0)])
                    pair[0]+=Decimal(row["debit"]);pair[1]+=Decimal(row["credit"])
                classified={"asset":[],"liability":[],"equity":[]};suspense=[];unmapped=[];current_earnings=Decimal(0)
                for account,(debit,credit) in sorted(balances.items()):
                    meta=conn.execute("SELECT account_name,account_type,normal_side,statement FROM chart_of_accounts WHERE account_code=?",(account,)).fetchone()
                    net=(debit-credit) if not meta or meta["normal_side"]=="debit" else (credit-debit)
                    if not meta or meta["statement"]=="unmapped" or meta["account_type"]=="unclassified":
                        unmapped.append({"account":account,"balance":str(net.quantize(CENT))});continue
                    record={"account":account,"name":meta["account_name"],"balance":str(net.quantize(CENT))}
                    kind=meta["account_type"]
                    if kind in classified: classified[kind].append(record)
                    elif kind=="suspense": suspense.append(record)
                    elif meta["statement"]=="income": current_earnings+=net if kind=="revenue" else -net
                sum_group=lambda group:sum((Decimal(x["balance"]) for x in group),Decimal(0)).quantize(CENT)
                assets=sum_group(classified["asset"]);liabilities=sum_group(classified["liability"]);equity=sum_group(classified["equity"]);earnings=current_earnings.quantize(CENT)
                difference=(assets-liabilities-equity-earnings).quantize(CENT)
                opening=conn.execute("SELECT as_of FROM opening_balance_imports LIMIT 1").fetchone()
                def source_date(item: sqlite3.Row) -> str | None:
                    lookups={
                        "purchase_receipt":("purchase_receipts","received_on"),"operating_expense":("expenses","incurred_on"),
                        "bike_landed_cost":("bike_landed_costs","incurred_on"),"bike_sale":("bike_sales","sold_on"),
                        "sale":("sales_imports","sold_on"),"legacy_sale":("sales_imports","sold_on"),
                        "bike_receipt":("bike_receipts","received_on"),"bike_milestone":("bike_milestones","recognized_on"),
                    }
                    if item["source_type"]=="legacy_purchase":return None
                    lookup=lookups.get(item["source_type"])
                    if not lookup:return None
                    found=conn.execute(f"SELECT {lookup[1]} FROM {lookup[0]} WHERE id=?",(item["source_id"],)).fetchone()
                    return found[0] if found and found[0] else None
                settled_by_item:dict[str,Decimal]={};unvalued_settlements=0
                for settlement in conn.execute("SELECT a.target_id,b.transaction_on,f.carrying_amount_cny FROM bank_allocations a JOIN bank_transactions b ON b.id=a.bank_transaction_id LEFT JOIN bank_allocation_fx f ON f.allocation_id=a.id WHERE a.target_type IN ('receivable','payable')"):
                    if settlement["transaction_on"]<=as_of:
                        if settlement["carrying_amount_cny"] is None:unvalued_settlements+=1
                        else:settled_by_item[settlement["target_id"]]=settled_by_item.get(settlement["target_id"],Decimal(0))+Decimal(settlement["carrying_amount_cny"])
                subledger_open={"receivable":Decimal(0),"payable":Decimal(0)};untraceable_items=0;unvalued_items=0
                for item in conn.execute("SELECT r.id,r.direction,r.source_type,r.source_id,r.currency,f.original_amount_cny FROM receivables_payables r LEFT JOIN open_item_fx f ON f.item_id=r.id"):
                    recognized=source_date(item)
                    if not recognized or recognized>as_of: 
                        if not recognized:untraceable_items+=1
                        continue
                    if item["original_amount_cny"] is None:unvalued_items+=1;continue
                    outstanding=Decimal(item["original_amount_cny"])-settled_by_item.get(item["id"],Decimal(0))
                    subledger_open[item["direction"]]+=outstanding
                ar_gl=balances.get("accounts_receivable",[Decimal(0),Decimal(0)])[0]-balances.get("accounts_receivable",[Decimal(0),Decimal(0)])[1]
                ap_gl=balances.get("accounts_payable",[Decimal(0),Decimal(0)])[1]-balances.get("accounts_payable",[Decimal(0),Decimal(0)])[0]
                ar_difference=(ar_gl-subledger_open["receivable"]).quantize(CENT);ap_difference=(ap_gl-subledger_open["payable"]).quantize(CENT)
                coffee_value=Decimal(0);unvalued_inventory=conn.execute("SELECT COUNT(*) FROM inventory_movements WHERE effective_on IS NULL OR (effective_on<=? AND base_unit_cost_cny IS NULL)",(as_of,)).fetchone()[0]
                for movement in conn.execute("SELECT quantity,base_unit_cost_cny FROM inventory_movements WHERE effective_on<=? AND base_unit_cost_cny IS NOT NULL",(as_of,)):
                    if movement["base_unit_cost_cny"] is None:unvalued_inventory+=1
                    else:coffee_value+=Decimal(movement["quantity"])*Decimal(movement["base_unit_cost_cny"])
                bike_receipts=sum((Decimal(row[0]) for row in conn.execute("SELECT contract_cost_cny FROM bike_receipts WHERE received_on<=?",(as_of,))),Decimal(0))
                bike_costs=sum((Decimal(row[0]) for row in conn.execute("SELECT base_amount_cny FROM bike_landed_costs WHERE incurred_on<=? AND cost_treatment='capitalized'",(as_of,))),Decimal(0))
                bike_cogs=sum((Decimal(row[0]) for row in conn.execute("SELECT cogs_cny FROM bike_sales WHERE sold_on<=?",(as_of,))),Decimal(0))
                outside_samples=exhibition_assets.outstanding_asset_value(conn,as_of)
                inventory_subledger=(coffee_value+outside_samples+bike_receipts+bike_costs-bike_cogs).quantize(CENT)
                inventory_gl=sum((balances.get(code,[Decimal(0),Decimal(0)])[0]-balances.get(code,[Decimal(0),Decimal(0)])[1] for code in ("inventory","inventory:bike","inventory:exhibition")),Decimal(0)).quantize(CENT)
                inventory_difference=(inventory_gl-inventory_subledger).quantize(CENT)
                has_balance_basis=bool(balances) or conn.execute("SELECT 1 FROM opening_balance_imports LIMIT 1").fetchone() is not None
                subledgers_reconciled=has_balance_basis and not (untraceable_items or unvalued_items or unvalued_settlements or unvalued_inventory) and abs(ar_difference)<=CENT and abs(ap_difference)<=CENT and abs(inventory_difference)<=CENT
                reconciliation={"as_of":as_of,"accounts_receivable":{"general_ledger":str(ar_gl.quantize(CENT)),"subledger":str(subledger_open["receivable"].quantize(CENT)),"difference":str(ar_difference)},"accounts_payable":{"general_ledger":str(ap_gl.quantize(CENT)),"subledger":str(subledger_open["payable"].quantize(CENT)),"difference":str(ap_difference)},"inventory":{"general_ledger":str(inventory_gl),"subledger":str(inventory_subledger),"difference":str(inventory_difference)},"untraceable_open_items":untraceable_items,"unvalued_open_items":unvalued_items,"unvalued_settlements":unvalued_settlements,"unvalued_inventory_movements":unvalued_inventory,"reconciled":subledgers_reconciled}
                classification_complete=not unmapped
                complete=opening is not None and classification_complete and not suspense and difference==0
                return self.reply(200,{"as_of":as_of,"currency":"CNY","assets":classified["asset"],"liabilities":classified["liability"],"equity":classified["equity"],"suspense":suspense,"unmapped_accounts":unmapped,"current_earnings":str(earnings),"total_assets":str(assets),"total_liabilities":str(liabilities),"total_equity":str(equity),"total_current_earnings":str(earnings),"liabilities_and_equity":str((liabilities+equity+earnings).quantize(CENT)),"difference":str(difference),"balanced":difference==0,"classification_complete":classification_complete,"subledger_reconciliation":reconciliation,"subledgers_reconciled":subledgers_reconciled,"complete":False,"opening_balances_as_of":opening["as_of"] if opening else None,"scope":"CNY 系统已过账总账；损益累计额计入权益。库存、应收应付按子账可追溯日期比对；外币未估值项、期初子账迁移及外部账簿仍未覆盖"})
            if path == "/api/sales/returns":
                result=[]
                lines=conn.execute("SELECT l.*,i.sold_on,i.source,i.business_line,i.entity_code,i.project_code,i.id AS import_id FROM sale_lines l JOIN sales_imports i ON i.id=l.import_id ORDER BY i.sold_on DESC,i.created_at DESC,l.source_row_key").fetchall()
                for line in lines:
                    returned=conn.execute("SELECT COALESCE(SUM(quantity),0) AS quantity,COALESCE(SUM(packaging_quantity),0) AS packaging_quantity FROM sales_returns WHERE sale_line_id=?",(line["id"],)).fetchone()
                    expected_package=0 if line["packaging_included"] else int(line["quantity"])*int(line["packaging_per_unit"])
                    product=conn.execute("SELECT COUNT(*) AS n,COALESCE(SUM(-quantity),0) AS quantity,COALESCE(SUM(stock_layer_id IS NULL),0) AS missing_layers,COALESCE(SUM(cogs_cny IS NULL),0) AS missing_cost FROM inventory_movements WHERE movement_type='sale' AND sale_line_id=? AND sku=?",(line["id"],line["sku"])).fetchone()
                    package=None
                    if expected_package:
                        package=conn.execute("SELECT COUNT(*) AS n,COALESCE(SUM(-quantity),0) AS quantity,COALESCE(SUM(stock_layer_id IS NULL),0) AS missing_layers,COALESCE(SUM(cogs_cny IS NULL),0) AS missing_cost FROM inventory_movements WHERE movement_type='sale' AND sale_line_id=? AND sku=?",(line["id"],line["packaging_sku"])).fetchone()
                    traceable=bool(product["n"] and int(product["quantity"])==int(line["quantity"]) and not product["missing_layers"] and not product["missing_cost"] and line["cogs_cny"] is not None)
                    if package:
                        traceable=traceable and int(package["quantity"])==expected_package and not package["missing_layers"] and not package["missing_cost"]
                    result.append({**dict(line),"returned_quantity":returned["quantity"],"returned_packaging_quantity":returned["packaging_quantity"],"returnable":traceable,"return_trace_note":None if traceable else "原销售行未保存完整的 FIFO 库存层成本关联，不能自动退货"})
                return self.reply(200,result)
            if path == "/api/sales/imports":
                rows = conn.execute("SELECT * FROM sales_imports ORDER BY created_at DESC").fetchall()
                return self.reply(200, [dict(row) for row in rows])
            if path == "/api/packaging-rules":
                rows = conn.execute("SELECT * FROM sku_packaging_rules ORDER BY sku").fetchall()
                return self.reply(200, [dict(row) for row in rows])
            if path == "/api/receivables-payables":
                rows = conn.execute("SELECT r.*,f.original_amount_cny,f.settled_amount_cny,f.exchange_rate AS book_exchange_rate,f.rate_date AS book_rate_date,f.rate_source AS book_rate_source FROM receivables_payables r LEFT JOIN open_item_fx f ON f.item_id=r.id ORDER BY r.created_at DESC").fetchall()
                return self.reply(200, [dict(row) for row in rows])
            if path == "/api/expenses":
                rows = conn.execute("SELECT * FROM expenses ORDER BY incurred_on DESC,created_at DESC").fetchall()
                allocations_by_expense: dict[str, Decimal] = {}
                allocation_details: dict[str, list[dict[str, str]]] = {}
                for allocation in conn.execute("SELECT a.id,a.expense_id,a.source_key,a.project_code,p.name AS project_name,a.business_line,a.amount,a.allocated_on,COALESCE(SUM(r.amount),0) AS reversed_amount FROM expense_project_allocations a JOIN projects p ON p.code=a.project_code LEFT JOIN expense_project_allocation_reversals r ON r.allocation_id=a.id GROUP BY a.id ORDER BY a.allocated_on,a.created_at"):
                    net_amount=Decimal(allocation["amount"])-Decimal(str(allocation["reversed_amount"]))
                    allocations_by_expense[allocation["expense_id"]] = allocations_by_expense.get(allocation["expense_id"], Decimal(0)) + net_amount
                    allocation_details.setdefault(allocation["expense_id"],[]).append({"id":allocation["id"],"source_key":allocation["source_key"],"project_code":allocation["project_code"],"project_name":allocation["project_name"],"business_line":allocation["business_line"],"amount":allocation["amount"],"reversed_amount":str(Decimal(str(allocation["reversed_amount"])).quantize(CENT)),"reversible_amount":str(net_amount.quantize(CENT)),"allocated_on":allocation["allocated_on"]})
                result=[]
                for row in rows:
                    allocated=allocations_by_expense.get(row["id"],Decimal(0)).quantize(CENT)
                    result.append({**dict(row),"allocated_amount":str(allocated),"unallocated_amount":str((Decimal(row["amount"])-allocated).quantize(CENT)),"allocations":allocation_details.get(row["id"],[])})
                return self.reply(200, result)
            if path == "/api/expenses/reconciliation":
                period=parse_qs(parsed.query).get("period",[date.today().strftime("%Y-%m")])[0]
                return self.reply(200,expense_reconciliation_status(conn,period))
            if path == "/api/platform-income/reconciliation":
                period=parse_qs(parsed.query).get("period",[date.today().strftime("%Y-%m")])[0]
                return self.reply(200,platform_income_status(conn,period))
            if path == "/api/projects":
                projects = []
                for project in conn.execute("SELECT * FROM projects ORDER BY starts_on DESC,code").fetchall():
                    budgets = [dict(row) for row in conn.execute("SELECT category,budget_amount FROM project_budgets WHERE project_code=? ORDER BY category", (project["code"],))]
                    actual_rows = conn.execute("SELECT l.account,l.debit,l.credit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE COALESCE(l.project_code,e.project_code)=? AND l.account LIKE 'operating_expense:%' ORDER BY l.account", (project["code"],)).fetchall()
                    actual_totals: dict[str, Decimal] = {}
                    for row in actual_rows:
                        category = row["account"].split(":",1)[1]
                        actual_totals[category] = actual_totals.get(category, Decimal(0)) + Decimal(row["debit"]) - Decimal(row["credit"])
                    actuals = [{"category":key,"actual_amount":str(value.quantize(CENT))} for key,value in sorted(actual_totals.items())]
                    projects.append({**dict(project), "budgets": budgets, "actuals": actuals})
                return self.reply(200, projects)
            if path == "/api/projects/samples":
                code = parse_qs(parsed.query).get("project_code", [""])[0].strip()
                sql = "SELECT d.*,p.name AS project_name FROM sample_dispatches d JOIN projects p ON p.code=d.project_code"
                params: tuple[str,...] = ()
                if code:
                    sql += " WHERE d.project_code=?"
                    params = (code,)
                sql += " ORDER BY d.dispatched_on DESC,d.created_at DESC"
                rows = conn.execute(sql, params).fetchall()
                dispatches = []
                for row in rows:
                    returned = sum((Decimal(item[0]) for item in conn.execute("SELECT total_cost FROM sample_returns WHERE dispatch_id=?", (row["id"],))), Decimal(0))
                    disposed = sum((Decimal(item[0]) for item in conn.execute("SELECT total_cost FROM sample_dispositions WHERE dispatch_id=?", (row["id"],))), Decimal(0))
                    dispatches.append({**dict(row),"returned_cost":str(returned.quantize(CENT)),
                        "disposed_cost":str(disposed.quantize(CENT)),
                        "outstanding_quantity":row["quantity"]-row["returned_quantity"]-row["disposed_quantity"],
                        "outstanding_cost":str((Decimal(row["total_cost"])-returned-disposed).quantize(CENT))})
                return self.reply(200, dispatches)
            if path == "/api/bike/orders":
                orders = []
                for order in conn.execute("SELECT * FROM bike_orders ORDER BY ordered_on DESC,created_at DESC").fetchall():
                    milestones = []
                    for milestone in conn.execute("SELECT * FROM bike_milestones WHERE bike_order_id=? ORDER BY due_on,created_at", (order["id"],)):
                        settled = Decimal(0)
                        if milestone["payable_id"]:
                            item = conn.execute("SELECT original_amount,settled_amount FROM receivables_payables WHERE id=?", (milestone["payable_id"],)).fetchone()
                            if item:
                                settled = Decimal(item["settled_amount"])
                        milestones.append({**dict(milestone),"settled_amount":str(settled.quantize(CENT)),"outstanding_amount":str((Decimal(milestone["amount"])-settled).quantize(CENT)),"status":"planned" if not milestone["payable_id"] else "paid" if settled>=Decimal(milestone["amount"]) else "due"})
                    landed_rows = conn.execute("SELECT base_amount_cny FROM bike_landed_costs WHERE bike_order_id=? AND cost_treatment='capitalized'", (order["id"],)).fetchall()
                    sold_rows=conn.execute("SELECT quantity FROM bike_sales WHERE bike_order_id=?",(order["id"],)).fetchall()
                    sold_quantity=sum((int(row["quantity"]) for row in sold_rows),0)
                    receipt_rows=conn.execute("SELECT quantity FROM bike_receipts WHERE bike_order_id=?",(order["id"],)).fetchall()
                    received_quantity=sum((int(row["quantity"]) for row in receipt_rows),0)
                    contract_cny = (Decimal(order["contract_amount"]) * Decimal(order["exchange_rate"])).quantize(CENT, rounding=ROUND_HALF_UP)
                    landed_cny = sum((Decimal(row["base_amount_cny"]) for row in landed_rows), Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)
                    orders.append({**dict(order),"received_quantity":received_quantity,"available_to_sell":max(0,received_quantity-sold_quantity),"sold_quantity":sold_quantity,"unsold_quantity":max(0,int(order["quantity"])-sold_quantity),"contract_amount_cny":str(contract_cny),"capitalized_landed_cost_cny":str(landed_cny),"landed_total_cny":str((contract_cny+landed_cny).quantize(CENT)),"unit_landed_cost_cny":str(((contract_cny+landed_cny)/Decimal(order["quantity"])).quantize(CENT,rounding=ROUND_HALF_UP)),"milestones":milestones})
                return self.reply(200, orders)
            if path == "/api/bike/landed-costs":
                rows=conn.execute("SELECT c.*,o.alsvid_order_no,o.supplier AS order_supplier FROM bike_landed_costs c JOIN bike_orders o ON o.id=c.bike_order_id ORDER BY c.incurred_on DESC,c.created_at DESC").fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path == "/api/bike/sales":
                rows=conn.execute("SELECT s.*,o.alsvid_order_no,o.sku FROM bike_sales s JOIN bike_orders o ON o.id=s.bike_order_id ORDER BY s.sold_on DESC,s.created_at DESC").fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path == "/api/bike/receipts":
                rows=conn.execute("SELECT r.*,o.alsvid_order_no,o.sku FROM bike_receipts r JOIN bike_orders o ON o.id=r.bike_order_id ORDER BY r.received_on DESC,r.created_at DESC").fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path == "/api/periods":
                rows = conn.execute("SELECT period,closed_at,closed_by,reviewed_checks,journal_count FROM period_locks ORDER BY period DESC").fetchall()
                return self.reply(200, [dict(row) for row in rows])
            if path == "/api/periods/close-readiness":
                period=parse_qs(parsed.query).get("period",[""])[0]
                try:
                    if len(period)!=7 or period[4]!="-": raise ValueError
                    datetime.strptime(period,"%Y-%m")
                except ValueError:
                    return self.reply(422,{"error":"period must use YYYY-MM format"})
                if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(period,)).fetchone():
                    return self.reply(409,{"error":"accounting period is already closed"})
                return self.reply(200,period_close_readiness(conn,period))
            if path == "/api/reports/operating":
                period = parse_qs(parsed.query).get("period", [date.today().strftime("%Y-%m")])[0]
                filters = parse_qs(parsed.query)
                business_line = filters.get("business_line", [""])[0].strip()
                entity_code = filters.get("entity_code", [""])[0].strip()
                project_code = filters.get("project_code", [""])[0].strip()
                try:
                    if len(period) != 7 or period[4] != "-":
                        raise ValueError
                    datetime.strptime(period, "%Y-%m")
                except ValueError:
                    return self.reply(422, {"error":"period must use YYYY-MM format"})
                sql = "SELECT l.account,l.debit,l.credit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.posted_on LIKE ? AND e.currency='CNY'"
                params: list[str] = [f"{period}-%"]
                for column, value in (("business_line", business_line), ("entity_code", entity_code), ("project_code", project_code)):
                    if value:
                        sql += f" AND COALESCE(l.{column},e.{column})=?"
                        params.append(value)
                rows = conn.execute(sql, params).fetchall()
                revenue = sum((Decimal(row["credit"])-Decimal(row["debit"]) for row in rows if row["account"]=="sales_revenue"), Decimal(0)).quantize(CENT)
                cogs = sum((Decimal(row["debit"])-Decimal(row["credit"]) for row in rows if row["account"]=="cost_of_goods_sold"), Decimal(0)).quantize(CENT)
                expenses = sum((Decimal(row["debit"])-Decimal(row["credit"]) for row in rows if row["account"].startswith("operating_expense:")), Decimal(0)).quantize(CENT)
                adjustment_gain=sum((Decimal(row["credit"])-Decimal(row["debit"]) for row in rows if row["account"]=="inventory_adjustment_gain"),Decimal(0)).quantize(CENT)
                adjustment_loss=sum((Decimal(row["debit"])-Decimal(row["credit"]) for row in rows if row["account"]=="inventory_adjustment_loss"),Decimal(0)).quantize(CENT)
                adjustment_net=(adjustment_gain-adjustment_loss).quantize(CENT)
                fx_rows=conn.execute("SELECT l.account,l.debit,l.credit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.posted_on LIKE ? AND e.currency='CNY'",(f"{period}-%",)).fetchall()
                fx_gain=sum((Decimal(row["credit"])-Decimal(row["debit"]) for row in fx_rows if row["account"]=="realized_fx_gain"),Decimal(0))
                fx_loss=sum((Decimal(row["debit"])-Decimal(row["credit"]) for row in fx_rows if row["account"]=="realized_fx_loss"),Decimal(0))
                realized_fx=(fx_gain-fx_loss).quantize(CENT)
                by_category: dict[str, Decimal] = {}
                for row in rows:
                    if row["account"].startswith("operating_expense:"):
                        category = row["account"].split(":",1)[1]
                        by_category[category] = by_category.get(category, Decimal(0)) + Decimal(row["debit"]) - Decimal(row["credit"])
                expense_reconciliation=expense_reconciliation_status(conn,period)
                return self.reply(200, {"period":period,"currency":"CNY","business_line":business_line or None,"entity_code":entity_code or None,"project_code":project_code or None,"sales_revenue":str(revenue),"cost_of_goods_sold":str(cogs),"gross_profit":str(revenue-cogs),"operating_expenses":str(expenses),"operating_expenses_by_category":{k:str(v.quantize(CENT)) for k,v in by_category.items()},"inventory_adjustment_gain":str(adjustment_gain),"inventory_adjustment_loss":str(adjustment_loss),"inventory_adjustment_net":str(adjustment_net),"operating_profit":str(revenue-cogs-expenses+adjustment_net),"realized_fx_gain_loss":str(realized_fx),"expenses_complete":expense_reconciliation["complete"],"expense_reconciliation_summary":{"source_count":expense_reconciliation["source_count"],"unlinked_expense_count":expense_reconciliation["unlinked_expense_count"],"mismatched_source_count":expense_reconciliation["mismatched_source_count"],"coverage_confirmed":expense_reconciliation["coverage_confirmed"]}})
            if path == "/api/reports/coffee":
                period=parse_qs(parsed.query).get("period",[date.today().strftime("%Y-%m")])[0]
                try:
                    if len(period)!=7 or period[4]!="-":raise ValueError
                    datetime.strptime(period,"%Y-%m")
                except ValueError:
                    return self.reply(422,{"error":"period must use YYYY-MM format"})
                by_sku:dict[tuple[str,str],dict[str,object]]={}
                def metric(sku:str,channel:str)->dict[str,object]:
                    key=(sku,channel or "未标注")
                    return by_sku.setdefault(key,{"sku":sku,"channel":key[1],"sold_quantity":0,"returned_quantity":0,"sales_revenue":Decimal(0),"refunds":Decimal(0),"sales_cogs":Decimal(0),"returned_cogs":Decimal(0),"unvalued_sales_lines":0})
                sales=conn.execute("SELECT l.sku,l.channel,l.quantity,l.unit_price,l.cogs_cny FROM sale_lines l JOIN sales_imports i ON i.id=l.import_id WHERE i.sold_on LIKE ? AND i.business_line='coffee'",(f"{period}-%",)).fetchall()
                for row in sales:
                    item=metric(row["sku"],row["channel"]);qty=int(row["quantity"]);item["sold_quantity"]+=qty;item["sales_revenue"]+=(Decimal(row["unit_price"])*qty).quantize(CENT,rounding=ROUND_HALF_UP)
                    if row["cogs_cny"] is None:item["unvalued_sales_lines"]+=1
                    else:item["sales_cogs"]+=Decimal(row["cogs_cny"])
                returns=conn.execute("SELECT l.sku,l.channel,r.quantity,r.revenue_cny,r.cogs_cny FROM sales_returns r JOIN sale_lines l ON l.id=r.sale_line_id WHERE r.returned_on LIKE ?",(f"{period}-%",)).fetchall()
                for row in returns:
                    item=metric(row["sku"],row["channel"]);item["returned_quantity"]+=int(row["quantity"]);item["refunds"]+=Decimal(row["revenue_cny"])
                    if row["cogs_cny"] is None:item["unvalued_sales_lines"]+=1
                    else:item["returned_cogs"]+=Decimal(row["cogs_cny"])
                items=[]
                for item in by_sku.values():
                    net_revenue=(item["sales_revenue"]-item["refunds"]).quantize(CENT);net_cogs=(item["sales_cogs"]-item["returned_cogs"]).quantize(CENT);complete=item["unvalued_sales_lines"]==0
                    items.append({"sku":item["sku"],"channel":item["channel"],"sold_quantity":item["sold_quantity"],"returned_quantity":item["returned_quantity"],"net_quantity":item["sold_quantity"]-item["returned_quantity"],"sales_revenue":str(item["sales_revenue"].quantize(CENT)),"refunds":str(item["refunds"].quantize(CENT)),"net_revenue":str(net_revenue),"net_cogs":str(net_cogs) if complete else None,"gross_profit":str((net_revenue-net_cogs).quantize(CENT)) if complete else None,"unvalued_sales_lines":item["unvalued_sales_lines"]})
                items.sort(key=lambda row:(row["gross_profit"] is None, -(Decimal(row["gross_profit"]) if row["gross_profit"] is not None else Decimal(0)),row["sku"],row["channel"]))
                channel_metrics:dict[str,dict[str,object]]={}
                for item in items:
                    channel=channel_metrics.setdefault(item["channel"],{"channel":item["channel"],"sold_quantity":0,"returned_quantity":0,"sales_revenue":Decimal(0),"refunds":Decimal(0),"net_cogs":Decimal(0),"unvalued_sales_lines":0})
                    for field in ("sold_quantity","returned_quantity","unvalued_sales_lines"):channel[field]+=item[field]
                    for field in ("sales_revenue","refunds"):channel[field]+=Decimal(item[field])
                    if item["net_cogs"] is not None:channel["net_cogs"]+=Decimal(item["net_cogs"])
                channels=[]
                for channel in channel_metrics.values():
                    net_revenue=(channel["sales_revenue"]-channel["refunds"]).quantize(CENT);complete=channel["unvalued_sales_lines"]==0;net_cogs=channel["net_cogs"].quantize(CENT)
                    channels.append({"channel":channel["channel"],"sold_quantity":channel["sold_quantity"],"returned_quantity":channel["returned_quantity"],"net_quantity":channel["sold_quantity"]-channel["returned_quantity"],"sales_revenue":str(channel["sales_revenue"].quantize(CENT)),"refunds":str(channel["refunds"].quantize(CENT)),"net_revenue":str(net_revenue),"net_cogs":str(net_cogs) if complete else None,"gross_profit":str((net_revenue-net_cogs).quantize(CENT)) if complete else None,"unvalued_sales_lines":channel["unvalued_sales_lines"]})
                channels.sort(key=lambda row:(row["gross_profit"] is None, -(Decimal(row["gross_profit"]) if row["gross_profit"] is not None else Decimal(0)),row["channel"]))
                available=any(row["channel"]!="未标注" for row in channels)
                note="已按导入的渠道字段汇总。" if available else "未提供渠道字段的历史销售归入“未标注”；后续 CSV 可映射渠道列。"
                return self.reply(200,{"period":period,"currency":"CNY","items":items,"channels":channels,"channel_analysis_available":available,"channel_analysis_note":note})
            if path == "/api/reports/cash-flow":
                filters=parse_qs(parsed.query);start=filters.get("from",[""])[0];end=filters.get("to",[""])[0]
                try:
                    datetime.strptime(start,"%Y-%m-%d");datetime.strptime(end,"%Y-%m-%d")
                    if start>end or start[:7]!=end[:7]:raise ValueError
                except ValueError:
                    return self.reply(422,{"error":"from and to must be valid YYYY-MM-DD dates within the same month, with from <= to"})
                categories={key:{"inflow":Decimal(0),"outflow":Decimal(0),"count":0} for key in ("operating","investing","financing","transfer","unclassified")}
                unvalued=0;rows=conn.execute("SELECT transaction_on,direction,amount,currency,base_amount_cny,cash_flow_category,cash_flow_note FROM bank_transactions WHERE transaction_on BETWEEN ? AND ? ORDER BY transaction_on,created_at",(start,end)).fetchall()
                for row in rows:
                    category=row["cash_flow_category"] if row["cash_flow_category"] in categories else "unclassified";bucket=categories[category];bucket["count"]+=1
                    if row["base_amount_cny"] is None:unvalued+=1;continue
                    value=Decimal(row["base_amount_cny"])
                    if row["direction"]=="in":bucket["inflow"]+=value
                    else:bucket["outflow"]+=value
                results={key:{"inflow":str(value["inflow"].quantize(CENT)),"outflow":str(value["outflow"].quantize(CENT)),"net":str((value["inflow"]-value["outflow"]).quantize(CENT)),"transactions":value["count"]} for key,value in categories.items()}
                net=sum((categories[key]["inflow"]-categories[key]["outflow"] for key in ("operating","investing","financing")),Decimal(0)).quantize(CENT)
                unclassified_count=categories["unclassified"]["count"]
                period=start[:7];accounts=[]
                for account in conn.execute("SELECT * FROM bank_accounts WHERE active=1 ORDER BY account_name"):
                    rec=conn.execute("SELECT * FROM bank_statement_reconciliations WHERE bank_account_id=? AND period=?",(account["id"],period)).fetchone()
                    if not rec:accounts.append({"account_name":account["account_name"],"currency":account["currency"],"reconciled":False,"difference":None});continue
                    movement=conn.execute("SELECT direction,amount FROM bank_transactions WHERE account_name=? AND transaction_on LIKE ?",(account["account_name"],f"{period}-%")).fetchall()
                    incoming=sum((Decimal(row["amount"]) for row in movement if row["direction"]=="in"),Decimal(0));outgoing=sum((Decimal(row["amount"]) for row in movement if row["direction"]=="out"),Decimal(0))
                    difference=(Decimal(rec["opening_balance"])+incoming-outgoing-Decimal(rec["closing_balance"])).quantize(CENT)
                    accounts.append({"account_name":account["account_name"],"currency":account["currency"],"opening_balance":rec["opening_balance"],"closing_balance":rec["closing_balance"],"reconciled":difference==0,"difference":str(difference)})
                coverage=conn.execute("SELECT * FROM bank_account_coverage WHERE period=?",(period,)).fetchone()
                coverage_confirmed=coverage is not None and coverage["account_count"]==len(accounts)
                accounts_reconciled=bool(accounts) and all(account["reconciled"] for account in accounts)
                all_accounts_cny=bool(accounts) and all(account["currency"]=="CNY" for account in accounts)
                account_coverage_complete=coverage_confirmed and accounts_reconciled
                opening_cash=sum((Decimal(account["opening_balance"]) for account in accounts),Decimal(0)) if account_coverage_complete and all_accounts_cny else None
                closing_cash=sum((Decimal(account["closing_balance"]) for account in accounts),Decimal(0)) if account_coverage_complete and all_accounts_cny else None
                return self.reply(200,{"from":start,"to":end,"currency":"CNY","bank_transactions":len(rows),"categories":results,"net_change_classified_cash":str(net),"unclassified_transactions":unclassified_count,"unvalued_transactions":unvalued,"all_transactions_classified":bool(rows) and unclassified_count==0,"all_amounts_valued":bool(rows) and unvalued==0,"registered_bank_accounts":len(accounts),"all_registered_accounts_reconciled":accounts_reconciled,"account_inventory_confirmed":coverage_confirmed,"bank_account_coverage_complete":account_coverage_complete,"coverage_note":coverage["note"] if coverage else None,"account_reconciliations":accounts,"opening_cash_balance":str(opening_cash.quantize(CENT)) if opening_cash is not None else None,"closing_cash_balance":str(closing_cash.quantize(CENT)) if closing_cash is not None else None,"opening_and_closing_cash_available":opening_cash is not None and closing_cash is not None,"cash_balance_unavailable_due_to_foreign_accounts":bool(accounts) and not all_accounts_cny,"complete":False,"scope":"按导入银行流水及人工现金流分类汇总；内部转账单列并排除于分类净变动。账户覆盖由用户确认并与各账户对账单核对；外币账户余额缺少期初/期末折算依据，仍不纳入人民币现金余额。此视图不是完整法定现金流量表"})
            if path == "/api/reports/trial-balance":
                as_of=parse_qs(parsed.query).get("as_of",[date.today().isoformat()])[0]
                try:
                    if len(as_of)!=10: raise ValueError
                    datetime.strptime(as_of,"%Y-%m-%d")
                except ValueError:
                    return self.reply(422,{"error":"as_of must use YYYY-MM-DD format"})
                rows=conn.execute("SELECT l.account,l.debit,l.credit FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.posted_on<=? AND e.currency='CNY' ORDER BY l.account",(as_of,)).fetchall()
                balances:dict[str,list[Decimal]]={}
                for row in rows:
                    balance=balances.setdefault(row["account"],[Decimal(0),Decimal(0)])
                    balance[0]+=Decimal(row["debit"]);balance[1]+=Decimal(row["credit"])
                account_rows=[]
                for account,(debits,credits) in sorted(balances.items()):
                    net=debits-credits
                    account_rows.append({"account":account,"debit_turnover":str(debits.quantize(CENT)),"credit_turnover":str(credits.quantize(CENT)),"debit_balance":str(max(net,Decimal(0)).quantize(CENT)),"credit_balance":str(max(-net,Decimal(0)).quantize(CENT))})
                total_debits=sum((item["debit_turnover"] for item in account_rows),Decimal(0)).quantize(CENT)
                total_credits=sum((item["credit_turnover"] for item in account_rows),Decimal(0)).quantize(CENT)
                total_debit_balance=sum((item["debit_balance"] for item in account_rows),Decimal(0)).quantize(CENT)
                total_credit_balance=sum((item["credit_balance"] for item in account_rows),Decimal(0)).quantize(CENT)
                opening=conn.execute("SELECT as_of,created_at FROM opening_balance_imports ORDER BY created_at LIMIT 1").fetchone()
                return self.reply(200,{"as_of":as_of,"currency":"CNY","accounts":account_rows,"journal_lines":len(rows),"debit_turnover_total":str(total_debits),"credit_turnover_total":str(total_credits),"debit_balance_total":str(total_debit_balance),"credit_balance_total":str(total_credit_balance),"balanced":total_debits==total_credits and total_debit_balance==total_credit_balance,"opening_balances_configured":opening is not None,"opening_balances_as_of":opening["as_of"] if opening else None,"scope":"系统内截至日期的 CNY 已过账分录，含期初余额导入；不含外币账簿及未过账业务"})
            if path == "/api/bank/accounts":
                period=parse_qs(parsed.query).get("period",[date.today().strftime("%Y-%m")])[0]
                try:
                    if len(period)!=7 or period[4]!="-":raise ValueError
                    datetime.strptime(period,"%Y-%m")
                except ValueError:
                    return self.reply(422,{"error":"period must use YYYY-MM format"})
                accounts=[]
                for account in conn.execute("SELECT * FROM bank_accounts WHERE active=1 ORDER BY account_name"):
                    movements=conn.execute("SELECT direction,amount FROM bank_transactions WHERE account_name=? AND currency=? AND transaction_on LIKE ?",(account["account_name"],account["currency"],f"{period}-%")).fetchall()
                    inflow=sum((Decimal(row["amount"]) for row in movements if row["direction"]=="in"),Decimal(0)).quantize(CENT)
                    outflow=sum((Decimal(row["amount"]) for row in movements if row["direction"]=="out"),Decimal(0)).quantize(CENT)
                    reconciliation=conn.execute("SELECT * FROM bank_statement_reconciliations WHERE bank_account_id=? AND period=?",(account["id"],period)).fetchone()
                    difference=(Decimal(reconciliation["opening_balance"])+inflow-outflow-Decimal(reconciliation["closing_balance"])).quantize(CENT) if reconciliation else None
                    accounts.append({**dict(account),"period":period,"transactions":len(movements),"inflow":str(inflow),"outflow":str(outflow),"opening_balance":reconciliation["opening_balance"] if reconciliation else None,"closing_balance":reconciliation["closing_balance"] if reconciliation else None,"statement_reference":reconciliation["statement_reference"] if reconciliation else None,"note":reconciliation["note"] if reconciliation else None,"difference":str(difference) if difference is not None else None,"reconciled":difference==0 if difference is not None else False})
                coverage=conn.execute("SELECT * FROM bank_account_coverage WHERE period=?",(period,)).fetchone()
                reconciled=bool(accounts) and all(account["reconciled"] for account in accounts)
                coverage_confirmed=coverage is not None and coverage["account_count"]==len(accounts)
                return self.reply(200,{"period":period,"accounts":accounts,"active_account_count":len(accounts),"all_registered_accounts_reconciled":reconciled,"account_inventory_confirmed":coverage_confirmed,"coverage_note":coverage["note"] if coverage else None,"coverage_confirmed_at":coverage["confirmed_at"] if coverage else None,"bank_coverage_complete":reconciled and coverage_confirmed})
            if path == "/api/bank/transactions":
                rows = conn.execute("SELECT * FROM bank_transactions ORDER BY transaction_on DESC,created_at DESC").fetchall()
                transactions = []
                for row in rows:
                    allocated = sum((Decimal(item[0]) for item in conn.execute("SELECT amount FROM bank_allocations WHERE bank_transaction_id=?", (row["id"],))), Decimal(0)).quantize(CENT)
                    allocated_base=sum((Decimal(item[0]) for item in conn.execute("SELECT f.bank_amount_cny FROM bank_allocations a JOIN bank_allocation_fx f ON f.allocation_id=a.id WHERE a.bank_transaction_id=?",(row["id"],))),Decimal(0)).quantize(CENT)
                    base=Decimal(row["base_amount_cny"]) if row["base_amount_cny"] else None
                    transactions.append({**dict(row),"allocated_amount":str(allocated),"remaining_amount":str((Decimal(row["amount"])-allocated).quantize(CENT)),"allocated_base_amount_cny":str(allocated_base),"remaining_base_amount_cny":str((base-allocated_base).quantize(CENT)) if base is not None else None})
                return self.reply(200, transactions)
            if path == "/api/invoices":
                rows = conn.execute("SELECT i.*,COALESCE((SELECT SUM(l.linked_amount) FROM invoice_links l WHERE l.invoice_id=i.id),'0.00') AS linked_amount FROM invoices i ORDER BY i.issued_on DESC,i.created_at DESC").fetchall()
                return self.reply(200, [dict(row) for row in rows])
            if path == "/api/purchases":
                rows = conn.execute("SELECT * FROM purchases ORDER BY created_at DESC").fetchall()
                result=[]
                for row in rows:
                    item=dict(row);doc_amount=Decimal(row["document_amount"]);ordered=int(row["ordered_qty"]);received=int(row["received_qty"]);item["order_amount"]=str(doc_amount.quantize(CENT,rounding=ROUND_HALF_UP));item["received_amount"]=str((doc_amount if received==ordered else doc_amount*received/ordered).quantize(CENT,rounding=ROUND_HALF_UP));item["remaining_document_amount"]=str((doc_amount-Decimal(item["received_amount"])).quantize(CENT,rounding=ROUND_HALF_UP));inventory_amount=(Decimal(row["unit_cost"])*received).quantize(CENT,rounding=ROUND_HALF_UP);item["received_inventory_amount"]=str(inventory_amount);item["received_amount_difference"]=str((Decimal(item["received_amount"])-inventory_amount).quantize(CENT,rounding=ROUND_HALF_UP));item["receipts"]=[dict(receipt) for receipt in conn.execute("SELECT id,source_key,quantity,received_on FROM purchase_receipts WHERE purchase_id=? ORDER BY received_on,created_at,id",(row["id"],)).fetchall()];result.append(item)
                return self.reply(200,result)
            if path == "/api/inventory":
                rows = conn.execute("SELECT s.sku,COALESCE(p.currency,s.currency) AS currency,s.remaining_qty,s.unit_cost FROM stock_layers s LEFT JOIN purchases p ON p.id=s.purchase_id WHERE s.remaining_qty>0 ORDER BY s.sku,COALESCE(p.currency,s.currency),s.created_at,s.id").fetchall()
                totals = {}
                for row in rows:
                    key = (row["sku"], row["currency"])
                    item = totals.setdefault(key, {"sku": row["sku"], "currency": row["currency"], "quantity": 0, "value": Decimal(0)})
                    item["quantity"] += row["remaining_qty"]
                    item["value"] += Decimal(row["remaining_qty"]) * Decimal(row["unit_cost"])
                return self.reply(200, [{**item, "value": str(item["value"].quantize(CENT))} for item in totals.values()])
            if path == "/api/inventory/safety-stock":
                rows=conn.execute("SELECT sku,safety_quantity,updated_at FROM inventory_safety_stocks ORDER BY sku").fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path == "/api/inventory/adjustments":
                rows=conn.execute("SELECT * FROM inventory_adjustments ORDER BY adjusted_on DESC,created_at DESC").fetchall()
                return self.reply(200,[dict(row) for row in rows])
            if path == "/api/movements":
                rows = conn.execute("SELECT * FROM inventory_movements ORDER BY created_at DESC,id DESC LIMIT 500").fetchall()
                return self.reply(200, [dict(row) for row in rows])
            if path == "/api/journals":
                rows = conn.execute("SELECT e.id,e.source_type,e.source_id,e.posted_on,e.created_at,e.business_line,e.entity_code,e.project_code,e.currency,e.memo,l.account,l.debit,l.credit,l.business_line AS line_business_line,l.entity_code AS line_entity_code,l.project_code AS line_project_code FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id ORDER BY e.posted_on DESC,e.created_at DESC,l.id LIMIT 1200").fetchall()
                entries = {}
                for row in rows:
                    entry = entries.setdefault(row["id"], {"id":row["id"],"source_type":row["source_type"],"source_id":row["source_id"],"posted_on":row["posted_on"],"created_at":row["created_at"],"business_line":row["business_line"],"entity_code":row["entity_code"],"project_code":row["project_code"],"currency":row["currency"],"memo":row["memo"],"lines":[],"debit_total":Decimal(0),"credit_total":Decimal(0)})
                    line = {"account":row["account"],"debit":row["debit"],"credit":row["credit"],"business_line":row["line_business_line"] or row["business_line"],"entity_code":row["line_entity_code"] or row["entity_code"],"project_code":row["line_project_code"] or row["project_code"]}
                    entry["lines"].append(line)
                    entry["debit_total"] += Decimal(line["debit"])
                    entry["credit_total"] += Decimal(line["credit"])
                return self.reply(200, [{**entry,"debit_total":str(entry["debit_total"].quantize(CENT)),"credit_total":str(entry["credit_total"].quantize(CENT))} for entry in entries.values()])
        return self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not local_auth.origin_allowed(self.headers.get("Origin")):
            return self.reply(403,{"error":"request origin not allowed"})
        if path=="/api/auth/login":
            if self.headers.get("Host","").split(":")[0].strip().lower() not in ("127.0.0.1","localhost"):
                return self.reply(403,{"error":"invalid local request host"})
            try:
                data=self.body()
                if not local_auth.enabled():
                    return self.reply(200,{"authenticated":True,"local_only":True})
                password=data.get("password")
                if not isinstance(password,str) or len(password)>1024:
                    return self.reply(422,{"error":"password is required"})
                token=local_auth.issue_session(self.client_address[0],password)
                if not token:
                    return self.reply(401,{"error":"invalid password or temporarily rate-limited"})
                return self.reply(200,{"authenticated":True,"role":"owner"},
                                  cookie=local_auth.session_cookie(token))
            except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
                return self.reply(422,{"error":str(exc)})
        if not self._session_valid():
            return
        if path=="/api/auth/logout":
            local_auth.end_session(self.headers)
            return self.reply(200,{"authenticated":False},
                              cookie=local_auth.clear_cookie())
        try:
            if path == "/api/import/preview-xlsx":
                raw_length=self.headers.get("Content-Length","")
                if not raw_length.isdigit() or not 0<int(raw_length)<=xlsx_reader.XLSX_BYTES_LIMIT:
                    raise ValueError("Excel file must be non-empty and no larger than 20 MiB")
                rows=xlsx_reader.first_sheet_rows(self.rfile.read(int(raw_length)))
                return self.reply(200,{"rows":rows,"sheet":"first","count":len(rows)-1,
                    "archived":False,"note":"preview only; original must be archived before posting"})
            if path == "/api/archive/files":
                return self.upload_original_document()
            data = self.body()
            if path == "/api/archive/links":
                with db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    result=original_archive.link_document(
                        conn,str(data.get("file_id","")),str(data.get("source_system","")),
                        str(data.get("source_document_id","")))
                return self.reply(200 if result["duplicate"] else 201,result)
            if path == "/api/purchases":
                return self.create_purchase(data)
            if path.startswith("/api/purchases/") and path.endswith("/receive"):
                return self.receive(path.split("/")[3], data)
            if path.startswith("/api/purchase-receipts/") and path.endswith("/edit"):
                return self.edit_purchase_receipt(path.split("/")[3],data)
            if path.startswith("/api/sales/lines/") and path.endswith("/return"):
                return self.return_sale_line(path.split("/")[4],data)
            if path == "/api/sales/import":
                return self.import_sale(data)
            if path == "/api/sales/import-row":
                return self.import_sale_row(data)
            if path == "/api/packaging-rules":
                return self.save_packaging_rule(data)
            if path == "/api/inventory/safety-stock":
                return self.save_inventory_safety_stock(data)
            if path == "/api/inventory/adjustments":
                return self.create_inventory_adjustment(data)
            if path == "/api/periods/close":
                return self.close_period(data)
            if path == "/api/journals/adjustments":
                return self.create_manual_journal(data)
            if path == "/api/opening-balances":
                return self.import_opening_balances(data)
            if path == "/api/bank/import":
                return self.import_bank(data)
            if path == "/api/bank/accounts":
                return self.create_bank_account(data)
            if path == "/api/bank/reconciliation":
                return self.save_bank_reconciliation(data)
            if path == "/api/bank/accounts/coverage":
                return self.confirm_bank_account_coverage(data)
            if path == "/api/bank/fx-review":
                return self.review_bank_fx(data)
            if path == "/api/open-items/fx-review":
                return self.review_open_item_fx(data)
            if path == "/api/bank/allocate":
                return self.allocate_bank(data)
            if path == "/api/bank/cash-flow-classification":
                return self.classify_cash_flow(data)
            if path == "/api/invoices":
                return self.create_invoice(data)
            if path == "/api/expenses":
                return self.create_expense(data)
            if path == "/api/expenses/reconciliation/source":
                return self.save_expense_reconciliation_source(data)
            if path == "/api/expenses/reconciliation/link":
                return self.link_expense_source(data)
            if path == "/api/expenses/reconciliation/coverage":
                return self.confirm_expense_source_coverage(data)
            if path == "/api/platform-income/reconciliation":
                return self.save_platform_income_reconciliation(data)
            if path.startswith("/api/expenses/") and path.endswith("/allocations"):
                return self.allocate_expense(path.split("/")[3], data)
            if path.startswith("/api/expenses/allocations/") and path.endswith("/reverse"):
                return self.reverse_expense_allocation(path.split("/")[4],data)
            if path == "/api/projects":
                return self.create_project(data)
            if path == "/api/projects/budgets":
                return self.save_project_budgets(data)
            if path == "/api/projects/samples/dispatch":
                return self.dispatch_sample(data)
            if path == "/api/projects/samples/return":
                return self.return_sample(data)
            if path == "/api/projects/samples/dispose":
                return self.dispose_sample(data)
            if path == "/api/bike/orders":
                return self.create_bike_order(data)
            if path == "/api/bike/receipts":
                return self.receive_bike_order(data)
            if path == "/api/bike/sales/import-row":
                return self.import_bike_sale(data)
            if path == "/api/bike/milestones/due":
                return self.mark_bike_milestone_due(data)
            if path == "/api/bike/landed-costs":
                return self.create_bike_landed_cost(data)
            if path == "/api/invoices/link":
                return self.link_invoice(data)
            if path == "/api/invoices/tax-status":
                return self.update_invoice_tax_status(data)
            return self.reply(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return self.reply(422, {"error": str(exc)})
        except DB_INTEGRITY_ERRORS as exc:
            return self.reply(409, {"error": str(exc)})
        except DB_ERRORS as exc:
            return self.reply(500, {"error": "database error", "detail": str(exc)})

    def upload_original_document(self) -> None:
        # Binary streaming rather than JSON/base64 avoids blowing up memory for
        # the larger monthly source files. Only safe on a loopback-only API.
        raw_length=self.headers.get("Content-Length","")
        if not raw_length.isdigit():
            raise ValueError("Content-Length must be a positive integer")
        length=int(raw_length)
        filename=unquote(self.headers.get("X-File-Name",""))
        category=self.headers.get("X-Document-Category","")
        period=self.headers.get("X-Document-Period","")
        source_system=self.headers.get("X-Source-System","")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result=original_archive.ingest(
                conn,self.rfile,length,filename,category,period,source_system)
        return self.reply(201,result)

    def import_opening_balances(self, data: dict) -> None:
        as_of=str(data.get("as_of",""))
        try:
            if len(as_of)!=10: raise ValueError
            datetime.strptime(as_of,"%Y-%m-%d")
        except ValueError:
            raise ValueError("as_of must use YYYY-MM-DD format")
        source_key=str(data.get("source_key","")).strip()
        entity_code=str(data.get("entity_code","main")).strip()
        entries=data.get("accounts")
        if not source_key or not entity_code or not isinstance(entries,list) or len(entries)<2:
            raise ValueError("source_key, entity_code and at least two opening balance accounts are required")
        lines=[]; seen=set()
        for index,row in enumerate(entries):
            if not isinstance(row,dict): raise ValueError(f"accounts[{index}] must be an object")
            account=str(row.get("account","")).strip()
            if not account or account in seen: raise ValueError("opening balance account codes must be non-empty and unique")
            seen.add(account)
            debit=amount(row.get("debit",0),f"accounts[{index}].debit")
            credit=amount(row.get("credit",0),f"accounts[{index}].credit")
            if debit<0 or credit<0 or (debit>0 and credit>0) or (debit==0 and credit==0):
                raise ValueError(f"account {account} must have a positive amount on exactly one side")
            lines.append((account,debit,credit))
        debit_total=sum((line[1] for line in lines),Decimal(0)).quantize(CENT)
        credit_total=sum((line[2] for line in lines),Decimal(0)).quantize(CENT)
        if debit_total!=credit_total:
            raise ValueError("opening balances must balance; include the verified opening equity balance")
        payload=json.dumps({"as_of":as_of,"entity_code":entity_code,"accounts":[{"account":a,"debit":str(d),"credit":str(c)} for a,d,c in lines]},ensure_ascii=False,sort_keys=True,separators=(",",":"))
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            unknown=[a for a,_,_ in lines if not conn.execute("SELECT 1 FROM chart_of_accounts WHERE account_code=? AND active=1",(a,)).fetchone()]
            if unknown:
                raise ValueError("opening balances contain accounts missing from the chart of accounts: " + ", ".join(unknown))
            previous=conn.execute("SELECT * FROM opening_balance_imports WHERE source_key=?",(source_key,)).fetchone()
            if previous:
                if previous["payload"]!=payload: raise ValueError("source_key already exists with different opening balances")
                return self.reply(200,{"id":previous["id"],"as_of":previous["as_of"],"status":"already_imported","journal_entry_id":previous["journal_entry_id"]})
            if conn.execute("SELECT 1 FROM opening_balance_imports LIMIT 1").fetchone():
                raise ValueError("this local ledger already has an opening balance import; corrections require an adjustment journal")
            first_entry=conn.execute("SELECT MIN(posted_on) FROM journal_entries").fetchone()[0]
            if first_entry and as_of>=first_entry:
                raise ValueError(f"opening balance date must precede the first existing journal ({first_entry})")
            ensure_open(conn,as_of)
            import_id=str(uuid4());entry_id=str(uuid4())
            journal(conn,"opening_balance",import_id,as_of,lines,"corporate",entity_code,currency="CNY",line_dimensions=[("corporate",entity_code,None) for _ in lines])
            posted=conn.execute("SELECT id FROM journal_entries WHERE source_type='opening_balance' AND source_id=?",(import_id,)).fetchone()
            if not posted: raise ValueError("opening balance journal was not created")
            entry_id=posted["id"]
            conn.execute("INSERT INTO opening_balance_imports (id,source_key,as_of,payload,journal_entry_id,created_at) VALUES(?,?,?,?,?,?)",(import_id,source_key,as_of,payload,entry_id,now()))
            return self.reply(201,{"id":import_id,"as_of":as_of,"status":"imported","journal_entry_id":entry_id,"debit_total":str(debit_total),"credit_total":str(credit_total),"accounts":len(lines)})

    def create_manual_journal(self,data:dict)->None:
        source_key=str(data.get("source_key","")).strip()
        posted_on=str(data.get("posted_on",date.today().isoformat())).strip()
        business_line=str(data.get("business_line","corporate")).strip()
        entity_code=str(data.get("entity_code","")).strip()
        project_code=str(data.get("project_code","")).strip() or None
        description=str(data.get("description","")).strip()
        entries=data.get("lines")
        if not source_key or len(source_key)>160 or not entity_code or not description or len(description)>500 or not isinstance(entries,list) or len(entries)<2:
            raise ValueError("source_key, entity_code, description and at least two journal lines are required")
        if business_line not in {"corporate","coffee","bike","exhibition"}:
            raise ValueError("business_line must be corporate, coffee, bike or exhibition")
        try:
            if len(posted_on)!=10:raise ValueError
            datetime.strptime(posted_on,"%Y-%m-%d")
        except ValueError:raise ValueError("posted_on must use YYYY-MM-DD format")
        lines=[]
        for index,row in enumerate(entries):
            if not isinstance(row,dict):raise ValueError(f"lines[{index}] must be an object")
            account=str(row.get("account","")).strip()
            if not account:raise ValueError(f"lines[{index}].account is required")
            debit=amount(row.get("debit",0),f"lines[{index}].debit")
            credit=amount(row.get("credit",0),f"lines[{index}].credit")
            if debit<0 or credit<0 or (debit>0 and credit>0) or (debit==0 and credit==0):
                raise ValueError(f"account {account} must have a positive amount on exactly one side")
            lines.append((account,debit,credit))
        debit_total=sum((line[1] for line in lines),Decimal(0)).quantize(CENT)
        credit_total=sum((line[2] for line in lines),Decimal(0)).quantize(CENT)
        if debit_total!=credit_total:raise ValueError("adjustment journal must balance")
        if debit_total<=0:raise ValueError("adjustment journal total must be positive")
        payload=json.dumps({"posted_on":posted_on,"business_line":business_line,"entity_code":entity_code,"project_code":project_code,"description":description,"lines":[{"account":a,"debit":str(d),"credit":str(c)} for a,d,c in lines]},ensure_ascii=False,sort_keys=True,separators=(",",":"))
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous=conn.execute("SELECT payload,journal_entry_id FROM manual_journal_requests WHERE source_key=?",(source_key,)).fetchone()
            if previous:
                if previous["payload"]!=payload:raise ValueError("source_key already exists with different adjustment journal content")
                return self.reply(200,{"journal_entry_id":previous["journal_entry_id"],"status":"already_posted"})
            ensure_open(conn,posted_on)
            unknown=[account for account,_,_ in lines if not conn.execute("SELECT 1 FROM chart_of_accounts WHERE account_code=? AND active=1",(account,)).fetchone()]
            if unknown:raise ValueError("adjustment journal contains inactive or unknown accounts: "+", ".join(unknown))
            if project_code:
                project=conn.execute("SELECT entity_code FROM projects WHERE code=?",(project_code,)).fetchone()
                if not project or project["entity_code"]!=entity_code:raise ValueError("project code must exist and match the selected legal entity")
            source_id=str(uuid4())
            journal(conn,"manual_adjustment",source_id,posted_on,lines,business_line,entity_code,project_code,currency="CNY")
            entry=conn.execute("SELECT id FROM journal_entries WHERE source_type='manual_adjustment' AND source_id=?",(source_id,)).fetchone()
            conn.execute("UPDATE journal_entries SET memo=? WHERE id=?",(description,entry["id"]))
            conn.execute("INSERT INTO manual_journal_requests (source_key,payload,journal_entry_id,created_at) VALUES(?,?,?,?)",(source_key,payload,entry["id"],now()))
            return self.reply(201,{"journal_entry_id":entry["id"],"status":"posted","debit_total":str(debit_total),"credit_total":str(credit_total),"line_count":len(lines)})

    def create_purchase(self, data: dict) -> None:
        archive_id=str(data.get("archive_file_id") or "").strip()
        number = str(data["number"]).strip()
        supplier = str(data["supplier"]).strip()
        sku = str(data["sku"]).strip()
        item_type = str(data["item_type"])
        qty = whole_quantity(data["quantity"], "purchase quantity")
        cost = amount(data["unit_cost"], "unit_cost")
        document_amount=amount(data.get("document_amount",str(cost*qty)),"document_amount")
        if not all((number, supplier, sku)) or qty <= 0 or cost < 0 or document_amount<0 or item_type not in ("finished_goods", "packaging"):
            raise ValueError("valid number, supplier, SKU, item_type, positive quantity and non-negative cost are required")
        day = str(data.get("ordered_on", date.today().isoformat()))
        business_line = str(data.get("business_line", "coffee")).strip()
        entity_code = str(data.get("entity_code", "main")).strip()
        project_code = str(data.get("project_code", "")).strip() or None
        if business_line != "coffee" or not entity_code:
            raise ValueError("coffee purchases require business_line=coffee and a legal entity code")
        currency = str(data.get("currency", "CNY")).upper().strip()
        rate = exchange_rate(data.get("exchange_rate", "1"))
        rate_date = str(data.get("rate_date") or day)
        try:
            datetime.strptime(rate_date,"%Y-%m-%d")
        except ValueError:
            raise ValueError("purchase exchange-rate date must use YYYY-MM-DD format")
        if len(currency)!=3 or not currency.isascii() or not currency.isalpha() or rate<=0:
            raise ValueError("purchase currency must be a three-letter ISO code with a positive exchange rate")
        if currency=="CNY" and rate!=Decimal(1):
            raise ValueError("CNY purchases must use the functional-currency exchange rate of 1")
        with db() as conn:
            ensure_open(conn, day)
            pid = str(uuid4())
            conn.execute("BEGIN IMMEDIATE")
            if project_code:
                project = conn.execute("SELECT entity_code FROM projects WHERE code=?", (project_code,)).fetchone()
                if not project or project["entity_code"] != entity_code:
                    raise ValueError("project code must exist and match the selected legal entity")
            conn.execute("INSERT INTO purchases (id,number,supplier,sku,item_type,ordered_qty,unit_cost,received_qty,currency,exchange_rate,rate_date,created_at,business_line,entity_code,project_code,document_amount) VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)", (pid, number, supplier, sku, item_type, qty, str(cost), currency, str(rate), rate_date, now(), business_line, entity_code, project_code, str(document_amount)))
            if archive_id:
                original_archive.link_document(conn,archive_id,"coffee.purchase",pid)
            return self.reply(201, {"id": pid, "number": number, "status": "open", "ordered_quantity": qty, "document_amount": str(document_amount)})

    def save_packaging_rule(self, data: dict) -> None:
        sku = str(data.get("sku", "")).strip()
        packaging_sku = str(data.get("packaging_sku", "")).strip() or None
        per_unit = int(data.get("packaging_per_unit", 0))
        raw_included = data.get("packaging_included", False)
        included = int(raw_included is True or str(raw_included).strip().lower() in ("1", "true", "yes", "on"))
        if included:
            per_unit = 0
        if not sku or per_unit < 0 or (per_unit > 0 and not packaging_sku):
            raise ValueError("SKU, non-negative packaging usage, and a packaging SKU for non-included packaging are required")
        if per_unit == 0 or included:
            packaging_sku = None
        with db() as conn:
            conn.execute("INSERT INTO sku_packaging_rules (sku,packaging_sku,packaging_per_unit,packaging_included,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET packaging_sku=excluded.packaging_sku,packaging_per_unit=excluded.packaging_per_unit,packaging_included=excluded.packaging_included,updated_at=excluded.updated_at", (sku, packaging_sku, per_unit, included, now()))
            return self.reply(200, {"sku":sku,"packaging_sku":packaging_sku,"packaging_per_unit":per_unit,"packaging_included":bool(included),"updated_at":now()})

    def save_inventory_safety_stock(self,data:dict)->None:
        sku=str(data.get("sku","")).strip()
        quantity=whole_quantity(data.get("safety_quantity",0),"safety quantity")
        if not sku or quantity<0:raise ValueError("SKU and a non-negative whole-number safety quantity are required")
        updated=now()
        with db() as conn:
            is_packaging=conn.execute("SELECT 1 FROM purchases WHERE sku=? AND item_type='packaging' UNION SELECT 1 FROM sku_packaging_rules WHERE packaging_sku=? LIMIT 1",(sku,sku)).fetchone()
            if not is_packaging:raise ValueError("safety stock can only be set for a packaging SKU recorded on a purchase or packaging rule")
            conn.execute("INSERT INTO inventory_safety_stocks (sku,safety_quantity,updated_at) VALUES(?,?,?) ON CONFLICT(sku) DO UPDATE SET safety_quantity=excluded.safety_quantity,updated_at=excluded.updated_at",(sku,quantity,updated))
            return self.reply(200,{"sku":sku,"safety_quantity":quantity,"updated_at":updated})

    def create_inventory_adjustment(self,data:dict)->None:
        source_key=str(data.get("source_key","")).strip();sku=str(data.get("sku","")).strip();item_type=str(data.get("item_type","")).strip();direction=str(data.get("direction","")).strip();reason=str(data.get("reason","")).strip();day=str(data.get("adjusted_on",date.today().isoformat())).strip();business_line=str(data.get("business_line","coffee")).strip();entity_code=str(data.get("entity_code","main")).strip();project_code=str(data.get("project_code","")).strip() or None;currency=str(data.get("currency","CNY")).upper().strip()
        qty=whole_quantity(data.get("quantity",0),"quantity");unit_cost=amount(data.get("unit_cost",0),"unit_cost");rate=exchange_rate(data.get("exchange_rate",1))
        if not source_key or len(source_key)>160 or not sku or item_type not in ("finished_goods","packaging") or direction not in ("gain","loss") or qty<=0 or not reason or len(reason)>500 or not entity_code:raise ValueError("source key, SKU, stock type, direction, positive quantity, reason and entity are required")
        if business_line!="coffee":raise ValueError("coffee inventory adjustment requires business_line=coffee")
        try:
            if len(day)!=10:raise ValueError
            datetime.strptime(day,"%Y-%m-%d")
        except ValueError:raise ValueError("adjusted_on must use YYYY-MM-DD format")
        if len(currency)!=3 or not currency.isalpha() or rate<=0 or unit_cost<0 or (currency=="CNY" and rate!=1):raise ValueError("currency, cost and exchange rate are invalid")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing=conn.execute("SELECT * FROM inventory_adjustments WHERE source_key=?",(source_key,)).fetchone()
            payload={"sku":sku,"item_type":item_type,"direction":direction,"quantity":qty,"unit_cost":str(unit_cost),"currency":currency,"exchange_rate":str(rate),"adjusted_on":day,"reason":reason,"business_line":business_line,"entity_code":entity_code,"project_code":project_code}
            if existing:
                if any(str(existing[k])!=str(v) for k,v in payload.items()):raise ValueError("source_key already exists with different inventory adjustment content")
                return self.reply(200,{"id":existing["id"],"status":"already_posted","journal_entry_id":existing["journal_entry_id"]})
            ensure_open(conn,day)
            item_exists=conn.execute("SELECT 1 FROM purchases WHERE sku=? AND item_type=? UNION SELECT 1 FROM sku_packaging_rules WHERE sku=? OR packaging_sku=? LIMIT 1",(sku,item_type,sku,sku)).fetchone()
            if not item_exists:raise ValueError("SKU must already exist in coffee purchases or packaging rules")
            if project_code:
                project=conn.execute("SELECT entity_code FROM projects WHERE code=?",(project_code,)).fetchone()
                if not project or project["entity_code"]!=entity_code:raise ValueError("project must exist and match entity")
            adjustment_id=str(uuid4());layer_id=None;value=Decimal(0)
            if direction=="gain":
                value=(unit_cost*rate*qty).quantize(CENT,rounding=ROUND_HALF_UP)
                if value<=0:raise ValueError("inventory gain requires a positive verified cost")
                layer_id=str(uuid4())
                conn.execute("INSERT INTO inventory_adjustments (id,source_key,sku,item_type,direction,quantity,unit_cost,currency,exchange_rate,adjusted_on,reason,business_line,entity_code,project_code,stock_layer_id,journal_entry_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, ?,NULL,NULL,?)",(adjustment_id,source_key,sku,item_type,direction,qty,str(unit_cost),currency,str(rate),day,reason,business_line,entity_code,project_code,now()))
                conn.execute("INSERT INTO stock_layers (id,purchase_id,adjustment_id,sku,received_qty,remaining_qty,unit_cost,currency,exchange_rate,created_at) VALUES(?,NULL,?,?,?,?,?,?,?,?)",(layer_id,adjustment_id,sku,qty,qty,str(unit_cost),currency,str(rate),now()))
                conn.execute("UPDATE inventory_adjustments SET stock_layer_id=? WHERE id=?",(layer_id,adjustment_id))
                conn.execute("INSERT INTO inventory_movements (id,sku,movement_type,quantity,unit_cost,source_type,source_id,created_at,currency,effective_on,base_unit_cost_cny) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(str(uuid4()),sku,"adjustment_gain",qty,str(unit_cost),"inventory_adjustment",adjustment_id,now(),currency,day,str((unit_cost*rate).quantize(Decimal("0.00000001")))) )
                journal(conn,"inventory_adjustment",adjustment_id,day,[("inventory",value,Decimal(0)),("inventory_adjustment_gain",Decimal(0),value)],business_line,entity_code,project_code,currency="CNY")
            else:
                layers=conn.execute("SELECT s.*,COALESCE(p.currency,s.currency) AS layer_currency,COALESCE(p.exchange_rate,s.exchange_rate) AS layer_rate FROM stock_layers s LEFT JOIN purchases p ON p.id=s.purchase_id WHERE s.sku=? AND s.remaining_qty>0 AND COALESCE(p.currency,s.currency)=? ORDER BY s.created_at,s.id",(sku,currency)).fetchall()
                if sum(int(x["remaining_qty"]) for x in layers)<qty:raise ValueError("inventory loss exceeds available stock in selected currency")
                remaining=qty;consumed=[]
                for layer in layers:
                    if remaining==0:break
                    take=min(remaining,int(layer["remaining_qty"]));layer_cost=Decimal(layer["unit_cost"]);layer_rate=Decimal(layer["layer_rate"]);base_unit=(layer_cost*layer_rate).quantize(Decimal("0.00000001"));value+=(base_unit*take);consumed.append((layer,take,layer_cost,layer_rate,base_unit));conn.execute("UPDATE stock_layers SET remaining_qty=remaining_qty-? WHERE id=?",(take,layer["id"]));remaining-=take
                value=value.quantize(CENT,rounding=ROUND_HALF_UP)
                conn.execute("INSERT INTO inventory_adjustments (id,source_key,sku,item_type,direction,quantity,unit_cost,currency,exchange_rate,adjusted_on,reason,business_line,entity_code,project_code,stock_layer_id,journal_entry_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)",(adjustment_id,source_key,sku,item_type,direction,qty,str((value/qty).quantize(CENT)),currency,str(rate),day,reason,business_line,entity_code,project_code,now()))
                for layer,take,cost,layer_rate,base_unit in consumed:
                    conn.execute("INSERT INTO inventory_movements (id,sku,movement_type,quantity,unit_cost,source_type,source_id,created_at,currency,effective_on,base_unit_cost_cny,stock_layer_id,cogs_cny) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(str(uuid4()),sku,"adjustment_loss",-take,str(cost),"inventory_adjustment",adjustment_id,now(),currency,day,str(base_unit),layer["id"],str((base_unit*take).quantize(CENT))))
                journal(conn,"inventory_adjustment",adjustment_id,day,[("inventory_adjustment_loss",value,Decimal(0)),("inventory",Decimal(0),value)],business_line,entity_code,project_code,currency="CNY")
            entry=conn.execute("SELECT id FROM journal_entries WHERE source_type='inventory_adjustment' AND source_id=?",(adjustment_id,)).fetchone()
            conn.execute("UPDATE inventory_adjustments SET journal_entry_id=? WHERE id=?",(entry["id"],adjustment_id))
            return self.reply(201,{"id":adjustment_id,"status":"posted","quantity":qty,"currency":currency,"inventory_value_cny":str(value),"journal_entry_id":entry["id"]})

    def create_bike_order(self, data: dict) -> None:
        alsvid_no = str(data.get("alsvid_order_no", "")).strip()
        supplier = str(data.get("supplier", "")).strip()
        sku = str(data.get("sku", "")).strip()
        entity_code = str(data.get("entity_code", "main")).strip()
        currency = str(data.get("currency", "CNY")).upper()
        contract = amount(data.get("contract_amount", "0"), "contract_amount")
        quantity = int(data.get("quantity", 0))
        rate = exchange_rate(data.get("exchange_rate", "1"))
        ordered_on = str(data.get("ordered_on", date.today().isoformat()))
        rate_date = str(data.get("rate_date", ordered_on))
        milestones = data.get("milestones")
        try:
            datetime.strptime(ordered_on, "%Y-%m-%d")
            datetime.strptime(rate_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("order and exchange-rate dates must use YYYY-MM-DD format")
        if not alsvid_no or not supplier or not sku or not entity_code or currency not in ("CNY", "EUR") or contract <= 0 or quantity <= 0 or rate <= 0 or not isinstance(milestones, list) or not milestones:
            raise ValueError("ALSVID order number, supplier, SKU/model, entity, positive quantity, CNY/EUR amount, exchange rate and payment milestones are required")
        normalized=[]
        percent_total=Decimal(0)
        for item in milestones:
            name=str(item.get("name", "")).strip()
            pct=Decimal(str(item.get("percentage", "0")))
            due_on=str(item.get("due_on", ""))
            try:
                datetime.strptime(due_on, "%Y-%m-%d")
            except ValueError:
                raise ValueError("milestone due dates must use YYYY-MM-DD format")
            if not name or not pct.is_finite() or pct <= 0:
                raise ValueError("milestone name and positive percentage are required")
            percent_total += pct
            normalized.append((name,pct,due_on))
        if percent_total != Decimal(100):
            raise ValueError("payment milestone percentages must total exactly 100")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            order_id=str(uuid4())
            conn.execute("INSERT INTO bike_orders (id,alsvid_order_no,supplier,contract_amount,currency,exchange_rate,rate_date,ordered_on,entity_code,created_at,quantity,sku) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (order_id,alsvid_no,supplier,str(contract),currency,str(rate),rate_date,ordered_on,entity_code,now(),quantity,sku))
            allocated=Decimal(0)
            for index,(name,pct,due_on) in enumerate(normalized):
                node_amount=(contract-allocated).quantize(CENT) if index==len(normalized)-1 else (contract*pct/Decimal(100)).quantize(CENT,rounding=ROUND_HALF_UP)
                if node_amount <= 0:
                    raise ValueError("each milestone must round to a positive amount")
                allocated += node_amount
                conn.execute("INSERT INTO bike_milestones (id,bike_order_id,name,percentage,amount,due_on,payable_id,recognized_on,created_at) VALUES(?,?,?,?,?,?,NULL,NULL,?)", (str(uuid4()),order_id,name,str(pct),str(node_amount),due_on,now()))
            return self.reply(201,{"id":order_id,"alsvid_order_no":alsvid_no,"contract_amount":str(contract),"currency":currency,"milestone_count":len(normalized)})

    def create_bike_landed_cost(self, data: dict) -> None:
        source_key=str(data.get("source_key", "")).strip()
        order_id=str(data.get("bike_order_id", "")).strip()
        cost_type=str(data.get("cost_type", "")).strip()
        capitalization_basis=str(data.get("capitalization_basis") or "").strip()
        treatment=("tax_pending" if cost_type=="import_vat" else
                   "capitalized" if cost_type in ("freight","duty") else
                   "period_expense" if cost_type=="warehouse" else
                   str(data.get("cost_treatment") or "period_expense").strip())
        if treatment not in ("tax_pending","capitalized","period_expense"):
            raise ValueError("invalid bicycle cost treatment")
        if cost_type=="other" and treatment=="capitalized" and not capitalization_basis:
            raise ValueError("other costs require an explicit acquisition capitalization basis")
        if cost_type=="other" and treatment=="tax_pending":
            raise ValueError("tax pending treatment is only valid for import VAT")
        counterparty=str(data.get("counterparty", "")).strip()
        reference_no=str(data.get("reference_no", "")).strip()
        incurred_on=str(data.get("incurred_on", date.today().isoformat()))
        rate_date=str(data.get("rate_date", incurred_on))
        currency=str(data.get("currency", "CNY")).upper()
        value=amount(data.get("amount", "0"), "amount")
        rate=exchange_rate(data.get("exchange_rate", "1"))
        try:
            datetime.strptime(rate_date,"%Y-%m-%d")
        except ValueError:
            raise ValueError("rate_date must use YYYY-MM-DD format")
        if not source_key or not order_id or not counterparty or cost_type not in ("freight","duty","warehouse","other","import_vat") or currency not in ("CNY","EUR") or value<=0 or rate<=0:
            raise ValueError("source key, ALSVID order, counterparty, supported cost type, positive amount and exchange rate are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing=conn.execute("SELECT * FROM bike_landed_costs WHERE source_key=?",(source_key,)).fetchone()
            if existing:
                require_same_source_payload(existing,{"bike_order_id":order_id,"cost_type":cost_type,"counterparty":counterparty,"reference_no":reference_no,"incurred_on":incurred_on,"amount":value,"currency":currency,"exchange_rate":rate,"rate_date":rate_date,"cost_treatment":treatment,"capitalization_basis":capitalization_basis})
                return self.reply(200,{"id":existing["id"],"duplicate":True})
            order=conn.execute("SELECT * FROM bike_orders WHERE id=?",(order_id,)).fetchone()
            if not order:
                raise ValueError("ALSVID purchase reference not found")
            if treatment=="capitalized" and conn.execute("SELECT 1 FROM bike_sales WHERE bike_order_id=? LIMIT 1",(order_id,)).fetchone():
                raise ValueError("capitalized landed costs are locked after sales imports; later adjustments require review")
            ensure_open(conn,incurred_on)
            cost_id=str(uuid4())
            base=(value*rate).quantize(CENT,rounding=ROUND_HALF_UP)
            posted_payable=None
            if treatment!="tax_pending":
                posted_payable=str(uuid4())
            conn.execute("INSERT INTO bike_landed_costs (id,source_key,bike_order_id,cost_type,counterparty,reference_no,incurred_on,amount,currency,exchange_rate,rate_date,base_amount_cny,payable_id,created_at,cost_treatment,capitalization_basis) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(cost_id,source_key,order_id,cost_type,counterparty,reference_no,incurred_on,str(value),currency,str(rate),rate_date,str(base),posted_payable,now(),treatment,capitalization_basis))
            if posted_payable:
                conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00',?,NULL,?)",(posted_payable,"payable","bike_landed_cost",cost_id,counterparty,str(value),currency,now()))
                record_open_item_fx(conn,posted_payable,base,rate,rate_date,"landed_cost_entry")
                expense_account="operating_expense:logistics" if cost_type=="warehouse" else "operating_expense:other"
                debit_account="inventory:bike" if treatment=="capitalized" else expense_account
                journal(conn,"bike_landed_cost",cost_id,incurred_on,[(debit_account,base,Decimal(0)),("accounts_payable",Decimal(0),base)],"bike",order["entity_code"],currency="CNY")
            return self.reply(201,{"id":cost_id,"source_key":source_key,"amount":str(value),"currency":currency,"base_amount_cny":str(base),"cost_type":cost_type,"cost_treatment":treatment,"capitalized":treatment=="capitalized","payable_id":posted_payable})

    def import_bike_sale(self, data: dict) -> None:
        archive_id=str(data.get("archive_file_id") or "").strip()
        source_key=str(data.get("source_key", "")).strip()
        order_no=str(data.get("alsvid_order_no", "")).strip()
        source=str(data.get("source", "")).strip()
        external_order_no=str(data.get("external_order_no", "")).strip()
        channel=str(data.get("channel", "")).strip()
        sold_on=str(data.get("sold_on", ""))
        currency=str(data.get("currency", "")).upper()
        try:
            raw_quantity=str(data.get("quantity", ""))
            if not raw_quantity.isdigit():
                raise ValueError
            quantity=int(raw_quantity)
            datetime.strptime(sold_on,"%Y-%m-%d")
        except (ValueError,TypeError):
            raise ValueError("quantity must be a positive integer and sold_on must use YYYY-MM-DD format")
        revenue=amount(data.get("net_revenue", "0"),"net_revenue")
        rate=exchange_rate(data.get("exchange_rate", "1"))
        rate_date=str(data.get("rate_date",sold_on))
        try:
            datetime.strptime(rate_date,"%Y-%m-%d")
        except ValueError:
            raise ValueError("rate_date must use YYYY-MM-DD format")
        if not source_key or not order_no or not source or not external_order_no or not channel or quantity<=0 or currency not in ("CNY","EUR") or revenue<=0 or rate<=0:
            raise ValueError("source row key, ALSVID order, channel, external order, positive quantity, net revenue, currency and rate are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate=conn.execute("SELECT * FROM bike_sales WHERE source_key=?",(source_key,)).fetchone()
            if duplicate:
                order=conn.execute("SELECT id FROM bike_orders WHERE alsvid_order_no=?",(order_no,)).fetchone()
                require_same_source_payload(duplicate,{"bike_order_id":order["id"] if order else "","source":source,"external_order_no":external_order_no,"sold_on":sold_on,"channel":channel,"quantity":quantity,"currency":currency,"net_revenue":revenue,"exchange_rate":rate,"rate_date":rate_date})
                if archive_id:
                    original_archive.link_document(conn,archive_id,"bike.sale",duplicate["id"])
                return self.reply(200,{"id":duplicate["id"],"duplicate":True,"posting_status":"posted"})
            ensure_open(conn,sold_on)
            order=conn.execute("SELECT * FROM bike_orders WHERE alsvid_order_no=?",(order_no,)).fetchone()
            if not order:
                raise ValueError("ALSVID purchase order reference not found")
            sold=conn.execute("SELECT quantity FROM bike_sales WHERE bike_order_id=?",(order["id"],)).fetchall()
            already_sold=sum((int(row["quantity"]) for row in sold),0)
            receipt_rows=conn.execute("SELECT quantity FROM bike_receipts WHERE bike_order_id=?",(order["id"],)).fetchall()
            received_quantity=sum((int(row["quantity"]) for row in receipt_rows),0)
            if already_sold+quantity>received_quantity:
                raise ValueError(f"sale quantity exceeds ALSVID-reconciled received quantity; available to sell: {max(0,received_quantity-already_sold)}")
            cost_rows=conn.execute("SELECT base_amount_cny FROM bike_landed_costs WHERE bike_order_id=? AND cost_treatment='capitalized'",(order["id"],)).fetchall()
            contract_cny=(Decimal(order["contract_amount"])*Decimal(order["exchange_rate"])).quantize(CENT,rounding=ROUND_HALF_UP)
            landed_cny=sum((Decimal(row["base_amount_cny"]) for row in cost_rows),Decimal(0)).quantize(CENT,rounding=ROUND_HALF_UP)
            unit_cost=((contract_cny+landed_cny)/Decimal(order["quantity"])).quantize(CENT,rounding=ROUND_HALF_UP)
            revenue_cny=(revenue*rate).quantize(CENT,rounding=ROUND_HALF_UP)
            cogs=(unit_cost*quantity).quantize(CENT,rounding=ROUND_HALF_UP)
            sale_id=str(uuid4())
            conn.execute("INSERT INTO bike_sales (id,source_key,bike_order_id,source,external_order_no,sold_on,channel,quantity,currency,net_revenue,exchange_rate,rate_date,revenue_cny,unit_landed_cost_cny,cogs_cny,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(sale_id,source_key,order["id"],source,external_order_no,sold_on,channel,quantity,currency,str(revenue),str(rate),rate_date,str(revenue_cny),str(unit_cost),str(cogs),now()))
            journal(conn,"bike_sale",sale_id,sold_on,[("accounts_receivable",revenue_cny,Decimal(0)),("sales_revenue",Decimal(0),revenue_cny),("cost_of_goods_sold",cogs,Decimal(0)),("inventory:bike",Decimal(0),cogs)],"bike",order["entity_code"],currency="CNY")
            receivable_id=str(uuid4())
            conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00',?,NULL,?)",(receivable_id,"receivable","bike_sale",sale_id,channel,str(revenue),currency,now()))
            record_open_item_fx(conn,receivable_id,revenue_cny,rate,rate_date,"bike_sales_import")
            if archive_id:
                original_archive.link_document(conn,archive_id,"bike.sale",sale_id)
            return self.reply(201,{"id":sale_id,"source_key":source_key,"quantity":quantity,"revenue_cny":str(revenue_cny),"unit_landed_cost_cny":str(unit_cost),"cogs_cny":str(cogs),"gross_margin_cny":str((revenue_cny-cogs).quantize(CENT)),"posting_status":"posted"})

    def receive_bike_order(self, data: dict) -> None:
        source_key=str(data.get("source_key", "")).strip()
        order_no=str(data.get("alsvid_order_no", "")).strip()
        received_on=str(data.get("received_on", ""))
        try:
            raw_quantity=str(data.get("quantity", ""))
            if not raw_quantity.isdigit():
                raise ValueError
            quantity=int(raw_quantity)
            datetime.strptime(received_on,"%Y-%m-%d")
        except (ValueError,TypeError):
            raise ValueError("quantity must be a positive integer and received_on must use YYYY-MM-DD format")
        if not source_key or not order_no or quantity<=0:
            raise ValueError("stable source row key, ALSVID order number and positive received quantity are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            order=conn.execute("SELECT * FROM bike_orders WHERE alsvid_order_no=?",(order_no,)).fetchone()
            if not order:
                raise ValueError("ALSVID purchase reference not found")
            duplicate=conn.execute("SELECT * FROM bike_receipts WHERE source_key=?",(source_key,)).fetchone()
            if duplicate:
                require_same_source_payload(duplicate,{"bike_order_id":order["id"],"received_on":received_on,"quantity":quantity})
                return self.reply(200,{"id":duplicate["id"],"duplicate":True})
            ensure_open(conn,received_on)
            previous=conn.execute("SELECT quantity,contract_cost_cny,advance_cleared_cny FROM bike_receipts WHERE bike_order_id=?",(order["id"],)).fetchall()
            previous_qty=sum((int(row["quantity"]) for row in previous),0)
            if previous_qty+quantity>int(order["quantity"]):
                raise ValueError(f"received quantity exceeds the ALSVID purchase reference; remaining quantity: {int(order['quantity'])-previous_qty}")
            previous_contract_cost=sum((Decimal(row["contract_cost_cny"]) for row in previous),Decimal(0))
            previous_advance_cleared=sum((Decimal(row["advance_cleared_cny"]) for row in previous),Decimal(0))
            total_contract_cny=(Decimal(order["contract_amount"])*Decimal(order["exchange_rate"])).quantize(CENT,rounding=ROUND_HALF_UP)
            cumulative_qty=previous_qty+quantity
            cumulative_cost=total_contract_cny if cumulative_qty==int(order["quantity"]) else (total_contract_cny*Decimal(cumulative_qty)/Decimal(order["quantity"])).quantize(CENT,rounding=ROUND_HALF_UP)
            receipt_cost=(cumulative_cost-previous_contract_cost).quantize(CENT)
            milestone_rows=conn.execute("SELECT m.amount FROM bike_milestones m WHERE m.bike_order_id=? AND m.payable_id IS NOT NULL",(order["id"],)).fetchall()
            recognized_advances=sum(((Decimal(row["amount"])*Decimal(order["exchange_rate"])).quantize(CENT,rounding=ROUND_HALF_UP) for row in milestone_rows),Decimal(0))
            cumulative_advance_clear=min(cumulative_cost,recognized_advances)
            advance_clear=(cumulative_advance_clear-previous_advance_cleared).quantize(CENT)
            if advance_clear<0:
                raise ValueError("recognized milestone advance clearing is inconsistent; review the batch before receiving")
            ap_credit=(receipt_cost-advance_clear).quantize(CENT)
            receipt_id=str(uuid4())
            conn.execute("INSERT INTO bike_receipts (id,source_key,bike_order_id,received_on,quantity,contract_cost_cny,advance_cleared_cny,created_at) VALUES(?,?,?,?,?,?,?,?)",(receipt_id,source_key,order["id"],received_on,quantity,str(receipt_cost),str(advance_clear),now()))
            lines=[("inventory:bike",receipt_cost,Decimal(0))]
            if advance_clear:
                lines.append(("supplier_advances",Decimal(0),advance_clear))
            if ap_credit:
                lines.append(("accounts_payable",Decimal(0),ap_credit))
                conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00','CNY',NULL,?)",(str(uuid4()),"payable","bike_receipt",receipt_id,order["supplier"],str(ap_credit),now()))
            journal(conn,"bike_receipt",receipt_id,received_on,lines,"bike",order["entity_code"],currency="CNY")
            return self.reply(201,{"id":receipt_id,"alsvid_order_no":order_no,"quantity":quantity,"contract_cost_cny":str(receipt_cost),"advance_cleared_cny":str(advance_clear),"accounts_payable_cny":str(ap_credit)})

    def mark_bike_milestone_due(self, data: dict) -> None:
        milestone_id=str(data.get("milestone_id", "")).strip()
        recognized_on=str(data.get("recognized_on", date.today().isoformat()))
        if not milestone_id:
            raise ValueError("milestone_id is required")
        try:
            datetime.strptime(recognized_on,"%Y-%m-%d")
        except ValueError:
            raise ValueError("recognized_on must use YYYY-MM-DD format")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT m.*,o.supplier,o.currency,o.exchange_rate,o.rate_date,o.entity_code,o.alsvid_order_no FROM bike_milestones m JOIN bike_orders o ON o.id=m.bike_order_id WHERE m.id=?",(milestone_id,)).fetchone()
            if not row:
                raise ValueError("bike payment milestone not found")
            if row["payable_id"]:
                if row["recognized_on"]!=recognized_on:
                    raise ValueError("milestone was already recognized on a different date; review the existing payable instead of reposting")
                return self.reply(200,{"milestone_id":milestone_id,"payable_id":row["payable_id"],"duplicate":True})
            if recognized_on<row["due_on"]:
                raise ValueError("payment milestone cannot be recognized before its planned due date")
            if conn.execute("SELECT 1 FROM bike_receipts WHERE bike_order_id=? LIMIT 1",(row["bike_order_id"],)).fetchone():
                raise ValueError("goods receipt has recognized the batch liability; use the receipt payable instead of accruing the milestone again")
            ensure_open(conn,recognized_on)
            payable_id=str(uuid4())
            conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00',?,?,?)",(payable_id,"payable","bike_milestone",milestone_id,row["supplier"],row["amount"],row["currency"],row["due_on"],now()))
            conn.execute("UPDATE bike_milestones SET payable_id=?,recognized_on=? WHERE id=?",(payable_id,recognized_on,milestone_id))
            amount_cny=(Decimal(row["amount"])*Decimal(row["exchange_rate"])).quantize(CENT,rounding=ROUND_HALF_UP)
            record_open_item_fx(conn,payable_id,amount_cny,Decimal(row["exchange_rate"]),row["rate_date"],"bike_contract_rate")
            journal(conn,"bike_milestone_due",milestone_id,recognized_on,[("supplier_advances",amount_cny,Decimal(0)),("accounts_payable",Decimal(0),amount_cny)],"bike",row["entity_code"],currency="CNY")
            return self.reply(201,{"milestone_id":milestone_id,"payable_id":payable_id,"amount":row["amount"],"currency":row["currency"],"supplier":row["supplier"],"alsvid_order_no":row["alsvid_order_no"]})

    def save_expense_reconciliation_source(self,data:dict)->None:
        period=str(data.get("period","")).strip();source_key=str(data.get("source_id_key","")).strip();source_name=str(data.get("source_name","")).strip();evidence_ref=str(data.get("evidence_ref","")).strip();scope_note=str(data.get("scope_note","")).strip();changed_by=str(data.get("changed_by","")).strip();action_key=str(data.get("action_key","")).strip();source_id=str(data.get("source_id","")).strip() or None
        try:
            if len(period)!=7:raise ValueError
            datetime.strptime(period,"%Y-%m");source_total=amount(data.get("source_total","0"),"source_total")
        except ValueError:raise ValueError("period must use YYYY-MM and source_total must be a valid CNY amount")
        if not source_key or not source_name or not evidence_ref or not scope_note or not changed_by or not action_key or source_total<0:raise ValueError("source key, source name, non-negative CNY total, evidence reference, scope note, operator name, and action_key are required")
        payload={"period":period,"source_id_key":source_key,"source_name":source_name,"source_total":str(source_total),"evidence_ref":evidence_ref,"scope_note":scope_note,"changed_by":changed_by,"source_id":source_id}
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate=replay_expense_reconciliation_request(conn,action_key,"source",payload)
            if duplicate is not None:return self.reply(200,duplicate)
            if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(period,)).fetchone():raise ValueError(f"accounting period {period} is closed")
            source=conn.execute("SELECT * FROM expense_reconciliation_sources WHERE id=?",(source_id,)).fetchone() if source_id else conn.execute("SELECT * FROM expense_reconciliation_sources WHERE period=? AND source_key=?",(period,source_key)).fetchone()
            stamp=now()
            if source:
                if source["period"]!=period or source["source_key"]!=source_key:raise ValueError("source identity cannot be moved to another key or period")
                old={key:source[key] for key in ("period","source_key","source_name","source_total","evidence_ref","scope_note","updated_by")}
                new={"period":period,"source_key":source_key,"source_name":source_name,"source_total":str(source_total),"evidence_ref":evidence_ref,"scope_note":scope_note,"updated_by":changed_by}
                sid=source["id"]
                if old!=new:
                    conn.execute("INSERT INTO expense_reconciliation_source_history VALUES(?,?,?,?,?,?,?)",(str(uuid4()),sid,json.dumps(old,ensure_ascii=False,sort_keys=True),json.dumps(new,ensure_ascii=False,sort_keys=True),action_key,stamp,changed_by))
                    conn.execute("UPDATE expense_reconciliation_sources SET source_name=?,source_total=?,evidence_ref=?,scope_note=?,updated_at=?,updated_by=? WHERE id=?",(source_name,str(source_total),evidence_ref,scope_note,stamp,changed_by,sid))
            else:
                sid=str(uuid4());conn.execute("INSERT INTO expense_reconciliation_sources VALUES(?,?,?,?,?,?,?,?,?,?)",(sid,period,source_key,source_name,str(source_total),evidence_ref,scope_note,stamp,stamp,changed_by))
            result={"source_id":sid,"period":period,"source_key":source_key,"complete":expense_reconciliation_status(conn,period)["complete"]}
            save_expense_reconciliation_request(conn,action_key,"source",payload,result)
            return self.reply(201,result)

    def save_platform_income_reconciliation(self,data:dict)->None:
        period=str(data.get("period","")).strip();platform=str(data.get("platform","")).strip();store_name=str(data.get("store_name","")).strip();evidence_ref=str(data.get("evidence_ref","")).strip();note=str(data.get("note","")).strip();changed_by=str(data.get("changed_by","")).strip();action_key=str(data.get("action_key","")).strip()
        try:
            if len(period)!=7:raise ValueError
            datetime.strptime(period,"%Y-%m")
            amounts={key:amount(data.get(key,"0"),key) for key in ("gross_amount","refund_amount","subsidy_amount","platform_fee_amount","settlement_amount")}
            optional={key:(None if data.get(key) in (None,"") else amount(data.get(key),key)) for key in ("bank_received_amount","declared_sales_amount")}
        except ValueError as exc:
            raise ValueError("period must use YYYY-MM and all amounts must be valid CNY amounts") from exc
        if not platform or not store_name or not evidence_ref or not changed_by or not action_key or any(value<0 for value in amounts.values()) or any(value is not None and value<0 for value in optional.values()):
            raise ValueError("platform, store, non-negative amounts, evidence reference, and operator name are required")
        payload={"period":period,"platform":platform,"store_name":store_name,**{key:str(value) for key,value in amounts.items()},**{key:str(value) if value is not None else None for key,value in optional.items()},"evidence_ref":evidence_ref,"note":note,"changed_by":changed_by}
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior=conn.execute("SELECT payload,response FROM platform_income_reconciliation_requests WHERE action_key=?",(action_key,)).fetchone()
            serialized=json.dumps(payload,ensure_ascii=False,sort_keys=True)
            if prior:
                if prior["payload"]!=serialized:raise ValueError("action_key is already recorded with different platform income data")
                return self.reply(200,{**json.loads(prior["response"]),"duplicate":True})
            if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(period,)).fetchone():raise ValueError(f"accounting period {period} is closed")
            row=conn.execute("SELECT * FROM platform_income_reconciliations WHERE period=? AND platform=? AND store_name=?",(period,platform,store_name)).fetchone();stamp=now()
            if row:
                old={key:row[key] for key in ("period","platform","store_name","gross_amount","refund_amount","subsidy_amount","platform_fee_amount","settlement_amount","bank_received_amount","declared_sales_amount","evidence_ref","note","updated_by")}
                new={**{key:payload[key] for key in ("period","platform","store_name","gross_amount","refund_amount","subsidy_amount","platform_fee_amount","settlement_amount","bank_received_amount","declared_sales_amount","evidence_ref","note")},"updated_by":changed_by}
                rid=row["id"]
                if old!=new:
                    conn.execute("INSERT INTO platform_income_reconciliation_history VALUES(?,?,?,?,?,?,?)",(str(uuid4()),rid,json.dumps(old,ensure_ascii=False,sort_keys=True),json.dumps(new,ensure_ascii=False,sort_keys=True),action_key,stamp,changed_by))
                    conn.execute("UPDATE platform_income_reconciliations SET gross_amount=?,refund_amount=?,subsidy_amount=?,platform_fee_amount=?,settlement_amount=?,bank_received_amount=?,declared_sales_amount=?,evidence_ref=?,note=?,updated_at=?,updated_by=? WHERE id=?",(str(amounts["gross_amount"]),str(amounts["refund_amount"]),str(amounts["subsidy_amount"]),str(amounts["platform_fee_amount"]),str(amounts["settlement_amount"]),str(optional["bank_received_amount"]) if optional["bank_received_amount"] is not None else None,str(optional["declared_sales_amount"]) if optional["declared_sales_amount"] is not None else None,evidence_ref,note,stamp,changed_by,rid))
            else:
                rid=str(uuid4());conn.execute("INSERT INTO platform_income_reconciliations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,period,platform,store_name,str(amounts["gross_amount"]),str(amounts["refund_amount"]),str(amounts["subsidy_amount"]),str(amounts["platform_fee_amount"]),str(amounts["settlement_amount"]),str(optional["bank_received_amount"]) if optional["bank_received_amount"] is not None else None,str(optional["declared_sales_amount"]) if optional["declared_sales_amount"] is not None else None,evidence_ref,note,stamp,changed_by))
            response={"id":rid,"period":period,"platform":platform,"store_name":store_name}
            conn.execute("INSERT INTO platform_income_reconciliation_requests VALUES(?,?,?,?)",(action_key,serialized,json.dumps(response,ensure_ascii=False,sort_keys=True),stamp))
            return self.reply(201,response)

    def link_expense_source(self,data:dict)->None:
        expense_id=str(data.get("expense_id","")).strip();source_id=str(data.get("source_id","")).strip();changed_by=str(data.get("changed_by","")).strip();action_key=str(data.get("action_key","")).strip()
        if not expense_id or not source_id or not changed_by or not action_key:raise ValueError("expense, source, operator name, and action_key are required")
        payload={"expense_id":expense_id,"source_id":source_id,"changed_by":changed_by}
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate=replay_expense_reconciliation_request(conn,action_key,"link",payload)
            if duplicate is not None:return self.reply(200,duplicate)
            expense=conn.execute("SELECT * FROM expenses WHERE id=?",(expense_id,)).fetchone();source=conn.execute("SELECT * FROM expense_reconciliation_sources WHERE id=?",(source_id,)).fetchone()
            if not expense or not source:raise ValueError("expense or external source was not found")
            period=expense["incurred_on"][:7]
            if source["period"]!=period:raise ValueError("expense and external source must belong to the same period")
            if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(period,)).fetchone():raise ValueError(f"accounting period {period} is closed")
            existing=conn.execute("SELECT * FROM expense_source_links WHERE expense_id=?",(expense_id,)).fetchone();stamp=now()
            if existing and existing["source_id"]!=source_id:
                conn.execute("INSERT INTO expense_source_link_history VALUES(?,?,?,?,?,?,?)",(str(uuid4()),expense_id,existing["source_id"],source_id,action_key,stamp,changed_by))
                conn.execute("UPDATE expense_source_links SET source_id=?,amount=?,linked_at=?,linked_by=? WHERE expense_id=?",(source_id,expense["amount"],stamp,changed_by,expense_id))
            elif not existing:
                conn.execute("INSERT INTO expense_source_links VALUES(?,?,?,?,?)",(expense_id,source_id,expense["amount"],stamp,changed_by))
                conn.execute("INSERT INTO expense_source_link_history VALUES(?,?,?,?,?,?,?)",(str(uuid4()),expense_id,None,source_id,action_key,stamp,changed_by))
            result={"expense_id":expense_id,"source_id":source_id,"period":period,"complete":expense_reconciliation_status(conn,period)["complete"]}
            save_expense_reconciliation_request(conn,action_key,"link",payload,result)
            return self.reply(201,result)

    def confirm_expense_source_coverage(self,data:dict)->None:
        period=str(data.get("period","")).strip();note=str(data.get("note","")).strip();confirmed_by=str(data.get("confirmed_by","")).strip();action_key=str(data.get("action_key","")).strip()
        try:
            if len(period)!=7:raise ValueError
            datetime.strptime(period,"%Y-%m")
        except ValueError:raise ValueError("period must use YYYY-MM format")
        if not note or not confirmed_by or not action_key:raise ValueError("coverage rationale, operator name, and action_key are required")
        payload={"period":period,"note":note,"confirmed_by":confirmed_by}
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate=replay_expense_reconciliation_request(conn,action_key,"coverage",payload)
            if duplicate is not None:return self.reply(200,duplicate)
            if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(period,)).fetchone():raise ValueError(f"accounting period {period} is closed")
            source_count=conn.execute("SELECT COUNT(*) FROM expense_reconciliation_sources WHERE period=?",(period,)).fetchone()[0]
            expense_count=conn.execute("SELECT COUNT(*) FROM expenses WHERE incurred_on LIKE ?",(period+"-%",)).fetchone()[0]
            stamp=now();old=conn.execute("SELECT * FROM expense_reconciliation_coverage WHERE period=?",(period,)).fetchone()
            new={"period":period,"source_count":source_count,"expense_count":expense_count,"note":note,"confirmed_by":confirmed_by,"confirmed_at":stamp}
            if old:
                old_payload={key:old[key] for key in new}
                conn.execute("INSERT INTO expense_reconciliation_coverage_history VALUES(?,?,?,?,?,?,?)",(str(uuid4()),period,json.dumps(old_payload,ensure_ascii=False,sort_keys=True),json.dumps(new,ensure_ascii=False,sort_keys=True),action_key,stamp,confirmed_by))
                conn.execute("UPDATE expense_reconciliation_coverage SET source_count=?,expense_count=?,note=?,confirmed_by=?,confirmed_at=? WHERE period=?",(source_count,expense_count,note,confirmed_by,stamp,period))
            else:
                conn.execute("INSERT INTO expense_reconciliation_coverage VALUES(?,?,?,?,?,?)",(period,source_count,expense_count,note,confirmed_by,stamp))
                conn.execute("INSERT INTO expense_reconciliation_coverage_history VALUES(?,?,?,?,?,?,?)",(str(uuid4()),period,"{}",json.dumps(new,ensure_ascii=False,sort_keys=True),action_key,stamp,confirmed_by))
            status=expense_reconciliation_status(conn,period)
            result={"period":period,"coverage_confirmed":status["coverage_confirmed"],"complete":status["complete"]}
            save_expense_reconciliation_request(conn,action_key,"coverage",payload,result)
            return self.reply(201,result)

    def create_expense(self, data: dict) -> None:
        source_key = str(data.get("source_key", "")).strip()
        day = str(data.get("incurred_on", date.today().isoformat()))
        category = str(data.get("category", "")).strip()
        counterparty = str(data.get("counterparty", "")).strip()
        memo = str(data.get("memo", "")).strip()
        currency = str(data.get("currency", "CNY")).upper()
        business_line = str(data.get("business_line", "corporate")).strip()
        entity_code = str(data.get("entity_code", "main")).strip()
        project_code = str(data.get("project_code", "")).strip() or None
        value = amount(data.get("amount", "0"), "amount")
        categories = EXPENSE_CATEGORIES
        business_lines = {"coffee", "bike", "exhibition", "corporate"}
        if not source_key or not counterparty or category not in categories or value <= 0 or currency != "CNY" or business_line not in business_lines or not entity_code:
            raise ValueError("source_key, counterparty, supported category, positive CNY amount, business line and legal entity are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if business_line == "exhibition" and not project_code:
                raise ValueError("exhibition expenses must be assigned to a project")
            if project_code:
                project = conn.execute("SELECT entity_code FROM projects WHERE code=?", (project_code,)).fetchone()
                if not project or project["entity_code"] != entity_code:
                    raise ValueError("project code must exist and match the selected legal entity")
            existing = conn.execute("SELECT * FROM expenses WHERE source_key=?", (source_key,)).fetchone()
            if existing:
                require_same_source_payload(existing,{"incurred_on":day,"category":category,"counterparty":counterparty,"amount":value,"currency":currency,"memo":memo,"business_line":business_line,"entity_code":entity_code,"project_code":project_code})
                return self.reply(200, {"id": existing["id"], "duplicate": True, "message": "source already recorded"})
            ensure_open(conn, day)
            expense_id = str(uuid4())
            conn.execute("INSERT INTO expenses (id,source_key,incurred_on,category,counterparty,amount,currency,memo,created_at,business_line,entity_code,project_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (expense_id, source_key, day, category, counterparty, str(value), currency, memo, now(), business_line, entity_code, project_code))
            journal(conn, "operating_expense", expense_id, day, [(f"operating_expense:{category}", value, Decimal(0)), ("accounts_payable", Decimal(0), value)], business_line, entity_code, project_code)
            conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00',?,NULL,?)", (str(uuid4()), "payable", "operating_expense", expense_id, counterparty, str(value), currency, now()))
            return self.reply(201, {"id": expense_id, "source_key": source_key, "amount": str(value), "currency": currency, "status": "accrued"})

    def allocate_expense(self, expense_id: str, data: dict) -> None:
        source_key = str(data.get("source_key", "")).strip()
        project_code = str(data.get("project_code", "")).strip()
        business_line = str(data.get("business_line", "")).strip()
        allocated_on = str(data.get("allocated_on", date.today().isoformat()))
        value = amount(data.get("amount", "0"), "amount")
        if not source_key or not project_code or value <= 0 or business_line not in {"coffee", "bike", "exhibition"}:
            raise ValueError("source key, project, positive amount and operating business line are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT * FROM expense_project_allocations WHERE source_key=?", (source_key,)).fetchone()
            if prior:
                require_same_source_payload(prior,{"expense_id":expense_id,"project_code":project_code,"business_line":business_line,"amount":value,"allocated_on":allocated_on})
                return self.reply(200,{"id":prior["id"],"duplicate":True,"message":"expense allocation already recorded"})
            expense=conn.execute("SELECT * FROM expenses WHERE id=?",(expense_id,)).fetchone()
            if not expense:
                raise ValueError("expense not found")
            if expense["project_code"]:
                raise ValueError("only expenses without an existing project assignment can be allocated")
            project=conn.execute("SELECT entity_code FROM projects WHERE code=?",(project_code,)).fetchone()
            if not project or project["entity_code"] != expense["entity_code"]:
                raise ValueError("project must exist and match the expense legal entity")
            allocated=Decimal(0)
            for row in conn.execute("SELECT a.amount,COALESCE(SUM(r.amount),'0.00') AS reversed_amount FROM expense_project_allocations a LEFT JOIN expense_project_allocation_reversals r ON r.allocation_id=a.id WHERE a.expense_id=? GROUP BY a.id",(expense_id,)):
                allocated += Decimal(row["amount"])-Decimal(str(row["reversed_amount"]))
            remaining=(Decimal(expense["amount"])-allocated).quantize(CENT)
            if value > remaining:
                raise ValueError(f"allocation exceeds unallocated expense balance {remaining}")
            ensure_open(conn,allocated_on)
            allocation_id=str(uuid4())
            conn.execute("INSERT INTO expense_project_allocations (id,source_key,expense_id,project_code,business_line,amount,allocated_on,created_at) VALUES(?,?,?,?,?,?,?,?)",(allocation_id,source_key,expense_id,project_code,business_line,str(value),allocated_on,now()))
            category_account=f"operating_expense:{expense['category']}"
            original_dimensions=(expense["business_line"],expense["entity_code"],None)
            project_dimensions=(business_line,project["entity_code"],project_code)
            journal(conn,"expense_project_allocation",allocation_id,allocated_on,[(category_account,value,Decimal(0)),(category_account,Decimal(0),value)],expense["business_line"],expense["entity_code"],currency="CNY",line_dimensions=[project_dimensions,original_dimensions])
            return self.reply(201,{"id":allocation_id,"expense_id":expense_id,"project_code":project_code,"business_line":business_line,"allocated_amount":str(value),"remaining_amount":str((remaining-value).quantize(CENT))})

    def reverse_expense_allocation(self,allocation_id:str,data:dict)->None:
        source_key=str(data.get("source_key","")).strip();reversed_on=str(data.get("reversed_on",date.today().isoformat()));actor=str(data.get("changed_by","local-unverified")).strip()
        value=amount(data.get("amount","0"),"reversal amount")
        try:datetime.strptime(reversed_on,"%Y-%m-%d")
        except ValueError:raise ValueError("reversed_on must use YYYY-MM-DD format")
        if not source_key or not actor or value<=0:raise ValueError("source_key, operator and positive reversal amount are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior=conn.execute("SELECT allocation_id,amount,reversed_on,changed_by FROM expense_project_allocation_reversals WHERE source_key=?",(source_key,)).fetchone()
            if prior:
                require_same_source_payload(prior,{"allocation_id":allocation_id,"amount":value,"reversed_on":reversed_on,"changed_by":actor})
                return self.reply(200,{"allocation_id":allocation_id,"reversed_amount":str(value),"duplicate":True})
            allocation=conn.execute("SELECT a.*,e.category,e.business_line AS original_business_line,e.entity_code,p.entity_code AS project_entity FROM expense_project_allocations a JOIN expenses e ON e.id=a.expense_id JOIN projects p ON p.code=a.project_code WHERE a.id=?",(allocation_id,)).fetchone()
            if not allocation:raise ValueError("expense allocation not found")
            already=conn.execute("SELECT amount FROM expense_project_allocation_reversals WHERE allocation_id=?",(allocation_id,)).fetchall()
            reversed_amount=sum((Decimal(row["amount"]) for row in already),Decimal(0))
            remaining=(Decimal(allocation["amount"])-reversed_amount).quantize(CENT)
            if value>remaining:raise ValueError(f"reversal exceeds remaining allocation amount {remaining}")
            ensure_open(conn,reversed_on)
            original=conn.execute("SELECT id FROM journal_entries WHERE source_type='expense_project_allocation' AND source_id=?",(allocation_id,)).fetchone()
            if not original:raise ValueError("original allocation journal is missing; it cannot be reversed safely")
            category_account=f"operating_expense:{allocation['category']}"
            source_dimensions=(allocation["original_business_line"],allocation["entity_code"],None)
            project_dimensions=(allocation["business_line"],allocation["project_entity"],allocation["project_code"])
            reversal_id=str(uuid4())
            # Reverse the original direction: debit the shared-cost dimension and credit the project.
            journal(conn,"expense_project_allocation_reversal",reversal_id,reversed_on,[(category_account,value,Decimal(0)),(category_account,Decimal(0),value)],allocation["original_business_line"],allocation["entity_code"],currency="CNY",line_dimensions=[source_dimensions,project_dimensions])
            conn.execute("INSERT INTO expense_project_allocation_reversals VALUES(?,?,?,?,?,?,?)",(str(uuid4()),source_key,allocation_id,str(value),reversed_on,actor,now()))
            return self.reply(201,{"allocation_id":allocation_id,"expense_id":allocation["expense_id"],"reversed_amount":str(value),"remaining_reversible_amount":str((remaining-value).quantize(CENT)),"duplicate":False,"authenticated":False})

    def create_project(self, data: dict) -> None:
        code = str(data.get("code", "")).strip()
        name = str(data.get("name", "")).strip()
        entity_code = str(data.get("entity_code", "main")).strip()
        starts_on = str(data.get("starts_on", ""))
        ends_on = str(data.get("ends_on", ""))
        try:
            start = datetime.strptime(starts_on, "%Y-%m-%d").date()
            end = datetime.strptime(ends_on, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("project dates must use YYYY-MM-DD format")
        if not code or not name or not entity_code or end < start:
            raise ValueError("project code, name, legal entity and valid start/end dates are required")
        with db() as conn:
            conn.execute("INSERT INTO projects (id,code,name,entity_code,starts_on,ends_on,currency,created_at) VALUES(?,?,?,?,?,?, 'CNY',?)", (str(uuid4()), code, name, entity_code, starts_on, ends_on, now()))
            return self.reply(201, {"code":code,"name":name,"entity_code":entity_code,"currency":"CNY"})

    def save_project_budgets(self, data: dict) -> None:
        code = str(data.get("project_code", "")).strip()
        lines = data.get("lines")
        if not code or not isinstance(lines, list) or not lines:
            raise ValueError("project_code and budget lines are required")
        normalized: dict[str, Decimal] = {}
        for line in lines:
            category = str(line.get("category", "")).strip()
            value = amount(line.get("budget_amount", "0"), "budget_amount")
            if category not in EXPENSE_CATEGORIES or value < 0:
                raise ValueError("budget category must be supported and amount cannot be negative")
            normalized[category] = value
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM projects WHERE code=?", (code,)).fetchone():
                raise ValueError("project not found")
            for category, value in normalized.items():
                conn.execute("INSERT INTO project_budgets (id,project_code,category,budget_amount,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(project_code,category) DO UPDATE SET budget_amount=excluded.budget_amount,updated_at=excluded.updated_at", (str(uuid4()), code, category, str(value), now()))
            return self.reply(200, {"project_code":code,"categories_updated":len(normalized)})

    def dispatch_sample(self, data: dict) -> None:
        source_key = str(data.get("source_key", "")).strip()
        project_code = str(data.get("project_code", "")).strip()
        sku = str(data.get("sku", "")).strip()
        qty = int(data.get("quantity", 0))
        day = str(data.get("dispatched_on", date.today().isoformat()))
        if not source_key or not project_code or not sku or qty <= 0:
            raise ValueError("source key, project, SKU and positive quantity are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM sample_dispatches WHERE source_key=?", (source_key,)).fetchone()
            if existing:
                require_same_source_payload(existing,{"project_code":project_code,"sku":sku,"quantity":qty,"dispatched_on":day})
                return self.reply(200, {"id":existing["id"],"duplicate":True,"message":"sample dispatch already recorded"})
            ensure_open(conn, day)
            project = conn.execute("SELECT * FROM projects WHERE code=?", (project_code,)).fetchone()
            if not project:
                raise ValueError("project not found")
            rule = conn.execute("SELECT * FROM sku_packaging_rules WHERE sku=?", (sku,)).fetchone()
            if not rule:
                raise ValueError(f"packaging rule is required for SKU {sku}")
            if self.inventory_currency(conn, sku) != "CNY":
                raise ValueError("exhibition samples currently require CNY inventory")
            packaging_sku = rule["packaging_sku"]
            per_unit = int(rule["packaging_per_unit"])
            included = bool(rule["packaging_included"])
            if per_unit > 0 and not included and self.inventory_currency(conn, str(packaging_sku)) != "CNY":
                raise ValueError("sample packaging must use CNY inventory")
            dispatch_id = str(uuid4())
            conn.execute("INSERT INTO sample_dispatches (id,source_key,project_code,sku,quantity,returned_quantity,packaging_sku,packaging_per_unit,packaging_included,dispatched_on,total_cost,currency,created_at,accounting_treatment) VALUES(?,?,?,?,?,0,?,?,?,?, '0.00','CNY',?,?)", (dispatch_id, source_key, project_code, sku, qty, packaging_sku, per_unit, int(included), day, now(),"asset_transfer"))
            item_layers: list[dict] = []
            total_cost = self.consume_fifo(conn, sku, qty, dispatch_id, day, layer_sink=item_layers, movement_type="sample_dispatch", source_type="exhibition_sample")
            package_layers: list[dict] = []
            if per_unit > 0 and not included:
                total_cost += self.consume_fifo(conn, str(packaging_sku), qty*per_unit, dispatch_id, day, layer_sink=package_layers, movement_type="sample_dispatch", source_type="exhibition_sample")
            for role, layers in (("item",item_layers),("packaging",package_layers)):
                for layer in layers:
                    conn.execute("INSERT INTO sample_dispatch_layers (id,dispatch_id,item_role,sku,stock_layer_id,quantity,returned_quantity,unit_cost,currency) VALUES(?,?,?,?,?,?,0,?,?)", (str(uuid4()), dispatch_id, role, layer["sku"], layer["stock_layer_id"], layer["quantity"], layer["unit_cost"], layer["currency"]))
            total_cost = total_cost.quantize(CENT)
            conn.execute("UPDATE sample_dispatches SET total_cost=? WHERE id=?", (str(total_cost), dispatch_id))
            journal(conn, "exhibition_sample", dispatch_id, day, [("inventory:exhibition", total_cost, Decimal(0)), ("inventory", Decimal(0), total_cost)], "coffee", project["entity_code"], project_code)
            return self.reply(201, {"id":dispatch_id,"duplicate":False,"project_code":project_code,"sku":sku,"quantity":qty,"cost":str(total_cost),"currency":"CNY"})

    def return_sample(self, data: dict) -> None:
        source_key = str(data.get("source_key", "")).strip()
        dispatch_id = str(data.get("dispatch_id", "")).strip()
        qty = int(data.get("quantity", 0))
        day = str(data.get("returned_on", date.today().isoformat()))
        if not source_key or not dispatch_id or qty <= 0:
            raise ValueError("source key, sample dispatch and positive return quantity are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT * FROM sample_returns WHERE source_key=?", (source_key,)).fetchone()
            if prior:
                require_same_source_payload(prior,{"dispatch_id":dispatch_id,"returned_on":day,"quantity":qty})
                return self.reply(200, {"id":prior["id"],"duplicate":True,"message":"sample return already recorded"})
            ensure_open(conn, day)
            dispatch = conn.execute("SELECT * FROM sample_dispatches WHERE id=?", (dispatch_id,)).fetchone()
            if not dispatch:
                raise ValueError("sample dispatch not found")
            if dispatch["returned_quantity"] + dispatch["disposed_quantity"] + qty > dispatch["quantity"]:
                raise ValueError("returned sample quantity exceeds the outstanding dispatched quantity")
            if day<dispatch["dispatched_on"]:
                raise ValueError("return date cannot precede sample dispatch")
            project = conn.execute("SELECT entity_code FROM projects WHERE code=?", (dispatch["project_code"],)).fetchone()
            return_id = str(uuid4())
            conn.execute("INSERT INTO sample_returns (id,source_key,dispatch_id,returned_on,quantity,total_cost,created_at) VALUES(?,?,?,?,?,'0.00',?)", (return_id, source_key, dispatch_id, day, qty, now()))
            restored_cost = Decimal(0)
            role_quantities = {"item":qty}
            if dispatch["packaging_per_unit"] > 0 and not dispatch["packaging_included"]:
                role_quantities["packaging"] = qty * dispatch["packaging_per_unit"]
            for role, required in role_quantities.items():
                layers = conn.execute("SELECT l.* FROM sample_dispatch_layers l JOIN stock_layers s ON s.id=l.stock_layer_id WHERE l.dispatch_id=? AND l.item_role=? AND l.returned_quantity+l.disposed_quantity<l.quantity ORDER BY s.created_at,s.id", (dispatch_id, role)).fetchall()
                available = sum(layer["quantity"]-layer["returned_quantity"]-layer["disposed_quantity"] for layer in layers)
                if available < required:
                    raise ValueError("sample return layer history is incomplete; inventory was not restored")
                remaining = required
                for layer in layers:
                    if not remaining:
                        break
                    take = min(remaining, layer["quantity"]-layer["returned_quantity"]-layer["disposed_quantity"])
                    rate_row=conn.execute("SELECT COALESCE(p.exchange_rate,s.exchange_rate) AS exchange_rate FROM stock_layers s LEFT JOIN purchases p ON p.id=s.purchase_id WHERE s.id=?",(layer["stock_layer_id"],)).fetchone()
                    exchange=Decimal(rate_row["exchange_rate"]) if rate_row else Decimal(1)
                    cost = Decimal(layer["unit_cost"]) * take * exchange
                    conn.execute("UPDATE stock_layers SET remaining_qty=remaining_qty+? WHERE id=?", (take, layer["stock_layer_id"]))
                    conn.execute("UPDATE sample_dispatch_layers SET returned_quantity=returned_quantity+? WHERE id=?", (take, layer["id"]))
                    base_cost=(Decimal(layer["unit_cost"])*Decimal(rate_row["exchange_rate"])).quantize(Decimal("0.00000001"),rounding=ROUND_HALF_UP) if rate_row else None
                    conn.execute("INSERT INTO inventory_movements (id,sku,movement_type,quantity,unit_cost,source_type,source_id,created_at,currency,effective_on,base_unit_cost_cny) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (str(uuid4()), layer["sku"], "sample_return", take, layer["unit_cost"], "exhibition_sample_return", return_id, now(), layer["currency"],day,str(base_cost) if base_cost is not None else None))
                    restored_cost += cost
                    remaining -= take
            restored_cost = restored_cost.quantize(CENT)
            conn.execute("UPDATE sample_dispatches SET returned_quantity=returned_quantity+? WHERE id=?", (qty, dispatch_id))
            conn.execute("UPDATE sample_returns SET total_cost=? WHERE id=?", (str(restored_cost), return_id))
            reverse_account="inventory:exhibition" if dispatch["accounting_treatment"]=="asset_transfer" else "operating_expense:sample_cost"
            line_business="coffee" if dispatch["accounting_treatment"]=="asset_transfer" else "exhibition"
            journal(conn, "exhibition_sample_return", return_id, day, [("inventory", restored_cost, Decimal(0)), (reverse_account, Decimal(0), restored_cost)], line_business, project["entity_code"], dispatch["project_code"])
            return self.reply(201, {"id":return_id,"duplicate":False,"dispatch_id":dispatch_id,"returned_quantity":qty,"restored_cost":str(restored_cost),"currency":"CNY"})

    def dispose_sample(self, data: dict) -> None:
        """Confirm actual sample consumption/gifting/damage; do not alter main stock twice."""
        source_key=str(data.get("source_key") or "").strip()
        dispatch_id=str(data.get("dispatch_id") or "").strip()
        reason=str(data.get("reason") or "consumed").strip()
        day=str(data.get("disposed_on") or date.today().isoformat())
        qty=whole_quantity(data.get("quantity",0),"sample disposed quantity")
        try:
            datetime.strptime(day,"%Y-%m-%d")
        except ValueError:
            raise ValueError("sample disposition date must use YYYY-MM-DD")
        if not source_key or not dispatch_id or qty<=0 or reason not in ("consumed","gifted","damaged"):
            raise ValueError("sample disposition needs a stable key, source dispatch, positive quantity and valid reason")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior=conn.execute("SELECT * FROM sample_dispositions WHERE source_key=?",(source_key,)).fetchone()
            if prior:
                require_same_source_payload(prior,{"dispatch_id":dispatch_id,"quantity":qty,
                                                   "reason":reason,"disposed_on":day})
                return self.reply(200,{"id":prior["id"],"duplicate":True,
                                       "total_cost":prior["total_cost"]})
            dispatch=conn.execute("SELECT * FROM sample_dispatches WHERE id=?",(dispatch_id,)).fetchone()
            if not dispatch:
                raise ValueError("sample dispatch does not exist")
            if dispatch["accounting_treatment"]!="asset_transfer":
                raise ValueError("legacy expensed dispatch cannot be disposed again; reconcile the historical ledger")
            if day<dispatch["dispatched_on"]:
                raise ValueError("disposition date cannot precede sample dispatch")
            if dispatch["returned_quantity"]+dispatch["disposed_quantity"]+qty>dispatch["quantity"]:
                raise ValueError("disposition exceeds outstanding outside samples")
            ensure_open(conn,day)
            project=conn.execute("SELECT entity_code FROM projects WHERE code=?",
                                 (dispatch["project_code"],)).fetchone()
            if not project:
                raise ValueError("exhibition project is missing")
            disposition_id=str(uuid4())
            conn.execute(
                "INSERT INTO sample_dispositions (id,source_key,dispatch_id,disposed_on,quantity,reason,total_cost,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (disposition_id,source_key,dispatch_id,day,qty,reason,"0.00",now()),
            )
            cost_total=Decimal(0)
            role_quantities={"item":qty}
            if int(dispatch["packaging_per_unit"])>0 and not dispatch["packaging_included"]:
                role_quantities["packaging"]=qty*int(dispatch["packaging_per_unit"])
            for role,required in role_quantities.items():
                layers=conn.execute(
                    "SELECT l.* FROM sample_dispatch_layers l "
                    "JOIN stock_layers s ON s.id=l.stock_layer_id "
                    "WHERE l.dispatch_id=? AND l.item_role=? "
                    "AND l.returned_quantity+l.disposed_quantity<l.quantity "
                    "ORDER BY s.created_at,s.id",
                    (dispatch_id,role),
                ).fetchall()
                if sum(layer["quantity"]-layer["returned_quantity"]-layer["disposed_quantity"] for layer in layers)<required:
                    raise ValueError("disposition quantity exceeds original FIFO layer balance")
                remaining=required
                for layer in layers:
                    if not remaining:
                        break
                    taken=min(remaining,layer["quantity"]-layer["returned_quantity"]-layer["disposed_quantity"])
                    fx=conn.execute(
                        "SELECT COALESCE(p.exchange_rate,s.exchange_rate) AS exchange_rate "
                        "FROM stock_layers s LEFT JOIN purchases p ON p.id=s.purchase_id WHERE s.id=?",
                        (layer["stock_layer_id"],)
                    ).fetchone()
                    if not fx:
                        raise ValueError("original sample stock layer does not exist")
                    portion=Decimal(layer["unit_cost"])*taken*Decimal(fx["exchange_rate"])
                    cost_total+=portion
                    conn.execute("UPDATE sample_dispatch_layers SET disposed_quantity=disposed_quantity+? WHERE id=?",
                                 (taken,layer["id"]))
                    conn.execute(
                        "INSERT INTO sample_disposition_layers (id,disposition_id,dispatch_layer_id,sku,quantity,cost_cny) VALUES(?,?,?,?,?,?)",
                        (str(uuid4()),disposition_id,layer["id"],layer["sku"],taken,str(portion))
                    )
                    remaining-=taken
            total=cost_total.quantize(CENT,rounding=ROUND_HALF_UP)
            conn.execute("UPDATE sample_dispositions SET total_cost=? WHERE id=?",
                         (str(total),disposition_id))
            conn.execute("UPDATE sample_dispatches SET disposed_quantity=disposed_quantity+? WHERE id=?",
                         (qty,dispatch_id))
            journal(conn,"exhibition_sample_disposition",disposition_id,day,[
                ("operating_expense:sample_cost",total,Decimal(0)),
                ("inventory:exhibition",Decimal(0),total)
            ],"coffee",project["entity_code"],dispatch["project_code"])
            return self.reply(201,{"id":disposition_id,"duplicate":False,
                "disposed_quantity":qty,"reason":reason,"total_cost":str(total),
                "project_code":dispatch["project_code"],"currency":"CNY"})

    def receive(self, purchase_id: str, data: dict) -> None:
        qty = whole_quantity(data["quantity"], "receipt quantity")
        source_key = str(data.get("source_key", "")).strip()
        day = str(data.get("received_on", date.today().isoformat()))
        if qty <= 0 or not source_key:
            raise ValueError("receipt quantity must be positive and stable source_key is required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior=conn.execute("SELECT id,purchase_id,quantity,received_on FROM purchase_receipts WHERE source_key=?",(source_key,)).fetchone()
            if prior:
                if prior["purchase_id"]!=purchase_id or int(prior["quantity"])!=qty or prior["received_on"]!=day:
                    raise ValueError("receipt source_key was already used for a different purchase, quantity, or date")
                return self.reply(200,{"receipt_id":prior["id"],"purchase_id":purchase_id,"duplicate":True,"message":"receipt already recorded"})
            row = conn.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
            if not row:
                raise ValueError("purchase not found")
            ensure_open(conn, day)
            if row["received_qty"] + qty > row["ordered_qty"]:
                raise ValueError("receipt exceeds outstanding purchase quantity")
            receipt_id = str(uuid4())
            conn.execute("INSERT INTO purchase_receipts (id,purchase_id,source_key,quantity,received_on,created_at) VALUES(?,?,?,?,?,?)",(receipt_id,purchase_id,source_key,qty,day,now()))
            cost = Decimal(row["unit_cost"])
            conn.execute("UPDATE purchases SET received_qty=received_qty+? WHERE id=?", (qty, purchase_id))
            layer_id=str(uuid4())
            conn.execute("INSERT INTO stock_layers (id,purchase_id,sku,received_qty,remaining_qty,unit_cost,created_at,purchase_receipt_id) VALUES(?,?,?,?,?,?,?,?)", (layer_id, purchase_id, row["sku"], qty, qty, str(cost), now(), receipt_id))
            base_unit_cost=(cost*Decimal(row["exchange_rate"])).quantize(Decimal("0.00000001"),rounding=ROUND_HALF_UP)
            conn.execute("INSERT INTO inventory_movements (id,sku,movement_type,quantity,unit_cost,source_type,source_id,created_at,currency,effective_on,base_unit_cost_cny) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (str(uuid4()), row["sku"], "receipt", qty, str(cost), "purchase_receipt", receipt_id, now(), row["currency"],day,str(base_unit_cost)))
            total = (cost * qty).quantize(CENT)
            rate=Decimal(row["exchange_rate"])
            total_cny=(total*rate).quantize(CENT,rounding=ROUND_HALF_UP)
            journal(conn, "purchase_receipt", receipt_id, day, [("inventory", total_cny, Decimal(0)), ("accounts_payable", Decimal(0), total_cny)], row["business_line"], row["entity_code"], row["project_code"], "CNY")
            payable_id=str(uuid4())
            conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00',?,NULL,?)", (payable_id, "payable", "purchase_receipt", receipt_id, row["supplier"], str(total), row["currency"], now()))
            record_open_item_fx(conn,payable_id,total_cny,rate,row["rate_date"] or day,"purchase_contract_rate")
            return self.reply(201, {"receipt_id": receipt_id, "purchase_id": purchase_id, "sku": row["sku"], "quantity": qty, "inventory_value": str(total), "base_inventory_value_cny":str(total_cny), "currency": row["currency"], "exchange_rate": row["exchange_rate"], "rate_date": row["rate_date"], "tax_status": "not_claimed_without_invoice"})

    def edit_purchase_receipt(self,receipt_id:str,data:dict)->None:
        qty=whole_quantity(data.get("quantity",0),"receipt quantity")
        action_key=str(data.get("action_key","")).strip();changed_by=str(data.get("changed_by","local-unverified")).strip()
        if qty<=0 or not action_key or not changed_by:raise ValueError("positive receipt quantity, action_key and changed_by are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior=conn.execute("SELECT receipt_id,new_quantity,changed_by FROM purchase_receipt_edit_history WHERE action_key=?",(action_key,)).fetchone()
            if prior:
                if prior["receipt_id"]!=receipt_id or int(prior["new_quantity"])!=qty or prior["changed_by"]!=changed_by:raise ValueError("action_key was already used for a different receipt edit")
                return self.reply(200,{"receipt_id":receipt_id,"quantity":qty,"duplicate":True})
            receipt=conn.execute("SELECT r.id,r.purchase_id,r.quantity,r.received_on,p.ordered_qty,p.received_qty,p.supplier,p.sku,p.unit_cost,p.currency,p.exchange_rate,p.rate_date,p.business_line,p.entity_code,p.project_code FROM purchase_receipts r JOIN purchases p ON p.id=r.purchase_id WHERE r.id=?",(receipt_id,)).fetchone()
            if not receipt:raise ValueError("purchase receipt not found")
            ensure_open(conn,receipt["received_on"])
            old_qty=int(receipt["quantity"]);delta=qty-old_qty;other_received=int(receipt["received_qty"])-old_qty
            if other_received+qty>int(receipt["ordered_qty"]):raise ValueError("edited receipt would exceed the purchase order quantity")
            layer=conn.execute("SELECT * FROM stock_layers WHERE purchase_receipt_id=?",(receipt_id,)).fetchone()
            if not layer:raise ValueError("receipt has no linked inventory layer; it cannot be edited safely")
            if int(layer["remaining_qty"])+delta<0:raise ValueError("receipt cannot be reduced below quantity already sold or used from this inventory layer")
            cost=Decimal(receipt["unit_cost"]);rate=Decimal(receipt["exchange_rate"]);new_original=(cost*qty).quantize(CENT,rounding=ROUND_HALF_UP);new_cny=(new_original*rate).quantize(CENT,rounding=ROUND_HALF_UP)
            payable=conn.execute("SELECT * FROM receivables_payables WHERE direction='payable' AND source_type='purchase_receipt' AND source_id=?",(receipt_id,)).fetchone()
            if not payable:raise ValueError("receipt payable record is missing; reconcile the purchase subledger before editing this receipt")
            if new_original<Decimal(payable["settled_amount"]):raise ValueError("edited receipt amount would be less than payments already allocated")
            linked=conn.execute("SELECT COALESCE(SUM(linked_amount),'0') FROM invoice_links WHERE target_type='payable' AND target_id=?",(payable["id"],)).fetchone()[0]
            if new_original<Decimal(linked):raise ValueError("edited receipt amount would be less than invoices already linked")
            fx=conn.execute("SELECT * FROM open_item_fx WHERE item_id=?",(payable["id"],)).fetchone()
            if not fx:raise ValueError("receipt payable CNY carrying value is missing; reconcile the subledger before editing this receipt")
            if new_cny<Decimal(fx["settled_amount_cny"]):raise ValueError("edited receipt value would be less than the CNY value already settled")
            entry=conn.execute("SELECT id FROM journal_entries WHERE source_type='purchase_receipt' AND source_id=?",(receipt_id,)).fetchone()
            if not entry:raise ValueError("receipt journal entry is missing; it cannot be edited safely")
            updated=now()
            conn.execute("UPDATE purchase_receipts SET quantity=? WHERE id=?",(qty,receipt_id))
            conn.execute("UPDATE purchases SET received_qty=? WHERE id=?",(other_received+qty,receipt["purchase_id"]))
            conn.execute("UPDATE stock_layers SET received_qty=?,remaining_qty=remaining_qty+? WHERE id=?",(qty,delta,layer["id"]))
            movement_update=conn.execute("UPDATE inventory_movements SET quantity=? WHERE source_type='purchase_receipt' AND source_id=?",(qty,receipt_id))
            inventory_line=conn.execute("UPDATE journal_lines SET debit=?,credit='0.00' WHERE entry_id=? AND account='inventory'",(str(new_cny),entry["id"]))
            payable_line=conn.execute("UPDATE journal_lines SET debit='0.00',credit=? WHERE entry_id=? AND account='accounts_payable'",(str(new_cny),entry["id"]))
            if movement_update.rowcount!=1 or inventory_line.rowcount!=1 or payable_line.rowcount!=1:raise ValueError("receipt inventory or journal linkage is incomplete; no changes were saved")
            conn.execute("UPDATE receivables_payables SET original_amount=? WHERE id=?",(str(new_original),payable["id"]))
            conn.execute("UPDATE open_item_fx SET original_amount_cny=? WHERE item_id=?",(str(new_cny),payable["id"]))
            conn.execute("INSERT INTO purchase_receipt_edit_history VALUES(?,?,?,?,?,?,?)",(str(uuid4()),receipt_id,old_qty,qty,action_key,updated,changed_by))
            return self.reply(200,{"receipt_id":receipt_id,"purchase_id":receipt["purchase_id"],"previous_quantity":old_qty,"quantity":qty,"inventory_quantity_delta":delta,"currency":receipt["currency"],"updated_at":updated,"operator_authenticated":False})

    def import_sale(self, data: dict) -> None:
        key = str(data.get("source_key") or self.headers.get("Idempotency-Key", "")).strip()
        source = str(data.get("source", "manual"))
        day = str(data.get("sold_on", date.today().isoformat()))
        business_line = str(data.get("business_line", "coffee")).strip()
        entity_code = str(data.get("entity_code", "main")).strip()
        project_code = str(data.get("project_code", "")).strip() or None
        lines = data.get("lines")
        if not key or not isinstance(lines, list) or not lines or business_line != "coffee" or not entity_code:
            raise ValueError("stable source_key, coffee business line, legal entity and at least one sale line are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ensure_open(conn, day)
            if conn.execute("SELECT 1 FROM sales_imports WHERE source_key=?", (key,)).fetchone():
                return self.reply(200, {"source_key": key, "duplicate": True, "message": "source already imported; no stock or journal changes made"})
            if project_code:
                project = conn.execute("SELECT entity_code FROM projects WHERE code=?", (project_code,)).fetchone()
                if not project or project["entity_code"] != entity_code:
                    raise ValueError("project code must exist and match the selected legal entity")
            sale_currencies = set()
            for line in lines:
                sku = str(line["sku"]).strip()
                qty = int(line["quantity"])
                if not sku or qty <= 0:
                    raise ValueError("each sale line requires SKU and positive quantity")
                rule = conn.execute("SELECT * FROM sku_packaging_rules WHERE sku=?", (sku,)).fetchone()
                if not rule:
                    raise ValueError(f"packaging rule is required for SKU {sku}; configure the SKU packaging rule before sales import")
                sale_currencies.add(self.inventory_currency(conn, sku))
                if rule["packaging_per_unit"] > 0 and not rule["packaging_included"]:
                    sale_currencies.add(self.inventory_currency(conn, str(rule["packaging_sku"])))
            if len(sale_currencies) != 1:
                raise ValueError("sale contains inventory in multiple currencies; reviewed conversion policy is required")
            if sale_currencies != {"CNY"}:
                raise ValueError("sales from foreign-currency inventory are disabled until a reviewed functional-currency conversion policy is configured")
            import_id = str(uuid4())
            conn.execute("INSERT INTO sales_imports (id,source_key,source,sold_on,created_at,business_line,entity_code,project_code) VALUES(?,?,?,?,?,?,?,?)", (import_id, key, source, day, now(), business_line, entity_code, project_code))
            summary=[]; cogs=Decimal(0); revenue=Decimal(0)
            for index,line in enumerate(lines,1):
                sku=str(line["sku"]).strip();qty=int(line["quantity"]);price=amount(line["unit_price"],"unit_price")
                if not sku or qty<=0 or price<0: raise ValueError("each sale line requires SKU, positive quantity, non-negative unit price")
                rule = conn.execute("SELECT * FROM sku_packaging_rules WHERE sku=?", (sku,)).fetchone()
                if not rule:
                    raise ValueError(f"packaging rule is required for SKU {sku}; configure the SKU packaging rule before sales import")
                line_currency = self.inventory_currency(conn, sku)
                row_key=str(line.get("source_row_key") or f"{key}:{index}")
                sale_line_id=str(uuid4())
                conn.execute("INSERT INTO sale_lines (id,import_id,source_row_key,sku,quantity,unit_price,packaging_sku,packaging_per_unit,packaging_included,cogs_cny,channel) VALUES(?,?,?,?,?,?,?,?,?,NULL,?)",(sale_line_id,import_id,row_key,sku,qty,str(price),rule["packaging_sku"],rule["packaging_per_unit"],rule["packaging_included"],str(line.get("channel","")).strip()))
                line_layers=[]
                line_cost=self.consume_fifo(conn,sku,qty,import_id,day,layer_sink=line_layers,sale_line_id=sale_line_id)
                packaging_sku=rule["packaging_sku"];per_unit=rule["packaging_per_unit"];included=bool(rule["packaging_included"])
                if per_unit and not included:
                    package_currency = self.inventory_currency(conn, str(packaging_sku))
                    if package_currency != line_currency:
                        raise ValueError("finished goods and packaging use different currencies; landed-cost conversion policy is not configured")
                    line_cost+=self.consume_fifo(conn,str(packaging_sku),qty*per_unit,import_id,day,layer_sink=line_layers,sale_line_id=sale_line_id)
                line_revenue=(price*qty).quantize(CENT);cogs+=line_cost;revenue+=line_revenue
                line_cost=line_cost.quantize(CENT,rounding=ROUND_HALF_UP)
                self.assign_sale_line_cost(conn,line_layers,line_cost)
                conn.execute("UPDATE sale_lines SET cogs_cny=? WHERE id=?",(str(line_cost),sale_line_id))
                summary.append({"sku":sku,"quantity":qty,"revenue":str(line_revenue),"cost_of_goods_sold":str(line_cost.quantize(CENT))})
            cogs=cogs.quantize(CENT);revenue=revenue.quantize(CENT)
            journal(conn,"sale",import_id,day,[("accounts_receivable",revenue,Decimal(0)),("sales_revenue",Decimal(0),revenue),("cost_of_goods_sold",cogs,Decimal(0)),("inventory",Decimal(0),cogs)], business_line, entity_code, project_code)
            conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00','CNY',NULL,?)",(str(uuid4()),"receivable","sale",import_id,str(data.get("customer","未指定客户")),str(revenue),now()))
            return self.reply(201,{"import_id":import_id,"duplicate":False,"lines":summary,"revenue":str(revenue),"cost_of_goods_sold":str(cogs),"gross_profit":str(revenue-cogs)})

    def import_sale_row(self, data: dict) -> None:
        archive_id = str(data.get("archive_file_id") or "").strip()
        # Files are upload carriers, not business identities. A renamed or
        # re-exported CSV must not post the same marketplace sale twice.
        identity = normalize_sale_source(data)
        raw_row_key = str(data.get("source_row_key", "")).strip()
        row_key = str(identity["source_row_key"])
        data = {**data, "source_row_key": row_key}
        if identity["structured"]:
            data.update({
                "source": identity["source"],
                "channel": identity["channel"],
                "store_name": identity["store_name"],
                "external_order_no": identity["external_order_no"],
                "external_line_id": identity["external_line_id"],
                "source_row_key_raw": raw_row_key,
            })
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT status,result FROM import_rows WHERE source_key=?", (row_key,)).fetchone()
            if prior and prior["status"] == "posted":
                prior_result=json.loads(prior["result"])
                import_id=prior_result.get("import_id")
                posted=conn.execute("SELECT i.source,i.sold_on,i.business_line,i.entity_code,i.project_code,s.sku,s.quantity,s.unit_price,s.channel,r.counterparty FROM sales_imports i JOIN sale_lines s ON s.import_id=i.id LEFT JOIN receivables_payables r ON r.source_type='sale' AND r.source_id=i.id AND r.direction='receivable' WHERE i.id=? AND s.source_row_key=?",(import_id,row_key)).fetchone() if import_id else None
                if not posted:
                    posted=conn.execute("SELECT i.source,i.sold_on,i.business_line,i.entity_code,i.project_code,s.sku,s.quantity,s.unit_price,s.channel,r.counterparty FROM sales_imports i JOIN sale_lines s ON s.import_id=i.id LEFT JOIN receivables_payables r ON r.source_type='sale' AND r.source_id=i.id AND r.direction='receivable' WHERE i.source_key=? AND s.source_row_key=?",(row_key,row_key)).fetchone()
                if not posted:
                    raise ValueError("source row is already posted but its original payload cannot be verified; reconcile the sales record")
                submitted={"source":str(data.get("source","file_import")),"sold_on":str(data.get("sold_on",date.today().isoformat())),"business_line":str(data.get("business_line","coffee")).strip(),"entity_code":str(data.get("entity_code","main")).strip(),"project_code":str(data.get("project_code") or "").strip() or None,"sku":str(data.get("sku","")).strip(),"quantity":whole_quantity(data.get("quantity",0),"quantity"),"unit_price":str(amount(data.get("unit_price","0"),"unit_price")),"channel":str(data.get("channel","")).strip(),"customer":str(data.get("customer","未指定客户")).strip() or "未指定客户"}
                recorded={"source":posted["source"],"sold_on":posted["sold_on"],"business_line":posted["business_line"],"entity_code":posted["entity_code"],"project_code":posted["project_code"],"sku":posted["sku"],"quantity":int(posted["quantity"]),"unit_price":str(amount(posted["unit_price"],"recorded unit_price")),"channel":posted["channel"] or "","customer":posted["counterparty"] or "未指定客户"}
                if submitted!=recorded:
                    raise ValueError("source row is already posted with different business data; reconcile it with a sales return or reviewed correction")
                if archive_id:
                    original_archive.link_document(conn,archive_id,"coffee.sale",row_key)
                return self.reply(200, {"duplicate": True, "status": "posted", "result": json.loads(prior["result"])})
            if prior:
                conn.execute("DELETE FROM import_rows WHERE source_key=?", (row_key,))
            conn.execute("SAVEPOINT import_row")
            try:
                payload = {"source": data.get("source", "file_import"), "sold_on": data.get("sold_on", date.today().isoformat()), "sku": data["sku"], "quantity": data["quantity"], "unit_price": data["unit_price"], "channel": str(data.get("channel", "")).strip(), "business_line": data.get("business_line", "coffee"), "entity_code": data.get("entity_code", "main"), "project_code": data.get("project_code"), "customer": data.get("customer", "未指定客户")}
                result = self.post_sale(conn, payload, row_key)
                if archive_id:
                    original_archive.link_document(conn,archive_id,"coffee.sale",row_key)
                conn.execute("INSERT INTO import_rows VALUES(?,?,?,?,?,?)", (str(uuid4()), row_key, json.dumps(data, ensure_ascii=False), "posted", json.dumps(result, ensure_ascii=False), now()))
                conn.execute("RELEASE SAVEPOINT import_row")
                return self.reply(201, {"duplicate": False, "status": "posted", "result": result})
            except (ValueError, KeyError, TypeError) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT import_row")
                conn.execute("RELEASE SAVEPOINT import_row")
                result = {"error": str(exc)}
                conn.execute("INSERT INTO import_rows VALUES(?,?,?,?,?,?)", (str(uuid4()), row_key, json.dumps(data, ensure_ascii=False), "rejected", json.dumps(result, ensure_ascii=False), now()))
                return self.reply(422, {"duplicate": False, "status": "rejected", "result": result})

    def post_sale(self, conn: sqlite3.Connection, data: dict, row_key: str) -> dict:
        day = str(data.get("sold_on", date.today().isoformat()))
        business_line = str(data.get("business_line", "coffee")).strip()
        entity_code = str(data.get("entity_code", "main")).strip()
        project_code = str(data.get("project_code") or "").strip() or None
        if business_line != "coffee" or not entity_code:
            raise ValueError("coffee sales require business_line=coffee and a legal entity code")
        ensure_open(conn, day)
        if project_code:
            project = conn.execute("SELECT entity_code FROM projects WHERE code=?", (project_code,)).fetchone()
            if not project or project["entity_code"] != entity_code:
                raise ValueError("project code must exist and match the selected legal entity")
        sku, qty = str(data["sku"]).strip(), int(data["quantity"])
        price = amount(data["unit_price"], "unit_price")
        if not sku or qty <= 0 or price < 0:
            raise ValueError("SKU, positive quantity and non-negative unit price are required")
        rule = conn.execute("SELECT * FROM sku_packaging_rules WHERE sku=?", (sku,)).fetchone()
        if not rule:
            raise ValueError(f"packaging rule is required for SKU {sku}; configure the SKU packaging rule before sales import")
        sale_currency = self.inventory_currency(conn, sku)
        if sale_currency != "CNY":
            raise ValueError("sales from foreign-currency inventory are disabled until a reviewed functional-currency conversion policy is configured")
        packaging_sku = rule["packaging_sku"]
        per_unit, included = rule["packaging_per_unit"], bool(rule["packaging_included"])
        if per_unit > 0 and not included:
            package_currency = self.inventory_currency(conn, str(packaging_sku))
            if package_currency != sale_currency:
                raise ValueError("finished goods and packaging use different currencies; landed-cost conversion policy is not configured")
        import_id = str(uuid4())
        conn.execute("INSERT INTO sales_imports (id,source_key,source,sold_on,created_at,business_line,entity_code,project_code) VALUES(?,?,?,?,?,?,?,?)", (import_id, row_key, str(data.get("source", "file_import")), day, now(), business_line, entity_code, project_code))
        sale_line_id=str(uuid4())
        conn.execute("INSERT INTO sale_lines (id,import_id,source_row_key,sku,quantity,unit_price,packaging_sku,packaging_per_unit,packaging_included,cogs_cny,channel) VALUES(?,?,?,?,?,?,?,?,?,NULL,?)",(sale_line_id,import_id,row_key,sku,qty,str(price),packaging_sku,per_unit,int(included),str(data.get("channel", "")).strip()))
        line_layers=[]
        cogs = self.consume_fifo(conn, sku, qty, import_id, day,layer_sink=line_layers,sale_line_id=sale_line_id)
        if per_unit and not included:
            cogs += self.consume_fifo(conn, str(packaging_sku), qty * per_unit, import_id, day,layer_sink=line_layers,sale_line_id=sale_line_id)
        revenue = (price * qty).quantize(CENT)
        cogs = cogs.quantize(CENT)
        self.assign_sale_line_cost(conn,line_layers,cogs)
        conn.execute("UPDATE sale_lines SET cogs_cny=? WHERE id=?",(str(cogs),sale_line_id))
        journal(conn, "sale", import_id, day, [("accounts_receivable", revenue, Decimal(0)), ("sales_revenue", Decimal(0), revenue), ("cost_of_goods_sold", cogs, Decimal(0)), ("inventory", Decimal(0), cogs)], business_line, entity_code, project_code)
        conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00','CNY',NULL,?)", (str(uuid4()), "receivable", "sale", import_id, str(data.get("customer", "未指定客户")), str(revenue), now()))
        return {"import_id": import_id, "sku": sku, "quantity": qty, "revenue": str(revenue), "cost_of_goods_sold": str(cogs), "gross_profit": str(revenue-cogs)}

    def inventory_currency(self, conn: sqlite3.Connection, sku: str) -> str:
        rows = conn.execute("SELECT DISTINCT COALESCE(p.currency,s.currency) AS currency FROM stock_layers s LEFT JOIN purchases p ON p.id=s.purchase_id WHERE s.sku=? AND s.remaining_qty>0", (sku,)).fetchall()
        if not rows:
            raise ValueError(f"no available inventory for SKU {sku}")
        if len(rows) != 1:
            raise ValueError(f"SKU {sku} has inventory in multiple currencies; reviewed currency conversion is required before sale")
        return rows[0]["currency"]

    def consume_fifo(self, conn: sqlite3.Connection, sku: str, qty: int, sale_id: str, day: str, layer_sink: list[dict] | None = None, movement_type: str = "sale", source_type: str = "sale", sale_line_id: str | None = None) -> Decimal:
        layers = conn.execute("SELECT s.*,COALESCE(p.currency,s.currency) AS currency,COALESCE(p.exchange_rate,s.exchange_rate) AS exchange_rate FROM stock_layers s LEFT JOIN purchases p ON p.id=s.purchase_id WHERE s.sku=? AND s.remaining_qty>0 ORDER BY s.created_at,s.id", (sku,)).fetchall()
        available = sum(row["remaining_qty"] for row in layers)
        if available < qty:
            raise ValueError(f"insufficient stock for SKU {sku}: available {available}, required {qty}")
        remaining = qty
        cost_total = Decimal(0)
        for layer in layers:
            if remaining == 0:
                break
            take = min(remaining, layer["remaining_qty"])
            cost = Decimal(layer["unit_cost"])
            cost_total += cost * take * Decimal(layer["exchange_rate"])
            conn.execute("UPDATE stock_layers SET remaining_qty=remaining_qty-? WHERE id=?", (take, layer["id"]))
            base_cost=(cost*Decimal(layer["exchange_rate"])).quantize(Decimal("0.00000001"),rounding=ROUND_HALF_UP)
            movement_id=str(uuid4())
            conn.execute("INSERT INTO inventory_movements (id,sku,movement_type,quantity,unit_cost,source_type,source_id,created_at,currency,effective_on,base_unit_cost_cny,sale_line_id,stock_layer_id,cogs_cny) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)", (movement_id, sku, movement_type, -take, str(cost), source_type, sale_id, now(), layer["currency"],day,str(base_cost),sale_line_id,layer["id"]))
            if layer_sink is not None:
                layer_sink.append({"movement_id":movement_id,"sku":sku,"stock_layer_id":layer["id"],"quantity":take,"unit_cost":str(cost),"currency":layer["currency"],"base_unit_cost_cny":str(base_cost)})
            remaining -= take
        return cost_total.quantize(CENT)

    def assign_sale_line_cost(self,conn:sqlite3.Connection,allocations:list[dict],total_cost:Decimal)->None:
        if not allocations:
            if total_cost!=0:raise ValueError("sale COGS cannot be assigned without traced FIFO layers")
            return
        raw=[Decimal(item["base_unit_cost_cny"])*int(item["quantity"]) for item in allocations]
        raw_total=sum(raw,Decimal(0))
        if raw_total<=0:
            if total_cost!=0:raise ValueError("sale COGS layer value must be positive")
            shares=[Decimal(0) for _ in allocations]
        else:
            remaining=total_cost.quantize(CENT,rounding=ROUND_HALF_UP)
            shares=[]
            for index,value in enumerate(raw):
                if index==len(raw)-1:share=remaining
                else:
                    share=(total_cost*value/raw_total).quantize(CENT,rounding=ROUND_DOWN)
                    remaining-=share
                shares.append(share)
        for item,share in zip(allocations,shares):
            conn.execute("UPDATE inventory_movements SET cogs_cny=? WHERE id=?",(str(share.quantize(CENT)),item["movement_id"]))

    def restore_sale_stock(self,conn:sqlite3.Connection,sale_line:sqlite3.Row,sales_return_id:str,sku:str,quantity:int,returned_on:str)->Decimal:
        if quantity==0:return Decimal(0)
        movements=conn.execute("SELECT * FROM inventory_movements WHERE movement_type='sale' AND sale_line_id=? AND sku=? ORDER BY created_at,id",(sale_line["id"],sku)).fetchall()
        if not movements or any(not row["stock_layer_id"] or row["cogs_cny"] is None for row in movements):
            raise ValueError("original FIFO layer cost cannot be traced for this SKU")
        remaining=quantity;total_cost=Decimal(0)
        for movement in movements:
            prior_rows=conn.execute("SELECT quantity,cogs_cny FROM sales_return_layers WHERE source_movement_id=?",(movement["id"],)).fetchall()
            prior_quantity=sum(int(row["quantity"]) for row in prior_rows)
            prior_cost=sum((Decimal(row["cogs_cny"]) for row in prior_rows),Decimal(0))
            original_qty=-int(movement["quantity"]);available=original_qty-prior_quantity
            if available<=0:continue
            take=min(remaining,available)
            original_cost=Decimal(movement["cogs_cny"])
            if take==available:allocated=(original_cost-prior_cost).quantize(CENT)
            else:allocated=(original_cost*Decimal(take)/Decimal(original_qty)).quantize(CENT,rounding=ROUND_HALF_UP)
            conn.execute("UPDATE stock_layers SET remaining_qty=remaining_qty+? WHERE id=?",(take,movement["stock_layer_id"]))
            conn.execute("INSERT INTO sales_return_layers (id,sales_return_id,sale_line_id,source_movement_id,stock_layer_id,sku,quantity,cogs_cny) VALUES(?,?,?,?,?,?,?,?)",(str(uuid4()),sales_return_id,sale_line["id"],movement["id"],movement["stock_layer_id"],sku,take,str(allocated)))
            conn.execute("INSERT INTO inventory_movements (id,sku,movement_type,quantity,unit_cost,source_type,source_id,created_at,currency,effective_on,base_unit_cost_cny,sale_line_id,stock_layer_id,cogs_cny) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(str(uuid4()),sku,"sale_return",take,movement["unit_cost"],"sale_return",sales_return_id,now(),movement["currency"],returned_on,movement["base_unit_cost_cny"],sale_line["id"],movement["stock_layer_id"],str(allocated)))
            total_cost+=allocated;remaining-=take
            if not remaining:break
        if remaining:
            raise ValueError(f"return quantity exceeds traced sold quantity for SKU {sku}")
        return total_cost.quantize(CENT)

    def return_sale_line(self,sale_line_id:str,data:dict)->None:
        source_key=str(data.get("source_key","")).strip();returned_on=str(data.get("returned_on",date.today().isoformat()))
        try:quantity=int(data.get("quantity",0));package_quantity=int(data.get("packaging_quantity",0));datetime.strptime(returned_on,"%Y-%m-%d")
        except (TypeError,ValueError):raise ValueError("return quantities must be integers and returned_on must use YYYY-MM-DD")
        if not source_key or quantity<=0 or package_quantity<0:raise ValueError("stable source_key, positive returned product quantity, and non-negative packaging quantity are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior=conn.execute("SELECT * FROM sales_returns WHERE source_key=?",(source_key,)).fetchone()
            if prior:
                require_same_source_payload(prior,{"sale_line_id":sale_line_id,"quantity":quantity,"packaging_quantity":package_quantity,"returned_on":returned_on})
                return self.reply(200,{"id":prior["id"],"duplicate":True,"revenue_reversed_cny":prior["revenue_cny"],"cogs_reversed_cny":prior["cogs_cny"]})
            line=conn.execute("SELECT l.*,i.sold_on,i.business_line,i.entity_code,i.project_code,i.id AS import_id FROM sale_lines l JOIN sales_imports i ON i.id=l.import_id WHERE l.id=?",(sale_line_id,)).fetchone()
            if not line:raise ValueError("sale line not found")
            ensure_open(conn,returned_on)
            returned=conn.execute("SELECT COALESCE(SUM(quantity),0) AS quantity,COALESCE(SUM(packaging_quantity),0) AS packaging_quantity FROM sales_returns WHERE sale_line_id=?",(sale_line_id,)).fetchone()
            quantity_remaining=int(line["quantity"])-int(returned["quantity"])
            if quantity>quantity_remaining:raise ValueError(f"return quantity exceeds unreturned sold quantity {quantity_remaining}")
            package_per=int(line["packaging_per_unit"]);package_cap=0 if line["packaging_included"] else quantity*package_per
            if package_quantity>package_cap:raise ValueError(f"returned packaging quantity cannot exceed {package_cap}")
            expected_package=int(line["quantity"])*package_per if not line["packaging_included"] else 0
            returned_revenue=(Decimal(line["unit_price"])*quantity).quantize(CENT,rounding=ROUND_HALF_UP)
            ar=conn.execute("SELECT * FROM receivables_payables WHERE direction='receivable' AND source_id=? AND source_type IN ('sale','legacy_sale') ORDER BY created_at LIMIT 1",(line["import_id"],)).fetchone()
            fx=conn.execute("SELECT * FROM open_item_fx WHERE item_id=?",(ar["id"],)).fetchone() if ar else None
            if not ar or not fx:raise ValueError("sale receivable cannot be traced; return requires finance review")
            open_ar=max(Decimal(0),Decimal(ar["original_amount"])-Decimal(ar["settled_amount"]))
            apply_to_ar=min(open_ar,returned_revenue).quantize(CENT)
            refund_due=(returned_revenue-apply_to_ar).quantize(CENT)
            return_id=str(uuid4())
            conn.execute("INSERT INTO sales_returns (id,source_key,sale_line_id,quantity,packaging_quantity,returned_on,revenue_cny,cogs_cny,created_at) VALUES(?,?,?,?,?,?,?,'0.00',?)",(return_id,source_key,sale_line_id,quantity,package_quantity,returned_on,str(returned_revenue),now()))
            cogs=self.restore_sale_stock(conn,line,return_id,line["sku"],quantity,returned_on)
            if expected_package:
                cogs+=self.restore_sale_stock(conn,line,return_id,str(line["packaging_sku"]),package_quantity,returned_on)
            cogs=cogs.quantize(CENT)
            conn.execute("UPDATE sales_returns SET cogs_cny=? WHERE id=?",(str(cogs),return_id))
            if apply_to_ar:
                conn.execute("UPDATE receivables_payables SET original_amount=? WHERE id=?",(str((Decimal(ar["original_amount"])-apply_to_ar).quantize(CENT)),ar["id"]))
                conn.execute("UPDATE open_item_fx SET original_amount_cny=? WHERE item_id=?",(str((Decimal(fx["original_amount_cny"])-apply_to_ar).quantize(CENT)),ar["id"]))
            if refund_due:
                payable_id=str(uuid4());conn.execute("INSERT INTO receivables_payables VALUES(?,?,?,?,?,?, '0.00','CNY',NULL,?)",(payable_id,"payable","sales_return",return_id,ar["counterparty"],str(refund_due),now()))
                record_open_item_fx(conn,payable_id,refund_due,Decimal(1),returned_on,"sales_return_refund_due")
            journal(conn,"sales_return",return_id,returned_on,[("sales_revenue",returned_revenue,Decimal(0)),("accounts_receivable",Decimal(0),apply_to_ar),("accounts_payable",Decimal(0),refund_due),("inventory",cogs,Decimal(0)),("cost_of_goods_sold",Decimal(0),cogs)],line["business_line"],line["entity_code"],line["project_code"],"CNY")
            return self.reply(201,{"id":return_id,"source_row_key":line["source_row_key"],"quantity":quantity,"packaging_quantity":package_quantity,"revenue_reversed_cny":str(returned_revenue),"ar_credit_cny":str(apply_to_ar),"customer_refund_due_cny":str(refund_due),"cogs_reversed_cny":str(cogs),"posting_status":"posted"})

    def review_bank_fx(self, data: dict) -> None:
        bank_id=str(data.get("bank_transaction_id","")).strip()
        rate=exchange_rate(data.get("exchange_rate","0"))
        rate_date=str(data.get("rate_date",""))
        rate_source=str(data.get("rate_source","")).strip()
        try:
            datetime.strptime(rate_date,"%Y-%m-%d")
        except ValueError:
            raise ValueError("rate_date must use YYYY-MM-DD format")
        if not bank_id or rate<=0 or not rate_source:
            raise ValueError("bank transaction, positive exchange rate, rate date and source are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT * FROM bank_transactions WHERE id=?",(bank_id,)).fetchone()
            if not row:
                raise ValueError("bank transaction not found")
            if row["currency"]=="CNY":
                raise ValueError("CNY bank transactions use the functional-currency rate of 1")
            if row["base_amount_cny"]:
                raise ValueError("bank transaction already has a reviewed CNY valuation")
            if conn.execute("SELECT 1 FROM bank_allocations WHERE bank_transaction_id=? LIMIT 1",(bank_id,)).fetchone():
                raise ValueError("bank transaction already has allocations; use a reviewed FX adjustment workflow")
            ensure_open(conn,row["transaction_on"])
            base=(Decimal(row["amount"])*rate).quantize(CENT,rounding=ROUND_HALF_UP)
            conn.execute("UPDATE bank_transactions SET exchange_rate=?,rate_date=?,rate_source=?,base_amount_cny=? WHERE id=?",(str(rate),rate_date,rate_source,str(base),bank_id))
            return self.reply(200,{"bank_transaction_id":bank_id,"exchange_rate":str(rate),"rate_date":rate_date,"rate_source":rate_source,"base_amount_cny":str(base)})

    def review_open_item_fx(self, data: dict) -> None:
        item_id=str(data.get("open_item_id","")).strip()
        rate=exchange_rate(data.get("exchange_rate","0"))
        rate_date=str(data.get("rate_date",""))
        rate_source=str(data.get("rate_source","")).strip()
        try:
            datetime.strptime(rate_date,"%Y-%m-%d")
        except ValueError:
            raise ValueError("rate_date must use YYYY-MM-DD format")
        if not item_id or rate<=0 or not rate_source:
            raise ValueError("open item, positive exchange rate, rate date and source are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item=conn.execute("SELECT * FROM receivables_payables WHERE id=?",(item_id,)).fetchone()
            if not item:
                raise ValueError("open item not found")
            ensure_open(conn,item["due_on"] or now()[:10])
            if item["currency"]=="CNY":
                raise ValueError("CNY open items already use the functional currency")
            if conn.execute("SELECT 1 FROM open_item_fx WHERE item_id=?",(item_id,)).fetchone():
                raise ValueError("open item already has a reviewed carrying value")
            if Decimal(item["settled_amount"])!=0:
                raise ValueError("partially settled legacy item needs a reviewed settlement-history adjustment")
            base=(Decimal(item["original_amount"])*rate).quantize(CENT,rounding=ROUND_HALF_UP)
            record_open_item_fx(conn,item_id,base,rate,rate_date,rate_source)
            return self.reply(200,{"open_item_id":item_id,"original_amount_cny":str(base),"exchange_rate":str(rate),"rate_date":rate_date,"rate_source":rate_source})

    def create_bank_account(self, data: dict) -> None:
        name=str(data.get("account_name","")).strip();currency=str(data.get("currency","CNY")).upper().strip()
        if not name or len(currency)!=3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("account_name and a three-letter currency code are required")
        with db() as conn:
            existing=conn.execute("SELECT * FROM bank_accounts WHERE account_name=?",(name,)).fetchone()
            if existing:
                if existing["currency"]!=currency:raise ValueError("bank account name is already registered with another currency")
                return self.reply(200,{"id":existing["id"],"account_name":name,"currency":currency,"status":"already_registered"})
            account_id=str(uuid4());conn.execute("INSERT INTO bank_accounts (id,account_name,currency,created_at) VALUES(?,?,?,?)",(account_id,name,currency,now()))
            return self.reply(201,{"id":account_id,"account_name":name,"currency":currency,"status":"registered"})

    def confirm_bank_account_coverage(self, data: dict) -> None:
        period=str(data.get("period",""));note=str(data.get("note","")).strip()
        try:
            if len(period)!=7 or period[4]!="-":raise ValueError
            datetime.strptime(period,"%Y-%m")
        except ValueError:raise ValueError("period must use YYYY-MM format")
        if not note:raise ValueError("record how the company bank account list was checked")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(period,)).fetchone():raise ValueError("bank account coverage cannot be changed after the period is closed")
            count=conn.execute("SELECT COUNT(*) FROM bank_accounts WHERE active=1").fetchone()[0]
            conn.execute("INSERT INTO bank_account_coverage (period,account_count,note,confirmed_at) VALUES(?,?,?,?) ON CONFLICT(period) DO UPDATE SET account_count=excluded.account_count,note=excluded.note,confirmed_at=excluded.confirmed_at",(period,count,note,now()))
            return self.reply(200,{"period":period,"registered_accounts":count,"status":"coverage_confirmed"})

    def save_bank_reconciliation(self, data: dict) -> None:
        account_id=str(data.get("bank_account_id",""));period=str(data.get("period",""));reference=str(data.get("statement_reference","")).strip();note=str(data.get("note","")).strip()
        try:
            if len(period)!=7 or period[4]!="-":raise ValueError
            datetime.strptime(period,"%Y-%m")
        except ValueError:raise ValueError("period must use YYYY-MM format")
        opening=amount(data.get("opening_balance"),"opening_balance");closing=amount(data.get("closing_balance"),"closing_balance")
        if not reference:raise ValueError("statement_reference is required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account=conn.execute("SELECT * FROM bank_accounts WHERE id=? AND active=1",(account_id,)).fetchone()
            if not account:raise ValueError("active bank account not found")
            if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(period,)).fetchone():raise ValueError("bank statement reconciliation cannot be changed after the period is closed")
            previous=conn.execute("SELECT * FROM bank_statement_reconciliations WHERE bank_account_id=? AND period=?",(account_id,period)).fetchone()
            reconciliation_id=previous["id"] if previous else str(uuid4())
            if previous:
                conn.execute("UPDATE bank_statement_reconciliations SET opening_balance=?,closing_balance=?,statement_reference=?,note=?,submitted_at=? WHERE id=?",(str(opening),str(closing),reference,note,now(),reconciliation_id))
            else:
                conn.execute("INSERT INTO bank_statement_reconciliations (id,bank_account_id,period,opening_balance,closing_balance,statement_reference,note,submitted_at) VALUES(?,?,?,?,?,?,?,?)",(reconciliation_id,account_id,period,str(opening),str(closing),reference,note,now()))
            conn.execute("INSERT INTO bank_reconciliation_history (id,reconciliation_id,opening_balance,closing_balance,statement_reference,note,changed_at) VALUES(?,?,?,?,?,?,?)",(str(uuid4()),reconciliation_id,str(opening),str(closing),reference,note,now()))
            rows=conn.execute("SELECT direction,amount FROM bank_transactions WHERE account_name=? AND currency=? AND transaction_on LIKE ?",(account["account_name"],account["currency"],f"{period}-%")).fetchall()
            inflow=sum((Decimal(row["amount"]) for row in rows if row["direction"]=="in"),Decimal(0));outflow=sum((Decimal(row["amount"]) for row in rows if row["direction"]=="out"),Decimal(0))
            difference=(opening+inflow-outflow-closing).quantize(CENT)
            return self.reply(200,{"bank_account_id":account_id,"period":period,"currency":account["currency"],"imported_inflow":str(inflow.quantize(CENT)),"imported_outflow":str(outflow.quantize(CENT)),"difference":str(difference),"reconciled":difference==0,"status":"reconciled" if difference==0 else "difference_requires_review"})

    def import_bank(self, data: dict) -> None:
        archive_id=str(data.get("archive_file_id") or "").strip()
        strict_identity=data.get("strict_identity") is True
        if strict_identity and not archive_id:
            raise ValueError("strict bank imports require an archived original document")
        rows = data.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError("rows must contain at least one bank transaction")
        created, duplicates, rejected = [], [], []
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for index, row in enumerate(rows, 1):
                supplied_key = str(row.get("source_key", "")).strip()
                key = supplied_key
                source_system = str(row.get("source_system", "")).strip()
                source_document_id = str(row.get("source_document_id", "")).strip()
                try:
                    if strict_identity:
                        key = bank_reference_identity(row)
                    elif not key:
                        raise ValueError("stable source_key is required")
                    if bool(source_system) != bool(source_document_id):
                        raise ValueError("source_system and source_document_id must be provided together")
                    value = amount(row["amount"], "amount")
                    direction = row["direction"]
                    day = str(row["transaction_on"])
                    currency = str(row.get("currency", "CNY")).upper()
                    if value <= 0 or direction not in ("in", "out") or len(currency) != 3 or not currency.isascii() or not currency.isalpha():
                        raise ValueError("amount must be positive and direction must be in/out")
                    datetime.strptime(day, "%Y-%m-%d")
                    if currency == "CNY":
                        rate = Decimal(1); rate_date = day; rate_source = "functional_currency"
                    else:
                        rate = exchange_rate(row.get("exchange_rate", "0"))
                        rate_date = str(row.get("rate_date", day))
                        rate_source = str(row.get("rate_source", "")).strip()
                        if not rate_source:
                            raise ValueError("foreign-currency bank rows require an exchange-rate source")
                        datetime.strptime(rate_date, "%Y-%m-%d")
                        if rate <= 0:
                            raise ValueError("foreign-currency bank rows require a positive exchange rate")
                    base_amount = (value * rate).quantize(CENT, rounding=ROUND_HALF_UP)
                    account_name = str(row.get("account_name", "未指定账户")).strip() or "未指定账户"
                    bank_reference = str(row.get("bank_reference", ""))
                    description = str(row.get("description", ""))
                    existing = conn.execute("SELECT * FROM bank_transactions WHERE source_key=?", (key,)).fetchone()
                    if not existing and source_system:
                        existing = conn.execute("SELECT * FROM bank_transactions WHERE source_system=? AND source_document_id=?", (source_system, source_document_id)).fetchone()
                    if strict_identity and not existing:
                        # Also compare existing legacy CSV rows: old keys were
                        # filename/line derived and must never be reposted.
                        matches = conn.execute(
                            "SELECT * FROM bank_transactions "
                            "WHERE account_name=? AND transaction_on=? AND bank_reference=?",
                            (account_name,day,bank_reference)
                        ).fetchall()
                        if len(matches)>1:
                            raise ValueError("multiple historical transactions share this bank reference; reconcile duplicates before importing")
                        if matches:
                            existing = matches[0]
                    payload = {"transaction_on": day, "account_name": account_name, "bank_reference": bank_reference, "description": description, "direction": direction, "amount": value, "currency": currency, "exchange_rate": rate, "rate_date": rate_date, "rate_source": rate_source, "base_amount_cny": base_amount, "source_system": source_system, "source_document_id": source_document_id}
                    if existing:
                        compare_payload = payload if not strict_identity else {
                            k:v for k,v in payload.items() if k not in ("source_system","source_document_id")
                        }
                        if any(str(existing[name] if existing[name] is not None else "") != str(val if val is not None else "") for name, val in compare_payload.items()):
                            raise ValueError("bank reference already exists with different business data; reconcile the original statement")
                        if archive_id:
                            original_archive.link_document(conn,archive_id,"finance.bank_transaction",existing["id"])
                        duplicates.append(supplied_key if strict_identity else key)
                        continue
                    bank_account = conn.execute("SELECT * FROM bank_accounts WHERE account_name=?", (account_name,)).fetchone()
                    if bank_account and bank_account["currency"] != currency:
                        raise ValueError("bank account is registered with another currency")
                    if not bank_account:
                        conn.execute("INSERT INTO bank_accounts (id,account_name,currency,created_at) VALUES(?,?,?,?)", (str(uuid4()), account_name, currency, now()))
                    ensure_open(conn, day)
                    row_id = str(uuid4())
                    conn.execute("INSERT INTO bank_transactions (id,source_key,transaction_on,account_name,bank_reference,description,direction,amount,currency,created_at,exchange_rate,rate_date,rate_source,base_amount_cny,source_system,source_document_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (row_id, key, day, account_name, bank_reference, description, direction, str(value), currency, now(), str(rate), rate_date, rate_source, str(base_amount), source_system, source_document_id))
                    if archive_id:
                        original_archive.link_document(conn,archive_id,"finance.bank_transaction",row_id)
                    created.append({"source_key": supplied_key if strict_identity else key,
                                    "canonical_source_key": key,
                                    "source_system": source_system or None,
                                    "source_document_id": source_document_id or None, "id": row_id})
                except (ValueError, KeyError, TypeError) as exc:
                    rejected.append({"row": index, "source_key": row.get("source_key"), "error": str(exc)})
        return self.reply(200, {"created": created, "duplicates": duplicates, "rejected": rejected})

    def classify_cash_flow(self, data: dict) -> None:
        bank_id=str(data.get("bank_transaction_id",""));category=str(data.get("category",""));note=str(data.get("note","")).strip()
        if category not in ("operating","investing","financing","transfer","unclassified"):
            raise ValueError("category must be operating, investing, financing, transfer or unclassified")
        if category in ("investing","financing","transfer") and not note:
            raise ValueError("a note is required for investing, financing and transfer classifications")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bank=conn.execute("SELECT * FROM bank_transactions WHERE id=?",(bank_id,)).fetchone()
            if not bank:raise ValueError("bank transaction not found")
            if conn.execute("SELECT 1 FROM period_locks WHERE period=?",(bank["transaction_on"][:7],)).fetchone():
                raise ValueError("cash-flow classification cannot be changed after the transaction period is closed")
            if bank["cash_flow_category"]==category and (bank["cash_flow_note"] or "")==note:
                return self.reply(200,{"bank_transaction_id":bank_id,"category":category,"status":"unchanged"})
            conn.execute("INSERT INTO bank_cash_flow_history (id,bank_transaction_id,old_category,new_category,note,changed_at) VALUES(?,?,?,?,?,?)",(str(uuid4()),bank_id,bank["cash_flow_category"],category,note,now()))
            conn.execute("UPDATE bank_transactions SET cash_flow_category=?,cash_flow_note=?,cash_flow_classified_at=? WHERE id=?",(category,note,now(),bank_id))
            return self.reply(200,{"bank_transaction_id":bank_id,"category":category,"status":"classified"})

    def allocate_bank(self, data: dict) -> None:
        bank_id = str(data["bank_transaction_id"])
        allocations = data.get("allocations")
        if not isinstance(allocations, list) or not allocations:
            raise ValueError("allocations must contain one or more targets")
        normalized=[]
        for item in allocations:
            normalized.append({"target_type":str(item.get("target_type","")),"target_id":str(item.get("target_id") or "") or None,"amount":str(amount(item.get("amount","0"),"allocation amount"))})
        payload={"bank_transaction_id":bank_id,"allocations":normalized}
        serialized=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        action_key=str(data.get("action_key","")).strip() or "bank_allocate:"+hashlib.sha256(serialized.encode()).hexdigest()
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior=conn.execute("SELECT action_type,payload,response FROM financial_action_requests WHERE action_key=?",(action_key,)).fetchone()
            if prior:
                if prior["action_type"]!="bank_allocate" or prior["payload"]!=serialized:
                    raise ValueError("action_key is already recorded for a different financial operation")
                return self.reply(200,{**json.loads(prior["response"]),"duplicate":True})
            bank = conn.execute("SELECT * FROM bank_transactions WHERE id=?", (bank_id,)).fetchone()
            if not bank:
                raise ValueError("bank transaction not found")
            if not bank["base_amount_cny"] or not bank["exchange_rate"]:
                raise ValueError("bank transaction has no reviewed CNY carrying value; add its exchange rate and source before allocation")
            already = sum((Decimal(row[0]) for row in conn.execute("SELECT amount FROM bank_allocations WHERE bank_transaction_id=?", (bank_id,))), Decimal(0))
            unvalued_allocations=conn.execute("SELECT COUNT(*) FROM bank_allocations a LEFT JOIN bank_allocation_fx f ON f.allocation_id=a.id WHERE a.bank_transaction_id=? AND f.allocation_id IS NULL",(bank_id,)).fetchone()[0]
            if unvalued_allocations:
                raise ValueError("legacy allocation lacks a reviewed CNY valuation; reconcile this bank transaction before adding more allocations")
            available = Decimal(bank["amount"]) - already
            already_base=sum((Decimal(row[0]) for row in conn.execute("SELECT f.bank_amount_cny FROM bank_allocations a JOIN bank_allocation_fx f ON f.allocation_id=a.id WHERE a.bank_transaction_id=?",(bank_id,))),Decimal(0))
            available_base=Decimal(bank["base_amount_cny"])-already_base
            if available_base<0:
                raise ValueError("bank CNY allocation exceeds the transaction's translated amount")
            if available <= 0:
                raise ValueError("bank transaction is already fully allocated")
            applied = []
            settlement_totals:dict[str,Decimal]={}
            settlement_base_totals:dict[str,Decimal]={}
            fx_gains=Decimal(0);fx_losses=Decimal(0)
            bank_rate=Decimal(bank["exchange_rate"])
            bank_rate_date=bank["rate_date"] or bank["transaction_on"]
            bank_rate_source=bank["rate_source"] or "legacy_unverified"
            for index,allocation in enumerate(allocations):
                part = amount(allocation["amount"], "allocation amount")
                if part <= 0 or part > available:
                    raise ValueError("allocation exceeds unallocated bank amount")
                remaining_after=available-part
                bank_part_base=(available_base if remaining_after==0 else (part*bank_rate).quantize(CENT,rounding=ROUND_HALF_UP))
                if bank_part_base>available_base:
                    raise ValueError("rounded CNY allocation exceeds remaining translated bank amount")
                target_type = str(allocation["target_type"])
                target_id = allocation.get("target_id")
                if target_type == "unclassified":
                    target_id = None
                    carrying_base=bank_part_base;fx_difference=Decimal(0)
                elif target_type in ("receivable", "payable"):
                    target = conn.execute("SELECT * FROM receivables_payables WHERE id=? AND direction=?", (target_id, target_type)).fetchone()
                    expected_bank_direction = "in" if target_type == "receivable" else "out"
                    if not target or bank["direction"] != expected_bank_direction or bank["currency"] != target["currency"]:
                        raise ValueError("bank direction, currency, or receivable/payable target does not match")
                    book=conn.execute("SELECT * FROM open_item_fx WHERE item_id=?",(target_id,)).fetchone()
                    if not book:
                        raise ValueError("open item has no reviewed CNY carrying value; foreign legacy balances cannot be allocated automatically")
                    settled_so_far = Decimal(target["settled_amount"]) + settlement_totals.get(target_id, Decimal(0))
                    outstanding = Decimal(target["original_amount"]) - settled_so_far
                    if part > outstanding:
                        raise ValueError("allocation exceeds outstanding receivable/payable")
                    settlement_totals[target_id] = settlement_totals.get(target_id, Decimal(0)) + part
                    settled_base=Decimal(book["settled_amount_cny"])+settlement_base_totals.get(target_id,Decimal(0))
                    outstanding_base=Decimal(book["original_amount_cny"])-settled_base
                    if outstanding<=0 or outstanding_base<0:
                        raise ValueError("open-item currency balance and CNY carrying balance are inconsistent")
                    carrying_base=(outstanding_base if part==outstanding else (Decimal(book["original_amount_cny"])*part/Decimal(target["original_amount"])).quantize(CENT,rounding=ROUND_HALF_UP))
                    if carrying_base>outstanding_base:
                        carrying_base=outstanding_base
                    settlement_base_totals[target_id]=settlement_base_totals.get(target_id,Decimal(0))+carrying_base
                    fx_difference=(bank_part_base-carrying_base).quantize(CENT)
                    if bank["direction"]=="in":
                        if fx_difference>=0: fx_gains+=fx_difference
                        else: fx_losses-=fx_difference
                    else:
                        if fx_difference>=0: fx_losses+=fx_difference
                        else: fx_gains-=fx_difference
                else:
                    raise ValueError("target_type must be receivable, payable, or unclassified")
                allocation_id=str(uuid4())
                conn.execute("INSERT INTO bank_allocations VALUES(?,?,?,?,?,?)", (allocation_id, bank_id, target_type, target_id, str(part), now()))
                conn.execute("INSERT INTO bank_allocation_fx VALUES(?,?,?,?)",(allocation_id,str(bank_part_base),str(carrying_base if carrying_base is not None else bank_part_base),str(fx_difference)))
                applied.append({"target_type": target_type, "target_id": target_id, "amount": str(part),"bank_amount_cny":str(bank_part_base),"carrying_amount_cny":str(carrying_base if carrying_base is not None else bank_part_base),"realized_fx_cny":str(fx_difference)})
                available -= part
                available_base-=bank_part_base
            day = bank["transaction_on"]
            ensure_open(conn, day)
            total_cny = sum((Decimal(x["bank_amount_cny"]) for x in applied), Decimal(0)).quantize(CENT)
            if bank["direction"] == "in":
                lines = [("bank", total_cny, Decimal(0))]
                for item in applied:
                    account = "accounts_receivable" if item["target_type"] == "receivable" else "unclassified_receipts"
                    lines.append((account, Decimal(0), Decimal(item["carrying_amount_cny"])))
            else:
                lines = [("bank", Decimal(0), total_cny)]
                for item in applied:
                    account = "accounts_payable" if item["target_type"] == "payable" else "unclassified_payments"
                    lines.append((account, Decimal(item["carrying_amount_cny"]), Decimal(0)))
            if fx_gains:
                lines.append(("realized_fx_gain",Decimal(0),fx_gains))
            if fx_losses:
                lines.append(("realized_fx_loss",fx_losses,Decimal(0)))
            journal(conn, "bank_allocation", bank_id, day, lines, currency="CNY")
            for target_id, settlement in settlement_totals.items():
                target = conn.execute("SELECT settled_amount FROM receivables_payables WHERE id=?", (target_id,)).fetchone()
                conn.execute("UPDATE receivables_payables SET settled_amount=? WHERE id=?", (str(Decimal(target["settled_amount"])+settlement), target_id))
                book=conn.execute("SELECT settled_amount_cny FROM open_item_fx WHERE item_id=?",(target_id,)).fetchone()
                conn.execute("UPDATE open_item_fx SET settled_amount_cny=? WHERE item_id=?",(str(Decimal(book["settled_amount_cny"])+settlement_base_totals[target_id]),target_id))
            response={"bank_transaction_id": bank_id, "allocated": str(sum((Decimal(x["amount"]) for x in applied),Decimal(0)).quantize(CENT)), "allocated_cny":str(total_cny),"unallocated": str(available), "unallocated_cny":str(available_base.quantize(CENT)), "allocations": applied, "realized_fx_gain_cny":str(fx_gains.quantize(CENT)),"realized_fx_loss_cny":str(fx_losses.quantize(CENT))}
            conn.execute("INSERT INTO financial_action_requests VALUES(?,?,?,?,?)",(action_key,"bank_allocate",serialized,json.dumps(response,ensure_ascii=False,sort_keys=True),now()))
            return self.reply(201,response)

    def create_invoice(self, data: dict) -> None:
        archive_id=str(data.get("archive_file_id") or "").strip()
        key = str(data.get("source_key", "")).strip()
        source_system = str(data.get("source_system", "")).strip()
        source_document_id = str(data.get("source_document_id", "")).strip()
        number, direction, party = str(data["invoice_number"]).strip(), str(data["direction"]), str(data["counterparty"]).strip()
        total, tax = amount(data["total_amount"], "total_amount"), amount(data.get("tax_amount", "0"), "tax_amount")
        day = str(data["issued_on"])
        currency = str(data.get("currency", "CNY")).upper()
        rate = exchange_rate(data.get("exchange_rate", "1"))
        rate_date = str(data.get("rate_date") or day)
        try:
            datetime.strptime(day, "%Y-%m-%d")
            datetime.strptime(rate_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("invoice and exchange-rate dates must use YYYY-MM-DD format")
        if bool(source_system) != bool(source_document_id):
            raise ValueError("source_system and source_document_id must be provided together")
        if not key or not number or not party or direction not in ("sales", "purchase") or total <= 0 or tax < 0 or tax > total or rate <= 0 or len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("valid source key, invoice number, direction, party, total and tax amounts are required")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM invoices WHERE source_key=?", (key,)).fetchone()
            if not existing and source_system:
                existing = conn.execute("SELECT * FROM invoices WHERE source_system=? AND source_document_id=?", (source_system, source_document_id)).fetchone()
            payload = {"invoice_number": number, "direction": direction, "counterparty": party, "issued_on": day, "total_amount": total, "tax_amount": tax, "currency": currency, "exchange_rate": rate, "rate_date": rate_date, "source_system": source_system, "source_document_id": source_document_id}
            if existing:
                if any(str(existing[name] if existing[name] is not None else "") != str(value if value is not None else "") for name, value in payload.items()):
                    raise ValueError("external invoice source is already recorded with a different payload; reconcile the source record")
                if archive_id:
                    original_archive.link_document(conn,archive_id,"finance.invoice",existing["id"])
                return self.reply(200, {"id": existing["id"], "invoice_number": existing["invoice_number"], "duplicate": True, "internal_status": "recorded", "tax_status": existing["tax_status"], "tax_credit_recognized": False})
            ensure_open(conn, day)
            invoice_id = str(uuid4())
            conn.execute("INSERT INTO invoices (id,invoice_number,direction,counterparty,issued_on,total_amount,tax_amount,currency,source_key,tax_status,tax_evidence_ref,exchange_rate,rate_date,created_at,source_system,source_document_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (invoice_id, number, direction, party, day, str(total), str(tax), currency, key, "pending", None, str(rate), rate_date, now(), source_system, source_document_id))
            if archive_id:
                original_archive.link_document(conn,archive_id,"finance.invoice",invoice_id)
            return self.reply(201, {"id": invoice_id, "invoice_number": number, "internal_status": "recorded", "tax_status": "pending", "tax_credit_recognized": False, "source_system": source_system or None, "source_document_id": source_document_id or None})

    def link_invoice(self, data: dict) -> None:
        invoice_id, target_type, target_id = str(data["invoice_id"]), str(data["target_type"]), str(data["target_id"])
        value = amount(data["amount"], "link amount")
        if value <= 0:
            raise ValueError("link amount must be positive")
        payload={"invoice_id":invoice_id,"target_type":target_type,"target_id":target_id,"amount":str(value)}
        serialized=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        action_key=str(data.get("action_key","")).strip() or "invoice_link:"+hashlib.sha256(serialized.encode()).hexdigest()
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior=conn.execute("SELECT action_type,payload,response FROM financial_action_requests WHERE action_key=?",(action_key,)).fetchone()
            if prior:
                if prior["action_type"]!="invoice_link" or prior["payload"]!=serialized:
                    raise ValueError("action_key is already recorded for a different financial operation")
                return self.reply(200,{**json.loads(prior["response"]),"duplicate":True})
            invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if not invoice:
                raise ValueError("invoice not found")
            if target_type == "receivable":
                target = conn.execute("SELECT * FROM receivables_payables WHERE id=? AND direction='receivable'", (target_id,)).fetchone()
            elif target_type == "payable":
                target = conn.execute("SELECT * FROM receivables_payables WHERE id=? AND direction='payable'", (target_id,)).fetchone()
            else:
                raise ValueError("target_type must be receivable or payable")
            expected_invoice_direction = "sales" if target_type == "receivable" else "purchase"
            if not target or invoice["direction"] != expected_invoice_direction or invoice["currency"] != target["currency"] or invoice["counterparty"] != target["counterparty"]:
                raise ValueError("invoice direction, currency, counterparty or linked business item does not match")
            existing_link=conn.execute("SELECT linked_amount FROM invoice_links WHERE invoice_id=? AND target_type=? AND target_id=?",(invoice_id,target_type,target_id)).fetchone()
            if existing_link:
                if Decimal(existing_link["linked_amount"])!=value:
                    raise ValueError("invoice is already linked to this business item with a different amount")
                response={"invoice_id":invoice_id,"linked_amount":str(value),"tax_status":invoice["tax_status"],"tax_credit_recognized":False,"accounting_effect":"none"}
                conn.execute("INSERT INTO financial_action_requests VALUES(?,?,?,?,?)",(action_key,"invoice_link",serialized,json.dumps(response,ensure_ascii=False,sort_keys=True),now()))
                return self.reply(200,{**response,"duplicate":True})
            ensure_open(conn,invoice["issued_on"])
            used = sum((Decimal(x[0]) for x in conn.execute("SELECT linked_amount FROM invoice_links WHERE invoice_id=?", (invoice_id,))), Decimal(0))
            target_used = sum((Decimal(x[0]) for x in conn.execute("SELECT linked_amount FROM invoice_links WHERE target_type=? AND target_id=?", (target_type,target_id))), Decimal(0))
            if used + value > Decimal(invoice["total_amount"]) or target_used + value > Decimal(target["original_amount"]):
                raise ValueError("links exceed invoice total or linked business item amount")
            conn.execute("INSERT INTO invoice_links VALUES(?,?,?,?,?,?)", (str(uuid4()), invoice_id, target_type, target_id, str(value), now()))
            response={"invoice_id": invoice_id, "linked_amount": str(value), "tax_status": invoice["tax_status"], "tax_credit_recognized": False, "accounting_effect": "none"}
            conn.execute("INSERT INTO financial_action_requests VALUES(?,?,?,?,?)",(action_key,"invoice_link",serialized,json.dumps(response,ensure_ascii=False,sort_keys=True),now()))
            return self.reply(201,response)

    def update_invoice_tax_status(self, data: dict) -> None:
        invoice_id, status = str(data["invoice_id"]), str(data["tax_status"])
        effective_on=str(data.get("effective_on") or date.today().isoformat())
        if len(effective_on)!=10:
            raise ValueError("tax status effective_on must use YYYY-MM-DD")
        try:
            datetime.strptime(effective_on,"%Y-%m-%d")
        except ValueError:
            raise ValueError("tax status effective_on must be a valid date")
        if status not in ("pending", "eligible", "certified_deductible", "non_deductible", "void"):
            raise ValueError("invalid tax status")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if not invoice:
                raise ValueError("invoice not found")
            if invoice["direction"] != "purchase" and status in ("eligible", "certified_deductible", "non_deductible"):
                raise ValueError("purchase VAT statuses apply only to input invoices")
            evidence = str(data.get("tax_evidence_ref", "")).strip() or None
            if status == "certified_deductible" and not evidence:
                raise ValueError("税务机关认证依据编号必填；发票登记或内部认领不等于税务确认")
            if effective_on < invoice["issued_on"]:
                raise ValueError("tax confirmation date cannot precede invoice issue date")
            changed=invoice["tax_status"] != status or invoice["tax_evidence_ref"] != evidence
            if not changed:
                return self.reply(200, {"invoice_id": invoice_id, "tax_status": status, "tax_evidence_ref": evidence, "tax_credit_recognized": False, "accounting_effect": "none", "duplicate": True})
            # The fiscal period of this later action controls locking, not the
            # original invoice issue month, which may be legitimately closed.
            ensure_open(conn,effective_on)
            conn.execute("UPDATE invoices SET tax_status=?,tax_evidence_ref=? WHERE id=?", (status, evidence, invoice_id))
            conn.execute("INSERT INTO invoice_tax_history (id,invoice_id,old_status,new_status,evidence_ref,changed_at,changed_by,effective_on,tax_period) VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid4()), invoice_id, invoice["tax_status"], status, evidence, now(), str(data.get("changed_by", "local-user")),effective_on,effective_on[:7]))
            return self.reply(200, {"invoice_id": invoice_id, "tax_status": status, "tax_evidence_ref": evidence, "effective_on":effective_on,"tax_period":effective_on[:7],"tax_credit_recognized": False, "accounting_effect": "none", "duplicate": False})

    def close_period(self, data: dict) -> None:
        period = str(data["period"])
        try:
            if len(period) != 7 or period[4] != "-":
                raise ValueError
            datetime.strptime(period, "%Y-%m")
        except ValueError:
            raise ValueError("period must use YYYY-MM format")
        actor = str(data.get("closed_by", "")).strip()
        if not actor.strip():
            raise ValueError("closed_by is required")
        reviewed=data.get("reviewed_checks")
        required={item["key"] for item in PERIOD_REVIEW_CHECKS}
        if not isinstance(reviewed,list) or set(reviewed)!=required:
            missing=sorted(required-set(reviewed if isinstance(reviewed,list) else []))
            raise ValueError("all close review checks must be explicitly acknowledged: "+", ".join(missing))
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reviewed_json=json.dumps(sorted(required))
            existing=conn.execute("SELECT period,closed_at,closed_by,reviewed_checks,journal_count FROM period_locks WHERE period=?",(period,)).fetchone()
            if existing:
                if existing["closed_by"]==actor and existing["reviewed_checks"]==reviewed_json:
                    return self.reply(200,{**dict(existing),"status":"closed","reviewed_checks":sorted(required),"duplicate":True})
                raise ValueError("accounting period is already closed by a different close record")
            readiness=period_close_readiness(conn,period)
            if readiness["blockers"]:
                return self.reply(422,{"error":"period has unresolved blocking items; resolve them before closing","readiness":readiness})
            closed_at=now()
            conn.execute("INSERT INTO period_locks (period,closed_at,closed_by,reviewed_checks,journal_count) VALUES(?,?,?,?,?)", (period, closed_at, actor, reviewed_json, readiness["journal_count"]))
            return self.reply(201, {"period": period,"closed_at":closed_at, "status": "closed", "closed_by": actor,"journal_count":readiness["journal_count"],"reviewed_checks":sorted(required),"duplicate":False})


def main() -> None:
    host = os.environ.get("WORKOS_HOST", "127.0.0.1")
    local_auth.require_loopback_host(host)
    if not local_auth.configured_hash_valid():
        raise RuntimeError("invalid WORKOS_AUTH_PASSWORD_HASH")
    if not local_auth.enabled():
        print("WARNING: local password not configured; this API must remain on loopback.")
    init_db()
    port = int(os.environ.get("WORKOS_PORT", "8103"))
    server = ThreadingHTTPServer((host, port), Api)
    database_label = f"SQLite: {DB_PATH}" if DB_BACKEND == "sqlite" else "PostgreSQL"
    print(f"Work OS API ready at http://{host}:{port} ({database_label})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()






