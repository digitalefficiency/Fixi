"""
Flask app: dashboard, product trees, order entry, inventory, tenders.
"""
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from database import get_conn, init_db, DB_PATH
from services import (
    product_tree,
    order_summary,
    record_order,
    inventory_status,
    tender_candidates,
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
    return render_template("inventory.html", rows=rows)


@app.route("/tenders")
def tenders():
    rows = tender_candidates()
    total = sum(r["estimated_cost"] for r in rows)
    return render_template("tenders.html", rows=rows, total=total)


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
