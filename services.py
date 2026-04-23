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
        # add waste allowance: buy more to cover expected loss
        waste = (row.get("waste_pct") or 0) / 100.0
        suggested_with_waste = suggested / (1 - waste) if waste < 1 else suggested
        row["suggested_purchase"] = round(suggested_with_waste, 2)
        row["estimated_cost"] = round(suggested_with_waste * row["cost_per_unit"], 2)
        result.append(row)
    # sort: most urgent first (lowest days_left, then largest shortfall)
    result.sort(key=lambda r: (r["days_left"] if r["days_left"] is not None else 1e9,
                               -r["suggested_purchase"]))
    return result


def parent_products_map():
    """Returns {ingredient_id: [product_name,...]} aggregating recipe, TA packaging,
    and service_extras (which apply to all products)."""
    with get_conn() as conn:
        all_products = [r["name"] for r in conn.execute(
            "SELECT name FROM products ORDER BY id").fetchall()]
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM ingredients").fetchall()]

        mapping = {iid: set() for iid in ids}

        for r in conn.execute(
            """SELECT r.ingredient_id, p.name FROM recipes r
               JOIN products p ON p.id=r.product_id"""):
            mapping[r["ingredient_id"]].add(r["name"])

        for r in conn.execute(
            """SELECT t.ingredient_id, p.name FROM ta_product_packaging t
               JOIN products p ON p.id=t.product_id"""):
            mapping[r["ingredient_id"]].add(f"{r['name']} (TA)")

        service_ings = {r["ingredient_id"] for r in conn.execute(
            "SELECT DISTINCT ingredient_id FROM service_extras").fetchall()}
        for iid in service_ings:
            # napkins/cutlery belong to all products
            for name in all_products:
                mapping[iid].add(name)

    return {iid: sorted(names) for iid, names in mapping.items()}


WORK_WEEK = ["sun", "mon", "tue", "wed", "thu"]  # Fri/Sat closed for ordering
WEEKDAY_TO_CODE = {6: "sun", 0: "mon", 1: "tue", 2: "wed", 3: "thu",
                   4: "fri", 5: "sat"}  # Python weekday(): Mon=0..Sun=6


def _weekday_code(d):
    return WEEKDAY_TO_CODE[d.weekday()]


def _days_until_next_order(current_code: str, schedule: list[str]) -> int:
    """How many days from today until the NEXT scheduled order day (exclusive of today).
    Used to size today's order: we need enough stock to last until the next delivery arrives.
    """
    order = WORK_WEEK  # iterate forward only over work days
    idx = order.index(current_code) if current_code in order else 0
    for offset in range(1, 8):
        code = order[(idx + offset) % len(order)]
        if code in schedule:
            # offset counts work days; map to calendar days
            return offset if (idx + offset) < len(order) else offset + 2  # skip weekend
    return 7  # fallback


def daily_order_for_date(day):
    """Build the purchase list for a given date.

    For each ingredient whose order_schedule includes today's weekday:
      needed_coverage_days = max(target_cover_days,
                                 lead_time + days_until_next_order)
      target_stock         = daily_rate * needed_coverage_days / (1 - waste%)
      suggested_qty        = max(0, target_stock - current_stock)

    Ingredients NOT scheduled today are skipped (unless they've crossed the
    tender threshold, in which case they show up as 'urgent').
    """
    code = _weekday_code(day)
    is_work_day = code in WORK_WEEK
    tenders = {t["id"]: t for t in tender_candidates()}

    recs = []
    for row in inventory_status():
        schedule = [s.strip() for s in (row["order_schedule"] or "").split(",") if s.strip()]
        scheduled_today = code in schedule
        urgent = row["id"] in tenders

        if not scheduled_today and not urgent:
            continue

        waste = (row.get("waste_pct") or 0) / 100.0
        rate = row["rate_per_day"] or 0
        lead = row["tender_lead_time_days"] or 0
        cover = row["target_cover_days"] or 0

        gap = _days_until_next_order(code, schedule) if scheduled_today else 0
        needed_days = max(cover, lead + gap)
        target = rate * needed_days
        if waste < 1:
            target = target / (1 - waste)
        shortfall = max(0.0, target - row["stock"])

        reason_parts = []
        if scheduled_today:
            reason_parts.append(f"מחזור {','.join(schedule)} - כיסוי {needed_days} ימים")
        if urgent:
            # emergency top-up to (lead_time + cover) - NOT the 60-day tender qty.
            # The full tender is a separate workflow in /tenders.
            emergency_target = rate * (lead + max(cover, 3))
            if waste < 1:
                emergency_target = emergency_target / (1 - waste)
            shortfall = max(shortfall, emergency_target - row["stock"])
            reason_parts.append("דחוף - מתחת לסף מכרז")

        if shortfall <= 0:
            continue

        recs.append({
            **row,
            "suggested_qty": round(shortfall, 2),
            "waste_factor": round(waste * 100, 1),
            "estimated_cost": round(shortfall * row["cost_per_unit"], 2),
            "reason": " | ".join(reason_parts),
            "day_code": code,
            "is_work_day": is_work_day,
        })
    recs.sort(key=lambda r: -r["estimated_cost"])
    return recs


def weekly_order_plan(base_date=None):
    """Returns a 7-day plan: day -> list of ingredient IDs scheduled for that day."""
    from datetime import date as _d, timedelta
    base = base_date or _d.today()
    # find previous Sunday
    sun_offset = (base.weekday() - 6) % 7
    sunday = base - timedelta(days=sun_offset)
    plan = []
    with get_conn() as conn:
        ingredients = conn.execute(
            "SELECT * FROM ingredients ORDER BY category, name"
        ).fetchall()
    for i in range(7):
        d = sunday + timedelta(days=i)
        code = _weekday_code(d)
        items = []
        for ing in ingredients:
            schedule = [s.strip() for s in (ing["order_schedule"] or "").split(",") if s.strip()]
            if code in schedule:
                items.append(dict(ing))
        plan.append({
            "date": d,
            "code": code,
            "is_work_day": code in WORK_WEEK,
            "items": items,
        })
    return plan


def save_daily_order(order_date, recommendations, notes=None):
    """Persists a daily purchase order + updates stock when status becomes 'received'.
    Here we save as 'pending' - receiving is a separate action."""
    total = sum(r["estimated_cost"] for r in recommendations)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO daily_orders(order_date,status,total_cost,notes) "
            "VALUES (?,?,?,?)",
            (order_date, "pending", total, notes),
        )
        doid = cur.lastrowid
        for r in recommendations:
            conn.execute(
                "INSERT INTO daily_order_items(daily_order_id,ingredient_id,"
                "quantity,waste_adjusted_qty,unit_cost,line_cost,reason) "
                "VALUES (?,?,?,?,?,?,?)",
                (doid, r["id"], r["suggested_qty"], r["suggested_qty"],
                 r["cost_per_unit"], r["estimated_cost"], r["reason"]),
            )
    return doid


def receive_daily_order(daily_order_id: int):
    """Marks a daily order as received and adds quantities into stock."""
    with get_conn() as conn:
        order = conn.execute(
            "SELECT * FROM daily_orders WHERE id=?", (daily_order_id,)
        ).fetchone()
        if not order or order["status"] != "pending":
            return False
        items = conn.execute(
            "SELECT * FROM daily_order_items WHERE daily_order_id=?",
            (daily_order_id,),
        ).fetchall()
        for it in items:
            conn.execute(
                "UPDATE ingredients SET stock = stock + ? WHERE id=?",
                (it["quantity"], it["ingredient_id"]),
            )
        conn.execute(
            "UPDATE daily_orders SET status='received' WHERE id=?",
            (daily_order_id,),
        )
    return True


def list_daily_orders():
    with get_conn() as conn:
        orders = conn.execute(
            "SELECT * FROM daily_orders ORDER BY order_date DESC, id DESC"
        ).fetchall()
        out = []
        for o in orders:
            items = conn.execute(
                """SELECT d.*, i.name, i.unit FROM daily_order_items d
                   JOIN ingredients i ON i.id=d.ingredient_id
                   WHERE d.daily_order_id=?""",
                (o["id"],),
            ).fetchall()
            out.append({
                **dict(o),
                "items": [dict(x) for x in items],
                "item_count": len(items),
            })
    return out
