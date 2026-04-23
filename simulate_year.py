"""
Simulates a full year of sales with realistic seasonal / weekly patterns,
then produces:

  1. A full BOM dump (what ingredients each product uses - SIT vs TA)
  2. Monthly sales & consumption report (orders + ingredient usage per month)
  3. Yearly totals that can drive automated procurement (reorder point, EOQ-like suggestion)

Assumptions used for the simulation (documented here so they can be tuned):
  - Restaurant open 30 days/month.
  - Baseline daily demand per product (different per product):
        המבורגר      : 40 orders/day
        סלט קיסר    : 18 orders/day
        קריספי ציקן  : 28 orders/day
  - TA vs SIT split: 45% TA / 55% SIT on weekdays, 30% TA / 70% SIT on weekends.
  - Weekly pattern: Fri/Sat busier (+35%), Sunday quieter (-15%).
  - Seasonality (monthly multiplier): summer (Jun-Aug) +20%, winter (Dec-Feb) -10%,
    holidays in Apr/Sep slight bumps.
  - Noise: +/- 10% random jitter per day.

All numbers are deterministic (fixed random seed) so the simulation is reproducible.
"""
from __future__ import annotations
import json
import random
from collections import defaultdict
from datetime import date, timedelta

from database import get_conn, reset_db
from seed import seed as seed_static
from services import product_tree


SEED = 42
SIM_DAYS = 365

BASE_DAILY = {
    "המבורגר":     40,
    "סלט קיסר":    18,
    "קריספי ציקן": 28,
}

# month (1..12) -> multiplier
MONTH_MULT = {
    1: 0.90, 2: 0.92, 3: 1.00, 4: 1.05, 5: 1.05,
    6: 1.20, 7: 1.22, 8: 1.20, 9: 1.05, 10: 1.00,
    11: 0.95, 12: 0.88,
}

# weekday Mon=0..Sun=6 (ISO: Mon=0)
WEEKDAY_MULT = {0: 0.95, 1: 0.95, 2: 1.00, 3: 1.05, 4: 1.35, 5: 1.35, 6: 0.85}


def _daily_volume(product: str, d: date, rng: random.Random) -> int:
    base = BASE_DAILY[product]
    v = base * MONTH_MULT[d.month] * WEEKDAY_MULT[d.weekday()]
    v *= rng.uniform(0.90, 1.10)
    return max(0, int(round(v)))


def _ta_ratio(d: date) -> float:
    return 0.30 if d.weekday() in (4, 5) else 0.45


def run_simulation(start: date | None = None):
    """Populates the DB with a year of synthetic orders starting 365 days before today."""
    if start is None:
        start = date.today() - timedelta(days=SIM_DAYS - 1)

    rng = random.Random(SEED)
    seed_static()   # resets DB and seeds static catalog
    # inflate stock so the simulation year doesn't run negative
    with get_conn() as conn:
        conn.execute("UPDATE ingredients SET stock = stock * 500")

        products = {r["name"]: r["id"] for r in conn.execute(
            "SELECT id,name FROM products").fetchall()}

    summary = {
        "days": SIM_DAYS,
        "start": start.isoformat(),
        "end": (start + timedelta(days=SIM_DAYS - 1)).isoformat(),
        "orders_by_product": defaultdict(int),
        "orders_by_mode":    defaultdict(int),
        "monthly": defaultdict(lambda: defaultdict(int)),  # (YYYY-MM) -> product -> units
        "monthly_mode": defaultdict(lambda: defaultdict(int)),
        "ingredient_usage": defaultdict(float),            # total usage in year
        "monthly_ingredient": defaultdict(lambda: defaultdict(float)),
        "total_revenue": 0.0,
        "total_cost": 0.0,
    }

    # precompute trees per (product, mode) and unit price
    trees = {}
    prices = {}
    with get_conn() as conn:
        for name, pid in products.items():
            for mode in ("SIT", "TA"):
                trees[(name, mode)] = product_tree(pid, mode)
            row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            prices[name] = {"SIT": row["price_sit"], "TA": row["price_ta"]}

    # walk days
    with get_conn() as conn:
        for i in range(SIM_DAYS):
            d = start + timedelta(days=i)
            ta_ratio = _ta_ratio(d)
            ym = d.strftime("%Y-%m")
            for name, pid in products.items():
                day_qty = _daily_volume(name, d, rng)
                if day_qty == 0:
                    continue
                ta_qty = int(round(day_qty * ta_ratio))
                sit_qty = day_qty - ta_qty
                for mode, qty in (("TA", ta_qty), ("SIT", sit_qty)):
                    if qty <= 0:
                        continue
                    tree = trees[(name, mode)]
                    unit_cost = sum(it["cost"] for it in tree)
                    unit_price = prices[name][mode]
                    total_cost = unit_cost * qty
                    total_rev = unit_price * qty
                    cur = conn.execute(
                        "INSERT INTO orders(product_id,service_mode,quantity,"
                        "total_cost,total_revenue,created_at) VALUES (?,?,?,?,?,?)",
                        (pid, mode, qty, total_cost, total_rev,
                         d.strftime("%Y-%m-%d 12:00:00")),
                    )
                    order_id = cur.lastrowid
                    for it in tree:
                        used = it["quantity"] * qty
                        conn.execute(
                            "INSERT INTO consumption_log(order_id,ingredient_id,"
                            "quantity,source,created_at) VALUES (?,?,?,?,?)",
                            (order_id, it["ingredient_id"], used, it["source"],
                             d.strftime("%Y-%m-%d 12:00:00")),
                        )
                        conn.execute(
                            "UPDATE ingredients SET stock = stock - ? WHERE id=?",
                            (used, it["ingredient_id"]),
                        )
                        summary["ingredient_usage"][it["name"]] += used
                        summary["monthly_ingredient"][ym][it["name"]] += used

                    summary["orders_by_product"][name] += qty
                    summary["orders_by_mode"][mode] += qty
                    summary["monthly"][ym][name] += qty
                    summary["monthly_mode"][ym][mode] += qty
                    summary["total_revenue"] += total_rev
                    summary["total_cost"] += total_cost
    return summary, trees, prices, products


def dump_bom(trees, prices):
    """Produces a readable BOM table per product for both SIT and TA."""
    lines = []
    lines.append("=" * 82)
    lines.append("BOM - מרכיבים לכל מנה (למנה אחת)")
    lines.append("=" * 82)
    for (product, mode), tree in sorted(trees.items()):
        unit_cost = sum(it["cost"] for it in tree)
        price = prices[product][mode]
        lines.append("")
        lines.append(f"▶ {product}  |  מצב שירות: {mode}  |  מחיר: ₪{price:.0f}  |  "
                     f"עלות: ₪{unit_cost:.2f}  |  רווח: ₪{price - unit_cost:.2f}")
        lines.append("-" * 82)
        lines.append(f"{'רכיב':<28} {'מקור':<16} {'כמות':>10} {'יחידה':>8} {'עלות':>10}")
        lines.append("-" * 82)
        for it in tree:
            lines.append(f"{it['name']:<28} {it['source']:<16} "
                         f"{it['quantity']:>10.2f} {it['unit']:>8} ₪{it['cost']:>8.2f}")
    lines.append("")
    return "\n".join(lines)


def monthly_report(summary):
    lines = []
    lines.append("=" * 82)
    lines.append("מחזור מכירות חודשי (מנות)")
    lines.append("=" * 82)
    months = sorted(summary["monthly"].keys())
    products = sorted(BASE_DAILY.keys())
    total_label = 'סה"כ'
    lines.append(f"{'חודש':<10} " + " ".join(f"{p:>12}" for p in products) +
                 f" {'TA':>8} {'SIT':>8} {total_label:>8}")
    for m in months:
        parts = [f"{m:<10}"]
        month_total = 0
        for p in products:
            q = summary["monthly"][m][p]
            month_total += q
            parts.append(f"{q:>12d}")
        ta = summary["monthly_mode"][m]["TA"]
        sit = summary["monthly_mode"][m]["SIT"]
        parts.append(f"{ta:>8d}")
        parts.append(f"{sit:>8d}")
        parts.append(f"{month_total:>8d}")
        lines.append(" ".join(parts))

    lines.append("")
    lines.append("=" * 82)
    lines.append("שימוש חודשי ברכיבים")
    lines.append("=" * 82)
    all_ing = sorted(summary["ingredient_usage"].keys())
    header = f"{'רכיב':<28}" + "".join(f"{m[-2:]:>8}" for m in months) + f"{'שנתי':>10}"
    lines.append(header)
    lines.append("-" * len(header))
    for ing in all_ing:
        row = [f"{ing:<28}"]
        for m in months:
            row.append(f"{summary['monthly_ingredient'][m][ing]:>8.0f}")
        row.append(f"{summary['ingredient_usage'][ing]:>10.0f}")
        lines.append("".join(row))
    return "\n".join(lines)


def reorder_table(summary):
    """Suggests automated ordering parameters based on observed yearly consumption.

    - monthly_avg: yearly usage / 12
    - safety_stock: 30% of monthly average
    - reorder_point: (monthly_avg / 30) * lead_time_days + safety_stock
    - order_qty (EOQ-ish simplification): 1 month supply (monthly_avg)
    """
    lines = []
    lines.append("=" * 82)
    lines.append("פרמטרים מוצעים להזמנה אוטומטית (על בסיס שנה של מכירות)")
    lines.append("=" * 82)
    header = (f"{'רכיב':<28} {'יחידה':>8} {'שנתי':>10} {'חודשי':>10} "
              f"{'ליום':>8} {'Lead':>6} {'Reorder':>10} {'כמות':>10} {'עלות':>12}")
    lines.append(header)
    lines.append("-" * len(header))
    with get_conn() as conn:
        rows = {r["name"]: r for r in conn.execute(
            "SELECT * FROM ingredients").fetchall()}
    for ing, yearly in sorted(summary["ingredient_usage"].items(),
                              key=lambda kv: -kv[1]):
        r = rows[ing]
        monthly = yearly / 12
        daily = yearly / 365
        lead = r["tender_lead_time_days"]
        safety = monthly * 0.30
        reorder_point = daily * lead + safety
        order_qty = monthly  # one month supply
        cost = order_qty * r["cost_per_unit"]
        lines.append(
            f"{ing:<28} {r['unit']:>8} {yearly:>10.0f} {monthly:>10.0f} "
            f"{daily:>8.1f} {lead:>6d} {reorder_point:>10.0f} "
            f"{order_qty:>10.0f} ₪{cost:>10.0f}"
        )
    return "\n".join(lines)


def export_json(summary, trees, prices, path="simulation_output.json"):
    payload = {
        "meta": {"days": summary["days"], "start": summary["start"],
                 "end": summary["end"], "seed": SEED},
        "totals": {
            "orders_by_product": dict(summary["orders_by_product"]),
            "orders_by_mode": dict(summary["orders_by_mode"]),
            "revenue": round(summary["total_revenue"], 2),
            "cost": round(summary["total_cost"], 2),
            "margin": round(summary["total_revenue"] - summary["total_cost"], 2),
        },
        "monthly_orders": {m: dict(v) for m, v in summary["monthly"].items()},
        "monthly_ingredient_usage": {m: dict(v) for m, v in
                                     summary["monthly_ingredient"].items()},
        "ingredient_yearly": dict(summary["ingredient_usage"]),
        "product_tree": {
            f"{prod}|{mode}": [
                {"ingredient": it["name"], "unit": it["unit"],
                 "qty_per_unit": it["quantity"], "cost": round(it["cost"], 3),
                 "source": it["source"]}
                for it in tree
            ]
            for (prod, mode), tree in trees.items()
        },
        "prices": prices,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def normalize_end_stock():
    """Sets each ingredient's ending stock to a realistic 'mid-cycle' level:
    enough for ~1.5x target_cover_days of consumption. This gives the
    daily-order view something to actually recommend today.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT i.*, COALESCE((SELECT SUM(c.quantity) FROM consumption_log c
                                     WHERE c.ingredient_id=i.id
                                       AND c.created_at >= datetime('now','-30 days')
                                    ), 0) AS used_30d
               FROM ingredients i"""
        ).fetchall()
        for r in rows:
            rate_per_day = (r["used_30d"] or 0) / 30.0
            cover = r["target_cover_days"] or 3
            lead = r["tender_lead_time_days"]
            # hold ~85% of (lead+cover) so we're mid-cycle but NOT under lead_time
            # (which would trigger tender). This gives realistic daily-order demo.
            target = rate_per_day * (lead + cover) * 0.85
            # never dip below lead_time * 1.1 (stay above tender trigger)
            floor = rate_per_day * lead * 1.1
            new_stock = max(target, floor, r["reorder_threshold"] * 1.2)
            conn.execute(
                "UPDATE ingredients SET stock=? WHERE id=?",
                (round(new_stock, 2), r["id"]),
            )


if __name__ == "__main__":
    summary, trees, prices, _ = run_simulation()
    normalize_end_stock()

    bom_report = dump_bom(trees, prices)
    month_report = monthly_report(summary)
    reorder = reorder_table(summary)

    print(bom_report)
    print()
    print(month_report)
    print()
    print(reorder)
    print()
    print(f"הכנסה שנתית: ₪{summary['total_revenue']:,.0f}")
    print(f"עלות חומר שנתית: ₪{summary['total_cost']:,.0f}")
    print(f"רווח גולמי: ₪{summary['total_revenue']-summary['total_cost']:,.0f}")

    path = export_json(summary, trees, prices)
    print(f"\nנשמר קובץ JSON מלא: {path}")
