"""
Business logic: resolve product tree, compute per-order cost, record consumption,
flag ingredients for tender.

The "product tree" is dynamic: base recipe + service-mode extras + product-specific
TA packaging. This is the single place that knows how to expand an order into a
full list of ingredients.
"""
from database import get_conn


def product_tree(product_id: int, service_mode: str):
    """
    Returns the full expanded BOM for one unit of a product in a given service mode.

    Output: list of dicts with keys: ingredient_id, name, unit, quantity, cost,
    source in {'recipe','service_extra','ta_packaging'}.
    """
    mode = service_mode.upper()
    items = []
    with get_conn() as conn:
        base = conn.execute(
            """SELECT r.ingredient_id, i.name, i.unit, r.quantity, i.cost_per_unit
               FROM recipes r JOIN ingredients i ON i.id=r.ingredient_id
               WHERE r.product_id=?""",
            (product_id,),
        ).fetchall()
        for row in base:
            items.append({
                "ingredient_id": row["ingredient_id"],
                "name": row["name"],
                "unit": row["unit"],
                "quantity": row["quantity"],
                "cost": row["quantity"] * row["cost_per_unit"],
                "source": "recipe",
            })

        extras = conn.execute(
            """SELECT s.ingredient_id, i.name, i.unit, s.quantity, i.cost_per_unit
               FROM service_extras s JOIN ingredients i ON i.id=s.ingredient_id
               WHERE s.service_mode=?""",
            (mode,),
        ).fetchall()
        for row in extras:
            items.append({
                "ingredient_id": row["ingredient_id"],
                "name": row["name"],
                "unit": row["unit"],
                "quantity": row["quantity"],
                "cost": row["quantity"] * row["cost_per_unit"],
                "source": "service_extra",
            })

        if mode == "TA":
            pkg = conn.execute(
                """SELECT t.ingredient_id, i.name, i.unit, t.quantity, i.cost_per_unit
                   FROM ta_product_packaging t JOIN ingredients i ON i.id=t.ingredient_id
                   WHERE t.product_id=?""",
                (product_id,),
            ).fetchall()
            for row in pkg:
                items.append({
                    "ingredient_id": row["ingredient_id"],
                    "name": row["name"],
                    "unit": row["unit"],
                    "quantity": row["quantity"],
                    "cost": row["quantity"] * row["cost_per_unit"],
                    "source": "ta_packaging",
                })

    # collapse duplicates (e.g. napkin added by SIT extra and also an extra somewhere)
    merged = {}
    for it in items:
        key = it["ingredient_id"]
        if key in merged:
            merged[key]["quantity"] += it["quantity"]
            merged[key]["cost"] += it["cost"]
            if merged[key]["source"] != it["source"]:
                merged[key]["source"] = "mixed"
        else:
            merged[key] = dict(it)
    return list(merged.values())


def order_summary(product_id: int, service_mode: str, quantity: int):
    tree = product_tree(product_id, service_mode)
    unit_cost = sum(it["cost"] for it in tree)
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    price = p["price_ta"] if service_mode.upper() == "TA" else p["price_sit"]
    return {
        "product": dict(p),
        "service_mode": service_mode.upper(),
        "quantity": quantity,
        "tree": tree,
        "unit_cost": unit_cost,
        "unit_price": price,
        "unit_margin": price - unit_cost,
        "total_cost": unit_cost * quantity,
        "total_revenue": price * quantity,
        "total_margin": (price - unit_cost) * quantity,
    }


def record_order(product_id: int, service_mode: str, quantity: int):
    """Records an order and decrements stock in a single transaction."""
    summary = order_summary(product_id, service_mode, quantity)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO orders(product_id,service_mode,quantity,total_cost,total_revenue)"
            " VALUES (?,?,?,?,?)",
            (product_id, summary["service_mode"], quantity,
             summary["total_cost"], summary["total_revenue"]),
        )
        order_id = cur.lastrowid
        for it in summary["tree"]:
            used = it["quantity"] * quantity
            conn.execute(
                "INSERT INTO consumption_log(order_id,ingredient_id,quantity,source)"
                " VALUES (?,?,?,?)",
                (order_id, it["ingredient_id"], used, it["source"]),
            )
            conn.execute(
                "UPDATE ingredients SET stock = stock - ? WHERE id=?",
                (used, it["ingredient_id"]),
            )
    summary["order_id"] = order_id
    return summary


def inventory_status():
    """Returns stock + consumption rate + tender flag for every ingredient."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT i.*,
                      COALESCE((SELECT SUM(c.quantity) FROM consumption_log c
                                WHERE c.ingredient_id=i.id
                                  AND c.created_at >= datetime('now','-30 days')
                               ), 0) AS used_30d
               FROM ingredients i
               ORDER BY i.category, i.name"""
        ).fetchall()

    out = []
    for r in rows:
        rate_per_day = (r["used_30d"] or 0) / 30.0
        days_left = (r["stock"] / rate_per_day) if rate_per_day > 0 else None
        needs_tender = (
            r["stock"] <= r["reorder_threshold"]
            or (days_left is not None and days_left <= r["tender_lead_time_days"])
        )
        out.append({
            **dict(r),
            "used_30d": r["used_30d"] or 0,
            "rate_per_day": rate_per_day,
            "days_left": days_left,
            "needs_tender": needs_tender,
        })
    return out


def tender_candidates():
    """Items that should go to tender now, with suggested purchase quantity."""
    result = []
    for row in inventory_status():
        if not row["needs_tender"]:
            continue
        # suggest enough for 60 days of current consumption, minimum = reorder_threshold * 2
        horizon_qty = row["rate_per_day"] * 60 if row["rate_per_day"] > 0 else 0
        suggested = max(horizon_qty, row["reorder_threshold"] * 2) - row["stock"]
        if suggested < 0:
            suggested = 0
        row["suggested_purchase"] = round(suggested, 2)
        row["estimated_cost"] = round(suggested * row["cost_per_unit"], 2)
        result.append(row)
    # sort: most urgent first (lowest days_left, then largest shortfall)
    result.sort(key=lambda r: (r["days_left"] if r["days_left"] is not None else 1e9,
                               -r["suggested_purchase"]))
    return result
