"""
End-of-day automation script. Intended to run from cron each work night.

Suggested crontab (Israeli work week: run Sun-Thu evening at 23:00):
    0 23 * * 0,1,2,3,4  cd /path/to/Fixi && /usr/bin/python3 auto_order.py >> logs/auto.log 2>&1

Logic:
    1. Take today's date (or the date passed as arg)
    2. Call auto_generate_next_day_orders(today) which:
       - Forecasts tomorrow's consumption (same-weekday average over last 4 weeks)
       - Projects tomorrow's opening stock = current + pending_receipts - forecast
       - Runs daily_order_for_date(tomorrow) against the projected opening
       - If today is Thursday, the target coverage includes Fri+Sat demand
         (Saturday is +100% peak in our demand model)
       - Creates a pending daily_order
    3. Prints summary; output goes to log file via cron.

Running with --dry-run shows what WOULD be ordered without saving.
"""
from __future__ import annotations
import argparse
import sys
from datetime import date, datetime

from services import auto_generate_next_day_orders


def main():
    parser = argparse.ArgumentParser(
        description="End-of-day automation: generate tomorrow's purchase orders"
    )
    parser.add_argument("--date", help="Today's date (YYYY-MM-DD, default=today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be ordered without saving")
    args = parser.parse_args()

    today = (datetime.strptime(args.date, "%Y-%m-%d").date()
             if args.date else date.today())
    print(f"[auto_order] today={today}  dry_run={args.dry_run}")

    summary = auto_generate_next_day_orders(today, dry_run=args.dry_run)

    tomorrow = summary["tomorrow"]
    recs = summary["recommendations"]
    weekday_name = datetime.strptime(tomorrow, "%Y-%m-%d").date().strftime("%A")
    print(f"[auto_order] tomorrow={tomorrow} ({weekday_name})")
    print(f"[auto_order] {len(recs)} items to order, total ₪{summary['total_cost']:.2f}")

    if recs:
        print(f"{'רכיב':25s} {'מלאי עכשיו':>10s} {'תחזית מחר':>10s} "
              f"{'פתיחה צפויה':>12s} {'כמות מוצעת':>10s} {'עלות':>10s}")
        for r in recs:
            iid = r["id"]
            fc = summary["forecast_consumption"].get(iid, 0)
            proj = summary["projected_opening"].get(iid, 0)
            print(f"{r['name']:25s} {r['stock']:>10.1f} {fc:>10.1f} "
                  f"{proj:>12.1f} {r['suggested_qty']:>10.1f} ₪{r['estimated_cost']:>8.2f}")

    if summary["saved"]:
        print(f"[auto_order] SAVED as daily_order #{summary['daily_order_id']}")
    elif not recs:
        print("[auto_order] nothing to order; stock is sufficient")
    elif args.dry_run:
        print("[auto_order] DRY RUN - nothing was saved")

    return 0 if recs or args.dry_run else 0  # always success; no recs is fine


if __name__ == "__main__":
    sys.exit(main())
