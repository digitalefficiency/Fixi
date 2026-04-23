"""
Seeds initial ingredients, products, BOM recipes and service-mode extras.

All quantities and costs are illustrative - adjust to real procurement data later.
Units:
  - 'gram' for food by weight
  - 'ml'   for sauces/oils
  - 'unit' for countable items (lettuce head, packaging, etc.)
"""
from database import get_conn, reset_db


INGREDIENTS = [
    # name,                  category,      unit,   cost/unit, stock,   reorder_threshold, lead_days
    # --- Food ---
    ("לחמניית המבורגר",       "food",       "unit",  2.50,     200,     80,                 7),
    ("קציצת בקר 180 גרם",      "food",       "unit",  9.00,     150,     60,                 10),
    ("גבינה צהובה",           "food",       "gram",  0.08,     8000,    3000,               14),
    ("חסה",                  "food",       "gram",  0.02,     4000,    1500,               5),
    ("עגבנייה",              "food",       "gram",  0.015,    5000,    2000,               5),
    ("בצל",                  "food",       "gram",  0.010,    3000,    1000,               5),
    ("מלפפון חמוץ",           "food",       "gram",  0.025,    2000,    800,                14),
    ("רוטב המבורגר",          "food",       "ml",    0.03,     3000,    1000,               14),

    ("חזה עוף פרוס",          "food",       "gram",  0.055,    6000,    2500,               7),
    ("פירורי לחם",            "food",       "gram",  0.012,    4000,    1500,               14),
    ("שמן טיגון",             "food",       "ml",    0.018,    10000,   4000,               14),
    ("רוטב קיסר",             "food",       "ml",    0.04,     2000,    800,                14),
    ("פרמזן מגוררת",          "food",       "gram",  0.12,     1500,    500,                21),
    ("קרוטונים",              "food",       "gram",  0.03,     1500,    500,                21),

    # --- Packaging / disposables (TA) ---
    ("שקית נייר ניידים",       "packaging",  "unit",  0.70,     500,     200,                14),
    ("קופסת המבורגר קרטון",     "packaging",  "unit",  1.20,     400,     150,                14),
    ("קופסת סלט PET",          "packaging",  "unit",  1.50,     300,     120,                14),
    ("מזלג חד פעמי",           "disposable", "unit",  0.15,     800,     300,                14),
    ("סכין חד פעמי",           "disposable", "unit",  0.15,     800,     300,                14),
    ("מפית נייר",              "disposable", "unit",  0.05,     3000,    1000,               14),
    ("מכסה לקופסת סלט",         "packaging",  "unit",  0.40,     300,     120,                14),
]


PRODUCTS = [
    # name,           price_sit, price_ta
    ("המבורגר",          62,       68),
    ("סלט קיסר",         54,       58),
    ("קריספי ציקן",      56,       60),
]


# BOM: product -> list of (ingredient_name, quantity)
RECIPES = {
    "המבורגר": [
        ("לחמניית המבורגר",  1),
        ("קציצת בקר 180 גרם", 1),
        ("גבינה צהובה",     25),       # grams
        ("חסה",             20),
        ("עגבנייה",         40),
        ("בצל",             20),
        ("מלפפון חמוץ",     15),
        ("רוטב המבורגר",    30),        # ml
    ],
    "סלט קיסר": [
        ("חסה",            180),
        ("עגבנייה",         50),
        ("רוטב קיסר",       40),
        ("פרמזן מגוררת",    15),
        ("קרוטונים",        25),
        ("חזה עוף פרוס",   120),
    ],
    "קריספי ציקן": [
        ("לחמניית המבורגר", 1),
        ("חזה עוף פרוס",   150),
        ("פירורי לחם",      40),
        ("שמן טיגון",       60),
        ("חסה",             20),
        ("עגבנייה",         30),
        ("רוטב המבורגר",    25),
    ],
}


# Extras auto-added per service mode. Applied once per unit sold.
SERVICE_EXTRAS = {
    "SIT": [
        ("מפית נייר", 2),
    ],
    "TA": [
        ("שקית נייר ניידים", 1),
        ("מזלג חד פעמי",    1),
        ("סכין חד פעמי",    1),
        ("מפית נייר",       2),
        # product-specific TA packaging is added in _attach_packaging below
    ],
}


# TA-only packaging that depends on product type.
TA_PRODUCT_PACKAGING = {
    "המבורגר":      [("קופסת המבורגר קרטון", 1)],
    "קריספי ציקן":  [("קופסת המבורגר קרטון", 1)],
    "סלט קיסר":    [("קופסת סלט PET", 1), ("מכסה לקופסת סלט", 1)],
}


def _id_by_name(conn, table, name):
    row = conn.execute(f"SELECT id FROM {table} WHERE name=?", (name,)).fetchone()
    if not row:
        raise ValueError(f"{table}: '{name}' not found")
    return row["id"]


def seed():
    reset_db()
    with get_conn() as conn:
        for row in INGREDIENTS:
            conn.execute(
                "INSERT INTO ingredients(name,category,unit,cost_per_unit,stock,"
                "reorder_threshold,tender_lead_time_days) VALUES (?,?,?,?,?,?,?)",
                row,
            )
        for row in PRODUCTS:
            conn.execute(
                "INSERT INTO products(name,price_sit,price_ta) VALUES (?,?,?)", row
            )
        for product_name, items in RECIPES.items():
            pid = _id_by_name(conn, "products", product_name)
            for ing_name, qty in items:
                iid = _id_by_name(conn, "ingredients", ing_name)
                conn.execute(
                    "INSERT INTO recipes(product_id,ingredient_id,quantity) VALUES (?,?,?)",
                    (pid, iid, qty),
                )
        for mode, extras in SERVICE_EXTRAS.items():
            for ing_name, qty in extras:
                iid = _id_by_name(conn, "ingredients", ing_name)
                conn.execute(
                    "INSERT INTO service_extras(service_mode,ingredient_id,quantity) "
                    "VALUES (?,?,?)",
                    (mode, iid, qty),
                )
    _attach_packaging()


def _attach_packaging():
    """TA packaging depends on the product - store per-product in a small helper table."""
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ta_product_packaging (
                product_id INTEGER NOT NULL,
                ingredient_id INTEGER NOT NULL,
                quantity REAL NOT NULL,
                PRIMARY KEY (product_id, ingredient_id),
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
                FOREIGN KEY (ingredient_id) REFERENCES ingredients(id) ON DELETE CASCADE
            )"""
        )
        for product_name, items in TA_PRODUCT_PACKAGING.items():
            pid = _id_by_name(conn, "products", product_name)
            for ing_name, qty in items:
                iid = _id_by_name(conn, "ingredients", ing_name)
                conn.execute(
                    "INSERT OR REPLACE INTO ta_product_packaging"
                    "(product_id,ingredient_id,quantity) VALUES (?,?,?)",
                    (pid, iid, qty),
                )


if __name__ == "__main__":
    seed()
    print("Seeded database at", "data.db")
