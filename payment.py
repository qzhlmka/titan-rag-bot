import base64
import json
import logging
import os

import httpx
from openai import OpenAI

import database as db

_openai_client = None
logger = logging.getLogger(__name__)


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


EXTRACT_PROMPT = """You are processing payment receipts for RichWorks / Titan business coaching program.

IMPORTANT CONTEXT:
- RichWorks, Titan, Richworks International, or any variation of these names is ALWAYS the RECIPIENT (payee) — the company being paid.
- The CLIENT is the SENDER/PAYER — the person or business making the payment.
- The reference field on a receipt often contains the payee name (RichWorks/Titan) — do NOT use this as the client's company name.

Extract the following and return a JSON object with exactly these fields:
- clientName: full name of the SENDER/PAYER (not RichWorks). Look for "From", "Sender", "Pengirim", or the account holder name.
- companyName: the SENDER's business/company name if visible. Never put RichWorks or Titan here. Return "" if not visible.
- installmentAmount: payment amount as a plain number only (no RM, no commas), e.g. 5000 or 2500.50
- installmentMonth: month and year the payment covers, e.g. "June 2026". Infer from the receipt date if not stated.
- reference: the transaction/reference ID number from the receipt (e.g. "TXN123456", "REF: 98765"). Return "" if not found.
- recipientAccount: the RECIPIENT's bank account number (the account that received the money — belonging to RichWorks/Titan). Look for "To", "Penerima", "Recipient", or the destination account. Return digits only, no spaces or dashes. Return "" if not found.

Return ONLY the JSON object. If a field is not visible, return ""."""


def lookup_account(account_number: str) -> dict:
    """Match a raw account number against bank_accounts.json. Returns {bank, accountName} or {}."""
    if not account_number:
        return {}
    path = os.path.join(os.path.dirname(__file__), "bank_accounts.json")
    try:
        with open(path) as f:
            accounts = json.load(f)
    except Exception:
        return {}
    digits = "".join(c for c in account_number if c.isdigit())
    return accounts.get(digits, {})


def extract_from_image(image_bytes: bytes, mime_type: str) -> dict:
    client = _get_openai()
    b64 = base64.b64encode(image_bytes).decode()
    response = client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}
            ]},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def extract_from_text(text: str) -> dict:
    client = _get_openai()
    response = client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": text[:4000]},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


RECEIPT_BUCKET = "payment-receipts"


def upload_receipt(file_bytes: bytes, filename: str, mime_type: str) -> str:
    """Upload receipt to Supabase Storage and return a permanent public URL."""
    import mimetypes
    from datetime import datetime
    # Prefix with timestamp to avoid collisions
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"{ts}_{filename}"
    client = db.get_client()
    client.storage.from_(RECEIPT_BUCKET).upload(
        path=path,
        file=file_bytes,
        file_options={"content-type": mime_type, "upsert": "true"},
    )
    return client.storage.from_(RECEIPT_BUCKET).get_public_url(path)


def lookup_client(client_name: str, company_name: str) -> dict:
    """Search CRM for best matching client. Returns record or {}."""
    results = []
    if client_name:
        results = db.search_clients(name=client_name, limit=5)
    if not results and company_name:
        results = db.search_clients(company_name=company_name, limit=5)
    if not results:
        return {}
    if client_name:
        for r in results:
            name = r.get("name", "").lower()
            if client_name.lower() in name or name in client_name.lower():
                return r
    return results[0]


def send_to_make(payload: dict) -> None:
    webhook_url = os.environ.get("PAYMENT_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("PAYMENT_WEBHOOK_URL not set — skipping webhook")
        return
    resp = httpx.post(webhook_url, json=payload, timeout=30)
    if resp.status_code >= 400:
        logger.error("Payment webhook error %s: %s", resp.status_code, resp.text)
