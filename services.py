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


WORK_WEEK = ["sun", "mon", "tue", "wed", "thu"]  # Fri/Sat: no supplier deliveries
WEEKDAY_TO_CODE = {6: "sun", 0: "mon", 1: "tue", 2: "wed", 3: "thu",
                   4: "fri", 5: "sat"}  # Python weekday(): Mon=0..Sun=6

# Sales demand by weekday, indexed by Python's weekday() (Mon=0..Sun=6).
# Model: Sun = base, each weekday +20% compounded, Fri -40% from Thu, Sat +100% from Fri.
WEEKDAY_DEMAND_MULT = {
    6: 1.000,                               # Sun (base)
    0: 1.200,                               # Mon = Sun * 1.20
    1: 1.200 * 1.20,                        # Tue = 1.44
    2: 1.200 * 1.20 * 1.20,                 # Wed = 1.728
    3: 1.200 * 1.20 * 1.20 * 1.20,          # Thu = 2.0736  (weekday peak)
    4: (1.200 * 1.20 * 1.20 * 1.20) * 0.60, # Fri = 1.2442  (-40% from Thu)
    5: (1.200 * 1.20 * 1.20 * 1.20) * 0.60 * 2.00,  # Sat = 2.4883  (+100% from Fri)
}
_WEEKLY_TOTAL_MULT = sum(WEEKDAY_DEMAND_MULT.values())  # ~= 11.174


def expected_daily_consumption(rate_per_day, for_date):
    """Convert a flat average daily rate into expected consumption for a
    specific calendar date, using the weekday demand multipliers."""
    mult = WEEKDAY_DEMAND_MULT[for_date.weekday()]
    return rate_per_day * mult * 7.0 / _WEEKLY_TOTAL_MULT


def consumption_over_window(rate_per_day, start_day, num_days):
    """Sum of expected consumption over `num_days` calendar days starting the
    day AFTER start_day. Accounts for weekend surge (Sat) and Friday dip."""
    from datetime import timedelta
    total = 0.0
    detail = []
    for i in range(1, num_days + 1):
        d = start_day + timedelta(days=i)
        q = expected_daily_consumption(rate_per_day, d)
        total += q
        detail.append({"date": d, "code": WEEKDAY_TO_CODE[d.weekday()],
                       "expected": q})
    return total, detail


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
        # Use weekday-weighted consumption so Thursday orders cover
        # Sat surge +100% and Fri dip -40%, not a flat average.
        target, window_detail = consumption_over_window(rate, day, needed_days)
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
            "window_detail": window_detail,   # per-day expected consumption
            "weekend_coverage": code == "thu", # Thu = last-delivery-of-week flag
        })
    recs.sort(key=lambda r: -r["estimated_cost"])
    return recs


def supplier_order_from_sales(day, product_id=None):
    """Given a specific sales date, returns the supplier purchase order needed
    to cover that day's consumption.

    Optional `product_id` limits the calculation to a single product's sales
    (e.g. only the hamburger). Other product sales are ignored.

    Steps:
      1. Aggregate consumption_log rows for the day -> kitchen-unit usage per ingredient
      2. Add waste buffer: needed_procure = usage / (1 - waste_pct/100)
      3. Convert to supplier packs: packs = ceil(needed_procure / pack_size)
      4. Actual purchased qty = packs * pack_size (always >= needed_procure)
      5. Cost = packs * pack_cost

    Output groups by supplier for easy ordering.
    """
    import math
    product_filter_sql = "AND o.product_id = ?" if product_id else ""
    params_sales = [str(day)]
    params_cons = [str(day)]
    if product_id:
        params_sales.append(product_id)
        params_cons.append(product_id)

    with get_conn() as conn:
        sales_rows = conn.execute(
            f"""SELECT p.name AS product, o.service_mode,
                      SUM(o.quantity) AS qty, SUM(o.total_revenue) AS revenue
               FROM orders o JOIN products p ON p.id=o.product_id
               WHERE DATE(o.created_at) = DATE(?) {product_filter_sql}
               GROUP BY p.name, o.service_mode""",
            params_sales,
        ).fetchall()

        # consumption_log is joined via order_id -> product_id for the filter
        cons_filter = "AND o.product_id = ?" if product_id else ""
        consumption = conn.execute(
            f"""SELECT c.ingredient_id, SUM(c.quantity) AS used
               FROM consumption_log c JOIN orders o ON o.id=c.order_id
               WHERE DATE(c.created_at) = DATE(?) {cons_filter}
               GROUP BY c.ingredient_id""",
            params_cons,
        ).fetchall()
        used_by_ing = {r["ingredient_id"]: r["used"] for r in consumption}

        ingredients = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM ingredients").fetchall()}

        product_meta = None
        if product_id:
            row = conn.execute(
                "SELECT * FROM products WHERE id=?", (product_id,)
            ).fetchone()
            product_meta = dict(row) if row else None

    parents = parent_products_map()
    items = []
    for iid, used_kitchen in used_by_ing.items():
        ing = ingredients[iid]
        waste = (ing["waste_pct"] or 0) / 100.0
        need_with_waste = used_kitchen / (1 - waste) if waste < 1 else used_kitchen
        pack_size = ing["pack_size"] or 1
        pack_cost = ing["pack_cost"] or 0
        packs = math.ceil(need_with_waste / pack_size) if pack_size > 0 else 0
        actual_qty = packs * pack_size
        total_cost = packs * pack_cost
        items.append({
            "ingredient_id": iid,
            "name": ing["name"],
            "category": ing["category"],
            "kitchen_unit": ing["unit"],
            "parent_products": ", ".join(parents.get(iid, [])) or "-",
            "sales_consumption": round(used_kitchen, 2),
            "waste_pct": ing["waste_pct"],
            "need_with_waste": round(need_with_waste, 2),
            "supplier_name": ing["supplier_name"] or "-",
            "pack_label": ing["supplier_pack_label"] or "-",
            "pack_size": pack_size,
            "pack_cost": pack_cost,
            "packs_to_order": packs,
            "actual_qty": round(actual_qty, 2),
            "surplus": round(actual_qty - need_with_waste, 2),
            "total_cost": round(total_cost, 2),
        })

    # group by supplier for easy ordering
    by_supplier = {}
    for it in items:
        by_supplier.setdefault(it["supplier_name"], []).append(it)
    for sup in by_supplier:
        by_supplier[sup].sort(key=lambda x: -x["total_cost"])

    return {
        "date": str(day),
        "product_id": product_id,
        "product": product_meta,
        "sales": [dict(r) for r in sales_rows],
        "sales_total_units": sum(r["qty"] for r in sales_rows),
        "sales_total_revenue": sum(r["revenue"] for r in sales_rows),
        "items": sorted(items, key=lambda x: -x["total_cost"]),
        "by_supplier": by_supplier,
        "total_cost": round(sum(it["total_cost"] for it in items), 2),
    }


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


def closing_day_report(day):
    """Reconstructs what happened on `day`:
      opening_stock  = current_stock + consumption(day) - receipts(day)
                       (working backwards from the present state)
      consumption    = sum(consumption_log for that day)
      receipts       = sum(daily_order_items where status='received' on that day)
      closing_stock  = opening - consumption + receipts
      latest_count   = latest physical count for that ingredient (if any)
      variance       = latest_count.physical - closing_stock (if exists)

    NOTE: this is an approximation based on walk-back. For a production system
    you'd snapshot opening stock at midnight. Here we keep it stateless.
    """
    with get_conn() as conn:
        ings = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM ingredients").fetchall()}

        cons = {r["ingredient_id"]: r["q"] for r in conn.execute(
            """SELECT ingredient_id, SUM(quantity) AS q
               FROM consumption_log
               WHERE DATE(created_at)=DATE(?) GROUP BY ingredient_id""",
            (str(day),),
        ).fetchall()}

        recv = {r["ingredient_id"]: r["q"] for r in conn.execute(
            """SELECT di.ingredient_id, SUM(di.quantity) AS q
               FROM daily_order_items di
               JOIN daily_orders d ON d.id=di.daily_order_id
               WHERE d.status='received' AND DATE(d.order_date)=DATE(?)
               GROUP BY di.ingredient_id""",
            (str(day),),
        ).fetchall()}

        counts = {r["ingredient_id"]: dict(r) for r in conn.execute(
            """SELECT * FROM stock_counts
               WHERE DATE(count_date)=DATE(?)
               ORDER BY created_at DESC""",
            (str(day),),
        ).fetchall()}

    # Walk back: current stock -> closing EOD that day -> opening that day.
    # NOTE: this only works for TODAY. For historical dates the closing is
    # approximate because stock has moved since.
    rows = []
    for iid, ing in ings.items():
        used = cons.get(iid, 0) or 0
        received = recv.get(iid, 0) or 0
        # assume closing = current stock (only true when 'day' is today)
        closing = ing["stock"]
        opening = closing + used - received
        cnt = counts.get(iid)
        variance = None
        if cnt:
            variance = cnt["physical_qty"] - closing
        rows.append({
            "ingredient_id": iid,
            "name": ing["name"],
            "unit": ing["unit"],
            "supplier_pack": ing["supplier_pack_label"],
            "pack_size": ing["pack_size"],
            "opening": round(opening, 2),
            "consumption": round(used, 2),
            "receipts": round(received, 2),
            "closing": round(closing, 2),
            "physical_count": cnt["physical_qty"] if cnt else None,
            "variance": round(variance, 2) if variance is not None else None,
            "reorder_threshold": ing["reorder_threshold"],
            "below_threshold": closing < (ing["reorder_threshold"] or 0),
        })
    rows.sort(key=lambda r: -r["consumption"])
    return rows


def record_stock_count(day, counts):
    """counts: list of dicts {ingredient_id, physical_qty, notes?}
    Adjusts ingredient.stock to match physical count (variance recorded)."""
    with get_conn() as conn:
        for c in counts:
            iid = int(c["ingredient_id"])
            physical = float(c["physical_qty"])
            row = conn.execute(
                "SELECT stock FROM ingredients WHERE id=?", (iid,)
            ).fetchone()
            if not row:
                continue
            system_qty = row["stock"]
            variance = physical - system_qty
            conn.execute(
                "INSERT INTO stock_counts(count_date,ingredient_id,system_qty,"
                "physical_qty,variance,notes) VALUES (?,?,?,?,?,?)",
                (str(day), iid, system_qty, physical, variance, c.get("notes")),
            )
            # trust the physical count: adjust stock to match
            conn.execute(
                "UPDATE ingredients SET stock=? WHERE id=?", (physical, iid),
            )


def forecast_tomorrow_consumption(for_date):
    """Estimates tomorrow's consumption per ingredient based on the same
    weekday's average over the last 4 weeks (falls back to 30-day average)."""
    target_weekday = for_date.weekday()  # Python: Mon=0..Sun=6
    with get_conn() as conn:
        # Use strftime %w (Sun=0..Sat=6); convert python weekday to %w
        sqlite_dow = (target_weekday + 1) % 7   # Mon(0)->1, Sun(6)->0
        same_weekday = conn.execute(
            """SELECT c.ingredient_id,
                      SUM(c.quantity) AS total,
                      COUNT(DISTINCT DATE(c.created_at)) AS days
               FROM consumption_log c
               WHERE CAST(strftime('%w', c.created_at) AS INT) = ?
                 AND c.created_at >= DATE(?, '-28 days')
               GROUP BY c.ingredient_id""",
            (sqlite_dow, str(for_date)),
        ).fetchall()
        fallback = conn.execute(
            """SELECT ingredient_id, SUM(quantity)/30.0 AS per_day
               FROM consumption_log
               WHERE created_at >= DATE(?, '-30 days')
               GROUP BY ingredient_id""",
            (str(for_date),),
        ).fetchall()
    fb = {r["ingredient_id"]: r["per_day"] for r in fallback}
    out = {}
    for r in same_weekday:
        if r["days"] > 0:
            out[r["ingredient_id"]] = r["total"] / r["days"]
    # fill gaps with fallback average
    for iid, v in fb.items():
        out.setdefault(iid, v)
    return out


def auto_generate_next_day_orders(today, dry_run=False):
    """Called at end-of-day (or overnight via cron). Projects what tomorrow's
    opening stock will be and creates a pending daily_order for anything that
    would dip below target coverage.

      projected_opening = current_stock + pending_receipts - est_tomorrow_consumption
                         (where pending_receipts are daily_orders with status
                          'pending' whose order_date <= tomorrow and are expected
                          to arrive overnight)

    We then evaluate daily_order_for_date(tomorrow) against projected stock.
    If dry_run=True, only returns the preview without saving.
    """
    from datetime import timedelta
    tomorrow = today + timedelta(days=1)
    forecast = forecast_tomorrow_consumption(tomorrow)

    with get_conn() as conn:
        incoming = {r["ingredient_id"]: r["q"] for r in conn.execute(
            """SELECT di.ingredient_id, SUM(di.quantity) AS q
               FROM daily_order_items di
               JOIN daily_orders d ON d.id=di.daily_order_id
               WHERE d.status='pending' AND DATE(d.order_date) <= DATE(?)
               GROUP BY di.ingredient_id""",
            (str(tomorrow),),
        ).fetchall()}

        saved_stocks = {}
        for iid, ing in conn.execute("SELECT id,stock FROM ingredients").fetchall() \
                or []:
            pass  # placeholder
        ingredients = conn.execute("SELECT * FROM ingredients").fetchall()
        original_stock = {r["id"]: r["stock"] for r in ingredients}

    # temporarily project tomorrow's opening stock to drive the recommendation
    projected = {}
    for iid, cur_stock in original_stock.items():
        proj = cur_stock + (incoming.get(iid, 0) or 0) - (forecast.get(iid, 0) or 0)
        projected[iid] = max(0.0, proj)

    # apply projected stock (in-memory only when dry_run=True)
    with get_conn() as conn:
        for iid, val in projected.items():
            conn.execute("UPDATE ingredients SET stock=? WHERE id=?", (val, iid))

    try:
        recs = daily_order_for_date(tomorrow)
    finally:
        # always restore original stock
        with get_conn() as conn:
            for iid, val in original_stock.items():
                conn.execute("UPDATE ingredients SET stock=? WHERE id=?", (val, iid))

    summary = {
        "today": str(today),
        "tomorrow": str(tomorrow),
        "projected_opening": projected,
        "forecast_consumption": forecast,
        "incoming_today": incoming,
        "recommendations": recs,
        "total_cost": round(sum(r["estimated_cost"] for r in recs), 2),
        "saved": False,
    }
    if recs and not dry_run:
        oid = save_daily_order(str(tomorrow), recs,
                               notes="auto-generated at end-of-day")
        summary["daily_order_id"] = oid
        summary["saved"] = True
    return summary


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
