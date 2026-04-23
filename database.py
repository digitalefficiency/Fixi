"""
Database layer for the product cost tracking system.

Schema notes:
- ingredients: raw materials and packaging items (unit, cost, stock, reorder point)
- products: finished dishes sold to customers
- recipes: BOM - which ingredient (and how much) goes into each product
- service_extras: items auto-added per service mode (TA adds bag+box+cutlery, SIT adds napkin)
- orders: single sale event (product + mode + qty)
- consumption_log: full ledger of ingredient decrements (for forecasting / tenders)
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,              -- 'food' | 'packaging' | 'disposable'
    unit TEXT NOT NULL,                  -- 'gram','unit','ml'
    cost_per_unit REAL NOT NULL,         -- cost in shekels per unit
    stock REAL NOT NULL DEFAULT 0,       -- current quantity in stock
    reorder_threshold REAL NOT NULL DEFAULT 0,  -- below this => tender candidate
    tender_lead_time_days INTEGER NOT NULL DEFAULT 14,
    waste_pct REAL NOT NULL DEFAULT 0,   -- expected % loss (spoilage/handling/trim)
    target_cover_days INTEGER NOT NULL DEFAULT 3,  -- desired days of stock on hand
    order_schedule TEXT NOT NULL DEFAULT 'sun,tue,thu'  -- CSV of weekdays to order
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    price_sit REAL NOT NULL,
    price_ta REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS recipes (
    product_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    PRIMARY KEY (product_id, ingredient_id),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY (ingredient_id) REFERENCES ingredients(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS service_extras (
    service_mode TEXT NOT NULL,          -- 'TA' | 'SIT'
    ingredient_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    PRIMARY KEY (service_mode, ingredient_id),
    FOREIGN KEY (ingredient_id) REFERENCES ingredients(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    service_mode TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    total_cost REAL NOT NULL,
    total_revenue REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS consumption_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    source TEXT NOT NULL,                -- 'recipe' | 'service_extra'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (ingredient_id) REFERENCES ingredients(id)
);

-- Daily purchase orders to suppliers (distinct from customer 'orders' above).
CREATE TABLE IF NOT EXISTS daily_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending',   -- 'pending'|'received'|'cancelled'
    total_cost REAL NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS daily_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    daily_order_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    waste_adjusted_qty REAL NOT NULL,
    unit_cost REAL NOT NULL,
    line_cost REAL NOT NULL,
    reason TEXT,                               -- why suggested (replenish/tender)
    FOREIGN KEY (daily_order_id) REFERENCES daily_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (ingredient_id) REFERENCES ingredients(id)
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def reset_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
