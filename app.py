"""
Flask app: dashboard, product trees, order entry, inventory, tenders.
"""
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from datetime import date, datetime
from database import get_conn, init_db, DB_PATH
from services import (
    product_tree,
    order_summary,
    record_order,
    inventory_status,
    tender_candidates,
    parent_products_map,
    daily_order_for_date,
    weekly_order_plan,
    save_daily_order,
    receive_daily_order,
    list_daily_orders,
    supplier_order_from_sales,
)

app = Flask(__name__)
app.secret_key = "dev-only-not-a-secret"


@app.before_request
def _ensure_db():
    if not DB_PATH.exists():
        init_db()


@app.route("/")
def index():
    with get_conn() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
        recent = conn.execute(
            """SELECT o.*, p.name AS product_name FROM orders o
               JOIN products p ON p.id=o.product_id
               ORDER BY o.created_at DESC LIMIT 10"""
        ).fetchall()
        kpi = conn.execute(
            """SELECT COALESCE(SUM(total_revenue),0) AS rev,
                      COALESCE(SUM(total_cost),0)    AS cost,
                      COUNT(*) AS cnt
               FROM orders
               WHERE created_at >= datetime('now','-30 days')"""
        ).fetchone()
    tenders = tender_candidates()
    return render_template(
        "index.html",
        products=products,
        recent=recent,
        kpi=kpi,
        tender_count=len(tenders),
    )


@app.route("/products/<int:product_id>")
def product_detail(product_id):
    with get_conn() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?", (product_id,)
        ).fetchone()
    if not product:
        return "Product not found", 404
    tree_sit = product_tree(product_id, "SIT")
    tree_ta = product_tree(product_id, "TA")
    cost_sit = sum(i["cost"] for i in tree_sit)
    cost_ta = sum(i["cost"] for i in tree_ta)
    return render_template(
        "product.html",
        product=product,
        tree_sit=tree_sit,
        tree_ta=tree_ta,
        cost_sit=cost_sit,
        cost_ta=cost_ta,
        margin_sit=product["price_sit"] - cost_sit,
        margin_ta=product["price_ta"] - cost_ta,
    )


@app.route("/order", methods=["GET", "POST"])
def order():
    with get_conn() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()

    if request.method == "POST":
        product_id = int(request.form["product_id"])
        service_mode = request.form["service_mode"]
        quantity = int(request.form.get("quantity", 1))
        if quantity < 1:
            flash("כמות חייבת להיות לפחות 1", "error")
            return redirect(url_for("order"))
        summary = record_order(product_id, service_mode, quantity)
        flash(
            f"הוזמן: {summary['product']['name']} x{quantity} ({summary['service_mode']}) "
            f"- עלות ₪{summary['total_cost']:.2f}, הכנסה ₪{summary['total_revenue']:.2f}",
            "success",
        )
        return redirect(url_for("order"))

    return render_template("order.html", products=products)


@app.route("/api/preview")
def api_preview():
    """Live preview for the order form - returns the full product tree as JSON."""
    product_id = int(request.args["product_id"])
    service_mode = request.args.get("service_mode", "SIT")
    quantity = int(request.args.get("quantity", 1))
    return jsonify(order_summary(product_id, service_mode, quantity))


@app.route("/inventory")
def inventory():
    rows = inventory_status()
    parents = parent_products_map()
    for r in rows:
        r["parent_products"] = ", ".join(parents.get(r["id"], [])) or "-"
    return render_template("inventory.html", rows=rows)


@app.route("/daily-order", methods=["GET", "POST"])
def daily_order():
    day_str = request.args.get("date") or request.form.get("date") or date.today().isoformat()
    try:
        d = datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError:
        d = date.today()

    if request.method == "POST" and request.form.get("action") == "save":
        recs = daily_order_for_date(d)
        if recs:
            oid = save_daily_order(d.isoformat(), recs,
                                   notes=request.form.get("notes") or None)
            flash(f"נוצרה הזמנה יומית #{oid} - {len(recs)} פריטים", "success")
        else:
            flash("אין פריטים להזמין היום", "error")
        return redirect(url_for("daily_order", date=day_str))

    recs = daily_order_for_date(d)
    parents = parent_products_map()
    for r in recs:
        r["parent_products"] = ", ".join(parents.get(r["id"], [])) or "-"
    plan = weekly_order_plan(d)
    total = sum(r["estimated_cost"] for r in recs)
    return render_template("daily_order.html",
                           day=d, day_iso=d.isoformat(), recs=recs,
                           total=total, plan=plan,
                           history=list_daily_orders())


@app.route("/daily-order/<int:order_id>/receive", methods=["POST"])
def daily_order_receive(order_id):
    if receive_daily_order(order_id):
        flash(f"הזמנה #{order_id} סומנה כ'התקבלה' והמלאי עודכן", "success")
    else:
        flash("לא ניתן לעדכן - ההזמנה כבר קיבלה סטטוס אחר", "error")
    return redirect(url_for("daily_order"))


@app.route("/supplier-order")
def supplier_order_view():
    day_str = request.args.get("date") or date.today().isoformat()
    try:
        d = datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError:
        d = date.today()
    result = supplier_order_from_sales(d)
    return render_template("supplier_order.html", day=d, day_iso=d.isoformat(),
                           result=result)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        with get_conn() as conn:
            for key, value in request.form.items():
                if not key.startswith("ing_"):
                    continue
                _, iid, field = key.split("_", 2)
                iid = int(iid)
                if field == "cover":
                    conn.execute(
                        "UPDATE ingredients SET target_cover_days=? WHERE id=?",
                        (int(value or 0), iid))
                elif field == "schedule":
                    conn.execute(
                        "UPDATE ingredients SET order_schedule=? WHERE id=?",
                        (value.strip() or "sun", iid))
                elif field == "waste":
                    conn.execute(
                        "UPDATE ingredients SET waste_pct=? WHERE id=?",
                        (float(value or 0), iid))
        flash("הגדרות נשמרו", "success")
        return redirect(url_for("settings"))

    with get_conn() as conn:
        ingredients = conn.execute(
            "SELECT * FROM ingredients ORDER BY category, name"
        ).fetchall()
    return render_template("settings.html", ingredients=ingredients)


@app.route("/tenders")
def tenders():
    rows = tender_candidates()
    total = sum(r["estimated_cost"] for r in rows)
    return render_template("tenders.html", rows=rows, total=total)


if __name__ == "__main__":
    import os
    init_db()
    host = os.environ.get("FIXI_HOST", "0.0.0.0")
    port = int(os.environ.get("FIXI_PORT", "5000"))
    app.run(host=host, port=port, debug=False)
