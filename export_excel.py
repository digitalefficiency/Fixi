"""
Exports the whole dataset (products, BOM, orders, inventory, monthly
sales/consumption, automated-reorder parameters) into a single multi-sheet
XLSX file: ``fixi_export.xlsx``.

Run AFTER ``simulate_year.py`` has populated the DB with a year of data.

Sheets produced:
    1. סקירה               - headline KPIs
    2. מוצרים              - product master + prices
    3. רכיבים              - ingredient master + current stock
    4. עצי מוצר             - BOM per product x service mode (long format)
    5. מכירות חודשיות       - orders/revenue/cost by month x product
    6. שימוש רכיבים חודשי   - ingredient usage matrix (rows=ingredient, cols=month)
    7. הזמנות               - raw orders log
    8. פרמטרי הזמנה אוטומטית - reorder point + order qty for each ingredient
    9. מלאי ומכרזים          - full inventory status with tender flag
"""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from datetime import date
from database import get_conn
from services import (
    product_tree, inventory_status, tender_candidates,
    parent_products_map, daily_order_for_date, weekly_order_plan,
    supplier_order_from_sales, WEEKDAY_DEMAND_MULT, WEEKDAY_TO_CODE,
    consumption_over_window, closing_day_report,
)


OUT_PATH = Path(__file__).parent / "fixi_export.xlsx"

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
WARN_FILL = PatternFill("solid", fgColor="FEE2E2")
TOTAL_FILL = PatternFill("solid", fgColor="F3F4F6")
TOTAL_FONT = Font(bold=True)
CENTER = Alignment(horizontal="center")
RIGHT = Alignment(horizontal="right")


def _style_header(ws, row_idx, n_cols):
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=row_idx, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER


def _autosize(ws, max_width=40):
    for col_idx, col in enumerate(ws.columns, start=1):
        letter = get_column_letter(col_idx)
        longest = 0
        for cell in col:
            v = cell.value
            if v is None:
                continue
            n = len(str(v))
            if n > longest:
                longest = n
        ws.column_dimensions[letter].width = min(max(longest + 2, 10), max_width)


def sheet_overview(wb):
    ws = wb.create_sheet("סקירה")
    ws.sheet_view.rightToLeft = True

    with get_conn() as conn:
        tot = conn.execute(
            """SELECT COUNT(*) AS cnt,
                      COALESCE(SUM(total_revenue),0) AS rev,
                      COALESCE(SUM(total_cost),0) AS cost,
                      MIN(created_at) AS first_order,
                      MAX(created_at) AS last_order
               FROM orders"""
        ).fetchone()
        by_mode = {r["service_mode"]: dict(r) for r in conn.execute(
            """SELECT service_mode, SUM(quantity) AS qty,
                      SUM(total_revenue) AS rev, SUM(total_cost) AS cost
               FROM orders GROUP BY service_mode"""
        ).fetchall()}
        by_product = conn.execute(
            """SELECT p.name, SUM(o.quantity) AS qty, SUM(o.total_revenue) AS rev,
                      SUM(o.total_cost) AS cost
               FROM orders o JOIN products p ON p.id=o.product_id
               GROUP BY p.name ORDER BY qty DESC"""
        ).fetchall()

    ws["A1"] = "Fixi - דוח סיכום שנתי"
    ws["A1"].font = Font(bold=True, size=16)
    ws["A3"] = "תקופה:"
    ws["B3"] = f"{tot['first_order']}  עד  {tot['last_order']}"

    rows = [
        ["סה\"כ הזמנות", tot["cnt"]],
        ["הכנסה", round(tot["rev"], 2)],
        ["עלות חומר", round(tot["cost"], 2)],
        ["רווח גולמי", round(tot["rev"] - tot["cost"], 2)],
        ["אחוז רווח", f"{(1 - tot['cost']/tot['rev'])*100:.1f}%" if tot["rev"] else "-"],
    ]
    r = 5
    for label, value in rows:
        ws.cell(row=r, column=1, value=label).font = TOTAL_FONT
        ws.cell(row=r, column=2, value=value)
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="פיצול לפי מצב שירות").font = Font(bold=True, size=13)
    r += 1
    hdr = ["מצב שירות", "כמות", "הכנסה", "עלות", "רווח"]
    for c, h in enumerate(hdr, start=1):
        ws.cell(row=r, column=c, value=h)
    _style_header(ws, r, len(hdr))
    r += 1
    for mode in ("SIT", "TA"):
        if mode not in by_mode:
            continue
        d = by_mode[mode]
        ws.cell(row=r, column=1, value=mode)
        ws.cell(row=r, column=2, value=d["qty"])
        ws.cell(row=r, column=3, value=round(d["rev"] or 0, 2))
        ws.cell(row=r, column=4, value=round(d["cost"] or 0, 2))
        ws.cell(row=r, column=5, value=round((d["rev"] or 0) - (d["cost"] or 0), 2))
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="פיצול לפי מוצר").font = Font(bold=True, size=13)
    r += 1
    hdr = ["מוצר", "כמות", "הכנסה", "עלות", "רווח"]
    for c, h in enumerate(hdr, start=1):
        ws.cell(row=r, column=c, value=h)
    _style_header(ws, r, len(hdr))
    r += 1
    for row in by_product:
        ws.cell(row=r, column=1, value=row["name"])
        ws.cell(row=r, column=2, value=row["qty"])
        ws.cell(row=r, column=3, value=round(row["rev"], 2))
        ws.cell(row=r, column=4, value=round(row["cost"], 2))
        ws.cell(row=r, column=5, value=round(row["rev"] - row["cost"], 2))
        r += 1

    _autosize(ws)


def sheet_products(wb):
    ws = wb.create_sheet("מוצרים")
    ws.sheet_view.rightToLeft = True
    hdr = ["ID", "מוצר", "מחיר SIT", "מחיר TA", "עלות SIT", "עלות TA",
           "רווח SIT", "רווח TA"]
    ws.append(hdr)
    _style_header(ws, 1, len(hdr))
    with get_conn() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    for p in products:
        cost_sit = sum(i["cost"] for i in product_tree(p["id"], "SIT"))
        cost_ta = sum(i["cost"] for i in product_tree(p["id"], "TA"))
        ws.append([
            p["id"], p["name"], p["price_sit"], p["price_ta"],
            round(cost_sit, 2), round(cost_ta, 2),
            round(p["price_sit"] - cost_sit, 2),
            round(p["price_ta"] - cost_ta, 2),
        ])
    _autosize(ws)


def sheet_ingredients(wb):
    ws = wb.create_sheet("רכיבים")
    ws.sheet_view.rightToLeft = True
    hdr = ["ID", "רכיב", "אב מוצר", "קטגוריה", "יחידה", "מחיר ליחידה",
           "פחת %", "מלאי נוכחי", "סף הזמנה", "זמן אספקה (ימים)",
           "כיסוי ימים (יעד)", "מחזור הזמנה"]
    ws.append(hdr)
    _style_header(ws, 1, len(hdr))
    parents = parent_products_map()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ingredients ORDER BY category, name"
        ).fetchall()
    for r in rows:
        ws.append([
            r["id"], r["name"],
            ", ".join(parents.get(r["id"], [])) or "-",
            r["category"], r["unit"], r["cost_per_unit"],
            r["waste_pct"], round(r["stock"], 2),
            r["reorder_threshold"], r["tender_lead_time_days"],
            r["target_cover_days"], r["order_schedule"],
        ])
    _autosize(ws)


def sheet_bom(wb):
    ws = wb.create_sheet("עצי מוצר")
    ws.sheet_view.rightToLeft = True
    hdr = ["מוצר", "מצב שירות", "רכיב", "מקור", "כמות ליחידה",
           "יחידה", "מחיר/יח'", "עלות ליחידה"]
    ws.append(hdr)
    _style_header(ws, 1, len(hdr))

    with get_conn() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
        ing_price = {r["id"]: r["cost_per_unit"] for r in conn.execute(
            "SELECT id, cost_per_unit FROM ingredients").fetchall()}

    for p in products:
        for mode in ("SIT", "TA"):
            tree = product_tree(p["id"], mode)
            for it in tree:
                ws.append([
                    p["name"], mode, it["name"], it["source"],
                    round(it["quantity"], 3), it["unit"],
                    round(ing_price.get(it["ingredient_id"], 0), 3),
                    round(it["cost"], 3),
                ])
            total_cost = sum(i["cost"] for i in tree)
            price = p["price_ta"] if mode == "TA" else p["price_sit"]
            row = ws.max_row + 1
            ws.cell(row=row, column=1, value=f"{p['name']} · {mode} סה\"כ")
            ws.cell(row=row, column=7, value="עלות למנה")
            ws.cell(row=row, column=8, value=round(total_cost, 2))
            for c in range(1, 9):
                ws.cell(row=row, column=c).fill = TOTAL_FILL
                ws.cell(row=row, column=c).font = TOTAL_FONT
            row2 = ws.max_row + 1
            ws.cell(row=row2, column=1, value=f"{p['name']} · {mode} רווח")
            ws.cell(row=row2, column=7, value=f"מחיר ₪{price:.0f}, רווח")
            ws.cell(row=row2, column=8, value=round(price - total_cost, 2))
    ws.freeze_panes = "A2"
    _autosize(ws)


def sheet_monthly_sales(wb):
    ws = wb.create_sheet("מכירות חודשיות")
    ws.sheet_view.rightToLeft = True

    with get_conn() as conn:
        products = [r["name"] for r in conn.execute(
            "SELECT name FROM products ORDER BY id").fetchall()]
        rows = conn.execute(
            """SELECT strftime('%Y-%m', created_at) AS ym,
                      p.name AS product, o.service_mode,
                      SUM(o.quantity) AS qty,
                      SUM(o.total_revenue) AS rev,
                      SUM(o.total_cost) AS cost
               FROM orders o JOIN products p ON p.id=o.product_id
               GROUP BY ym, product, o.service_mode
               ORDER BY ym, product, o.service_mode"""
        ).fetchall()

    # pivot: one row per month, cols: each product * (SIT,TA), plus TA total, SIT total, qty, rev, cost
    by_month = defaultdict(lambda: defaultdict(float))
    months = []
    seen = set()
    for r in rows:
        ym = r["ym"]
        if ym not in seen:
            seen.add(ym)
            months.append(ym)
        key_qty = (r["product"], r["service_mode"])
        by_month[ym][key_qty] += r["qty"]
        by_month[ym][("_rev_", None)] += r["rev"]
        by_month[ym][("_cost_", None)] += r["cost"]
        by_month[ym][("_qty_", None)] += r["qty"]
        by_month[ym][("_ta_", None)] += r["qty"] if r["service_mode"] == "TA" else 0
        by_month[ym][("_sit_", None)] += r["qty"] if r["service_mode"] == "SIT" else 0

    hdr = ["חודש"]
    col_keys = []
    for prod in products:
        for mode in ("SIT", "TA"):
            hdr.append(f"{prod} · {mode}")
            col_keys.append((prod, mode))
    hdr += ["TA סה\"כ", "SIT סה\"כ", "כמות", "הכנסה", "עלות", "רווח"]
    ws.append(hdr)
    _style_header(ws, 1, len(hdr))

    for m in months:
        row = [m]
        for key in col_keys:
            row.append(int(by_month[m].get(key, 0)))
        ta = int(by_month[m][("_ta_", None)])
        sit = int(by_month[m][("_sit_", None)])
        qty = int(by_month[m][("_qty_", None)])
        rev = round(by_month[m][("_rev_", None)], 2)
        cost = round(by_month[m][("_cost_", None)], 2)
        row += [ta, sit, qty, rev, cost, round(rev - cost, 2)]
        ws.append(row)

    # totals row
    r = ws.max_row + 1
    totals = ["סה\"כ"]
    for key in col_keys:
        totals.append(int(sum(by_month[m].get(key, 0) for m in months)))
    totals += [
        int(sum(by_month[m][("_ta_", None)] for m in months)),
        int(sum(by_month[m][("_sit_", None)] for m in months)),
        int(sum(by_month[m][("_qty_", None)] for m in months)),
        round(sum(by_month[m][("_rev_", None)] for m in months), 2),
        round(sum(by_month[m][("_cost_", None)] for m in months), 2),
    ]
    totals.append(round(totals[-2] - totals[-1], 2))
    ws.append(totals)
    for c in range(1, len(totals) + 1):
        ws.cell(row=r, column=c).fill = TOTAL_FILL
        ws.cell(row=r, column=c).font = TOTAL_FONT

    ws.freeze_panes = "B2"
    _autosize(ws)


def sheet_monthly_usage(wb):
    ws = wb.create_sheet("שימוש רכיבים חודשי")
    ws.sheet_view.rightToLeft = True

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT strftime('%Y-%m', c.created_at) AS ym,
                      i.name AS ing, i.unit, i.cost_per_unit,
                      SUM(c.quantity) AS qty
               FROM consumption_log c JOIN ingredients i ON i.id=c.ingredient_id
               GROUP BY ym, i.id ORDER BY i.category, i.name"""
        ).fetchall()

    months = sorted({r["ym"] for r in rows})
    matrix = defaultdict(lambda: defaultdict(float))
    unit_map = {}
    price_map = {}
    for r in rows:
        matrix[r["ing"]][r["ym"]] += r["qty"]
        unit_map[r["ing"]] = r["unit"]
        price_map[r["ing"]] = r["cost_per_unit"]

    hdr = ["רכיב", "יחידה", "מחיר/יח'"] + months + ["סה\"כ שנתי", "עלות שנתית"]
    ws.append(hdr)
    _style_header(ws, 1, len(hdr))

    for ing in sorted(matrix.keys()):
        row = [ing, unit_map[ing], price_map[ing]]
        yearly = 0.0
        for m in months:
            v = matrix[ing].get(m, 0)
            yearly += v
            row.append(round(v, 2))
        row.append(round(yearly, 2))
        row.append(round(yearly * price_map[ing], 2))
        ws.append(row)

    ws.freeze_panes = "D2"
    _autosize(ws)


def sheet_orders(wb):
    ws = wb.create_sheet("הזמנות")
    ws.sheet_view.rightToLeft = True
    hdr = ["ID", "תאריך", "מוצר", "מצב", "כמות", "הכנסה", "עלות", "רווח"]
    ws.append(hdr)
    _style_header(ws, 1, len(hdr))
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT o.id, o.created_at, p.name AS product_name,
                      o.service_mode, o.quantity, o.total_revenue, o.total_cost
               FROM orders o JOIN products p ON p.id=o.product_id
               ORDER BY o.created_at, o.id"""
        ).fetchall()
    for r in rows:
        ws.append([
            r["id"], r["created_at"], r["product_name"], r["service_mode"],
            r["quantity"], round(r["total_revenue"], 2), round(r["total_cost"], 2),
            round(r["total_revenue"] - r["total_cost"], 2),
        ])
    ws.freeze_panes = "A2"
    _autosize(ws)


def sheet_reorder(wb):
    ws = wb.create_sheet("פרמטרי הזמנה אוטומטית")
    ws.sheet_view.rightToLeft = True
    hdr = ["רכיב", "אב מוצר", "יחידה", "פחת %", "צריכה שנתית",
           "ממוצע חודשי", "ממוצע יומי", "זמן אספקה (ימים)",
           "כיסוי יעד (ימים)", "מלאי ביטחון", "Reorder Point",
           "כמות הזמנה (ללא פחת)", "כמות מוצעת (כולל פחת)",
           "מחיר/יח'", "עלות הזמנה"]
    ws.append(hdr)
    _style_header(ws, 1, len(hdr))

    parents = parent_products_map()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT i.*, COALESCE((SELECT SUM(c.quantity) FROM consumption_log c
                                     WHERE c.ingredient_id=i.id), 0) AS yearly
               FROM ingredients i ORDER BY yearly DESC"""
        ).fetchall()

    for r in rows:
        yearly = r["yearly"] or 0
        monthly = yearly / 12
        daily = yearly / 365
        lead = r["tender_lead_time_days"]
        waste = (r["waste_pct"] or 0) / 100.0
        safety = monthly * 0.30
        reorder_pt = daily * lead + safety
        order_qty_raw = monthly
        order_qty_waste = order_qty_raw / (1 - waste) if waste < 1 else order_qty_raw
        ws.append([
            r["name"], ", ".join(parents.get(r["id"], [])) or "-",
            r["unit"], r["waste_pct"],
            round(yearly, 1), round(monthly, 1), round(daily, 2),
            lead, r["target_cover_days"], round(safety, 1), round(reorder_pt, 1),
            round(order_qty_raw, 1), round(order_qty_waste, 1),
            r["cost_per_unit"], round(order_qty_waste * r["cost_per_unit"], 2),
        ])
    ws.freeze_panes = "A2"
    _autosize(ws)


def sheet_inventory(wb):
    ws = wb.create_sheet("מלאי ומכרזים")
    ws.sheet_view.rightToLeft = True
    hdr = ["רכיב", "אב מוצר", "קטגוריה", "יחידה", "פחת %",
           "מלאי נוכחי", "סף הזמנה", "מחזור הזמנה",
           "שימוש 30 יום", "שימוש יומי", "ימים שנשארו",
           "דרוש מכרז?", "כמות מוצעת למכרז", "עלות מוערכת"]
    ws.append(hdr)
    _style_header(ws, 1, len(hdr))

    rows = inventory_status()
    tender_map = {t["id"]: t for t in tender_candidates()}
    parents = parent_products_map()

    for r in rows:
        t = tender_map.get(r["id"])
        days_left = r["days_left"]
        row_values = [
            r["name"], ", ".join(parents.get(r["id"], [])) or "-",
            r["category"], r["unit"], r["waste_pct"],
            round(r["stock"], 2), r["reorder_threshold"], r["order_schedule"],
            round(r["used_30d"], 2), round(r["rate_per_day"], 2),
            round(days_left, 1) if days_left is not None else "-",
            "כן" if r["needs_tender"] else "לא",
            round(t["suggested_purchase"], 1) if t else 0,
            round(t["estimated_cost"], 2) if t else 0,
        ]
        ws.append(row_values)
        if r["needs_tender"]:
            for c in range(1, len(hdr) + 1):
                ws.cell(row=ws.max_row, column=c).fill = WARN_FILL

    ws.freeze_panes = "A2"
    _autosize(ws)


def sheet_weekly_plan(wb):
    """Week-at-a-glance: which ingredients are scheduled for each work day + today's qty."""
    ws = wb.create_sheet("תכנית שבועית")
    ws.sheet_view.rightToLeft = True

    plan = weekly_order_plan()
    ws["A1"] = "תכנית הזמנות שבועית (ראשון-חמישי, 5 ימי עבודה)"
    ws["A1"].font = Font(bold=True, size=14)

    hdr = ["יום", "תאריך", "יום עבודה?", "מספר פריטים",
           "פריטים (רשימה)", "עלות משוערת"]
    ws.append([])
    ws.append(hdr)
    _style_header(ws, 3, len(hdr))

    heb_days = {"sun":"ראשון","mon":"שני","tue":"שלישי","wed":"רביעי",
                "thu":"חמישי","fri":"שישי","sat":"שבת"}
    parents = parent_products_map()

    for day in plan:
        items = day["items"]
        if day["is_work_day"]:
            recs = daily_order_for_date(day["date"])
            qty_map = {r["id"]: r for r in recs}
            cost = sum(r["estimated_cost"] for r in recs)
            item_names = ", ".join(f"{i['name']} ({qty_map[i['id']]['suggested_qty']:.0f} {i['unit']})"
                                   if i['id'] in qty_map else i['name']
                                   for i in items)
        else:
            cost = 0
            item_names = "יום סגור"

        row_idx = ws.max_row + 1
        ws.append([
            heb_days.get(day["code"], day["code"]),
            day["date"].isoformat(),
            "כן" if day["is_work_day"] else "לא",
            len(items) if day["is_work_day"] else 0,
            item_names,
            round(cost, 2),
        ])
        if not day["is_work_day"]:
            for c in range(1, len(hdr) + 1):
                ws.cell(row=row_idx, column=c).fill = TOTAL_FILL

    _autosize(ws, max_width=80)


def sheet_weekly_demand(wb):
    """Visualizes the weekly demand pattern and the Thursday-covers-weekend
    arithmetic: for each ingredient, show daily expected consumption Sun-Sat,
    and how much Thursday's order must cover for the Fri+Sat weekend."""
    ws = wb.create_sheet("דפוס ביקוש שבועי")
    ws.sheet_view.rightToLeft = True

    ws["A1"] = "דפוס ביקוש שבועי (Sun=בסיס, יומי +20%, שישי -40%, שבת +100%)"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "חמישי = יום אספקה אחרון → הזמנת חמישי חייבת לכסות שישי + שבת"
    ws["A2"].font = Font(size=11, color="B91C1C")

    ws.append([])

    # Show the multipliers themselves
    heb = {"sun":"ראשון","mon":"שני","tue":"שלישי","wed":"רביעי",
           "thu":"חמישי","fri":"שישי","sat":"שבת"}
    weekdays_order = [6, 0, 1, 2, 3, 4, 5]  # Sun..Sat in Python weekday()
    hdr = ["מכפיל ביקוש"] + [heb[WEEKDAY_TO_CODE[w]] for w in weekdays_order]
    ws.append(hdr)
    _style_header(ws, 4, len(hdr))
    ws.append(["× ביחס לראשון"] +
              [round(WEEKDAY_DEMAND_MULT[w], 3) for w in weekdays_order])

    ws.append([])
    # Expected daily consumption per ingredient
    ws.append(["צריכה יומית צפויה לפי רכיב (מבוססת על ממוצע היסטורי)"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True, size=12)

    hdr = (["רכיב", "יחידה", "צריכה יומית ממוצעת"]
           + [heb[WEEKDAY_TO_CODE[w]] for w in weekdays_order]
           + ["סה\"כ שבוע", "צריכת סופ\"ש (ו'+ש')", "חלק מהשבוע"])
    ws.append(hdr)
    header_row = ws.max_row
    _style_header(ws, header_row, len(hdr))

    from datetime import timedelta
    rows = inventory_status()
    rows.sort(key=lambda r: -r["rate_per_day"])
    for r in rows:
        rate = r["rate_per_day"] or 0
        # build expected per weekday (normalize so weekly avg = rate)
        total_mult = sum(WEEKDAY_DEMAND_MULT.values())
        per_day = {w: rate * WEEKDAY_DEMAND_MULT[w] * 7.0 / total_mult
                   for w in weekdays_order}
        weekly = sum(per_day.values())
        weekend = per_day[4] + per_day[5]   # fri + sat
        pct = (weekend / weekly * 100) if weekly else 0

        row_vals = [r["name"], r["unit"], round(rate, 2)]
        row_vals += [round(per_day[w], 1) for w in weekdays_order]
        row_vals += [round(weekly, 1), round(weekend, 1), f"{pct:.1f}%"]
        ws.append(row_vals)

    ws.append([])
    ws.append(["דוגמה: אם היום חמישי וההזמנה הבאה רק ביום ראשון,"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append(["ההזמנה חייבת לכסות: שישי + שבת + ראשון_בוקר (עד האספקה)"])
    ws.append([])

    # Thursday-coverage table
    hdr2 = ["רכיב", "יחידה", "שישי צפוי", "שבת צפוי", "ראשון צפוי",
            "סה\"כ 3 ימים", "+פחת %", "נדרש כולל פחת"]
    ws.append(hdr2)
    hdr2_row = ws.max_row
    _style_header(ws, hdr2_row, len(hdr2))

    # next weekend starting from last Thursday (for display)
    today = date.today()
    # find most recent Thursday on/before today
    days_back = (today.weekday() - 3) % 7
    thu = today - timedelta(days=days_back)
    for r in rows:
        rate = r["rate_per_day"] or 0
        waste = (r.get("waste_pct") or 0) / 100.0
        _, detail = consumption_over_window(rate, thu, 3)  # Fri, Sat, Sun
        total = sum(x["expected"] for x in detail)
        with_waste = total / (1 - waste) if waste < 1 else total
        ws.append([
            r["name"], r["unit"],
            round(detail[0]["expected"], 1),
            round(detail[1]["expected"], 1),
            round(detail[2]["expected"], 1),
            round(total, 1),
            r["waste_pct"],
            round(with_waste, 1),
        ])

    _autosize(ws, max_width=30)


def sheet_closing_report(wb):
    """Today's closing: opening/consumption/receipts/closing/below-threshold."""
    ws = wb.create_sheet("סגירת יום")
    ws.sheet_view.rightToLeft = True

    ws["A1"] = f"דוח סגירת יום - {date.today().isoformat()}"
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])

    hdr = ["רכיב", "יחידה", "פתיחה", "צריכה", "קליטות",
           "סגירה (מערכת)", "ספירה פיזית", "פער", "סף הזמנה", "מתחת לסף?"]
    ws.append(hdr)
    _style_header(ws, 3, len(hdr))

    for r in closing_day_report(date.today()):
        row = [
            r["name"], r["unit"],
            r["opening"], r["consumption"], r["receipts"], r["closing"],
            r["physical_count"] if r["physical_count"] is not None else "",
            r["variance"] if r["variance"] is not None else "",
            r["reorder_threshold"],
            "כן" if r["below_threshold"] else "לא",
        ]
        ws.append(row)
        if r["below_threshold"]:
            for c in range(1, len(hdr) + 1):
                ws.cell(row=ws.max_row, column=c).fill = WARN_FILL

    ws.freeze_panes = "A4"
    _autosize(ws)


def sheet_supplier_order(wb, sample_date=None, product_id=None, sheet_name=None):
    """For a given date's sales, show the supplier purchase order grouped by supplier.

    sample_date defaults to the most recent day with sales data.
    If product_id is given, filter to a single product's consumption only.
    """
    if sheet_name is None:
        sheet_name = "הזמנה לספק (לפי מכירות)"
    ws = wb.create_sheet(sheet_name)
    ws.sheet_view.rightToLeft = True

    if sample_date is None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT DATE(MAX(created_at)) AS d FROM orders"
            ).fetchone()
        sample_date = row["d"] if row and row["d"] else date.today().isoformat()

    from datetime import datetime as _dt
    d = _dt.strptime(sample_date, "%Y-%m-%d").date() if isinstance(sample_date, str) \
        else sample_date
    result = supplier_order_from_sales(d, product_id=product_id)

    title = f"הזמנה לספקים לפי מכירות יום {d.isoformat()}"
    if result.get("product"):
        title += f" - {result['product']['name']} בלבד"
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"מכירות: {result['sales_total_units']} מנות · "
                f"הכנסה ₪{result['sales_total_revenue']:.2f} · "
                f"עלות הזמנה לספקים: ₪{result['total_cost']:.2f}")
    ws["A2"].font = Font(size=11)

    ws.append([])
    hdr = ["ספק", "פריט", "אריזה", "צריכה במטבח", "יחידת מטבח",
           "פחת %", "נדרש (כולל פחת)", "כמות בחבילה",
           "מס' חבילות להזמין", "כמות בפועל שמגיעה",
           "עודף (מעבר לנדרש)", "מחיר חבילה", "סה\"כ לפריט"]
    ws.append(hdr)
    _style_header(ws, 4, len(hdr))

    for supplier, items in result["by_supplier"].items():
        sup_total = sum(it["total_cost"] for it in items)
        for it in items:
            ws.append([
                supplier, it["name"], it["pack_label"],
                it["sales_consumption"], it["kitchen_unit"],
                it["waste_pct"], it["need_with_waste"],
                it["pack_size"], it["packs_to_order"],
                it["actual_qty"], it["surplus"],
                it["pack_cost"], it["total_cost"],
            ])
        # subtotal row per supplier
        row_idx = ws.max_row + 1
        ws.cell(row=row_idx, column=1, value=f"סה\"כ {supplier}").font = TOTAL_FONT
        ws.cell(row=row_idx, column=13, value=round(sup_total, 2)).font = TOTAL_FONT
        for c in range(1, len(hdr) + 1):
            ws.cell(row=row_idx, column=c).fill = TOTAL_FILL

    # grand total
    row_idx = ws.max_row + 2
    ws.cell(row=row_idx, column=1, value="סה\"כ כללי").font = Font(bold=True, size=12)
    ws.cell(row=row_idx, column=13, value=result["total_cost"]).font = Font(bold=True, size=12)

    ws.freeze_panes = "A5"
    _autosize(ws, max_width=40)


def sheet_daily_order(wb):
    """Today's recommended purchase order (detailed)."""
    ws = wb.create_sheet("הזמנה יומית")
    ws.sheet_view.rightToLeft = True

    today = date.today()
    recs = daily_order_for_date(today)
    parents = parent_products_map()

    ws["A1"] = f"הזמנה יומית מומלצת - {today.isoformat()}"
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])

    hdr = ["רכיב", "אב מוצר", "קטגוריה", "יחידה", "מלאי נוכחי",
           "שימוש יומי", "פחת %", "מחזור הזמנה", "כיסוי יעד",
           "כמות מוצעת", "מחיר/יח'", "עלות", "סיבה"]
    ws.append(hdr)
    _style_header(ws, 3, len(hdr))

    total = 0.0
    for r in recs:
        total += r["estimated_cost"]
        ws.append([
            r["name"], ", ".join(parents.get(r["id"], [])) or "-",
            r["category"], r["unit"], round(r["stock"], 2),
            round(r["rate_per_day"], 2), r["waste_pct"],
            r["order_schedule"], r["target_cover_days"],
            r["suggested_qty"], r["cost_per_unit"],
            r["estimated_cost"], r["reason"],
        ])

    row = ws.max_row + 1
    ws.cell(row=row, column=1, value="סה\"כ").font = TOTAL_FONT
    ws.cell(row=row, column=12, value=round(total, 2)).font = TOTAL_FONT
    for c in range(1, len(hdr) + 1):
        ws.cell(row=row, column=c).fill = TOTAL_FILL

    ws.freeze_panes = "A4"
    _autosize(ws)


def build():
    wb = Workbook()
    wb.remove(wb.active)  # drop default empty sheet

    sheet_overview(wb)
    sheet_products(wb)
    sheet_ingredients(wb)
    sheet_bom(wb)
    sheet_monthly_sales(wb)
    sheet_monthly_usage(wb)
    sheet_orders(wb)
    sheet_reorder(wb)
    sheet_inventory(wb)
    sheet_weekly_plan(wb)
    sheet_weekly_demand(wb)
    sheet_closing_report(wb)
    sheet_daily_order(wb)
    sheet_supplier_order(wb)

    # burger-only sheet (product_id=1 is the hamburger in our seed)
    with get_conn() as conn:
        burger = conn.execute(
            "SELECT id FROM products WHERE name='המבורגר'"
        ).fetchone()
    if burger:
        sheet_supplier_order(wb, product_id=burger["id"],
                             sheet_name="הזמנה לספק - המבורגר בלבד")

    wb.save(OUT_PATH)
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    size_kb = path.stat().st_size / 1024
    print(f"נוצר קובץ אקסל: {path}  ({size_kb:.1f} KB)")
    print("גיליונות:")
    for name in [
        "סקירה", "מוצרים", "רכיבים", "עצי מוצר", "מכירות חודשיות",
        "שימוש רכיבים חודשי", "הזמנות", "פרמטרי הזמנה אוטומטית",
        "מלאי ומכרזים", "תכנית שבועית", "דפוס ביקוש שבועי",
        "סגירת יום", "הזמנה יומית",
        "הזמנה לספק (לפי מכירות)", "הזמנה לספק - המבורגר בלבד",
    ]:
        print(f"  · {name}")
