"""One-time import of historical payment CSV into the payments table.

Usage:
    python import_payments.py payments.csv
"""
import csv
import re
import sys
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

import database as db


def _parse_amount(raw: str):
    """'RM 10,800.00' → 10800.0"""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", raw)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_month(raw: str) -> str:
    """Normalise the payment_month field.
    Handles plain text ('July 2026') and Excel serial numbers ('46174').
    """
    raw = raw.strip()
    if not raw:
        return ""
    # Excel serial date (numeric string)
    if re.fullmatch(r"\d{5}", raw):
        try:
            d = date(1899, 12, 30) + timedelta(days=int(raw))
            return d.strftime("%B %Y")
        except Exception:
            return raw
    return raw


def _clean(value: str) -> str:
    """Strip surrounding whitespace and internal newlines."""
    return " ".join(value.split())


def main(csv_path: str) -> None:
    client = db.get_client()
    rows_to_insert = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            member_id = _clean(row.get("Member ID", ""))
            if not member_id:
                continue

            amount = _parse_amount(row.get("Amount", ""))
            payment_month = _parse_month(row.get("Month", ""))

            rows_to_insert.append({
                "member_id": member_id,
                "name": _clean(row.get("Name", "")),
                "company_name": _clean(row.get("Company Name", "")),
                "amount": amount,
                "attachment_link": row.get("Attachment Link", "").strip(),
                "payment_month": payment_month,
                "cam": _clean(row.get("CAM", "")),
                "reference": _clean(row.get("References", "")),
                "account_number": _clean(row.get("Account Number", "")),
                "bank_name": _clean(row.get("Bank Name", "")),
                "account_name": _clean(row.get("Account Name", "")),
                "remark": _clean(row.get("Remark", "")),
                "source": "import",
            })

    if not rows_to_insert:
        print("No rows found.")
        return

    # Insert in batches of 100
    batch_size = 100
    inserted = 0
    for i in range(0, len(rows_to_insert), batch_size):
        batch = rows_to_insert[i : i + batch_size]
        client.from_("payments").insert(batch).execute()
        inserted += len(batch)
        print(f"Inserted {inserted}/{len(rows_to_insert)} rows...")

    print(f"Done. {inserted} payment records imported.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_payments.py <path_to_csv>")
        sys.exit(1)
    main(sys.argv[1])
