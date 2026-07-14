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


def _embed(text: str) -> list[float]:
    resp = _get_openai().embeddings.create(model="text-embedding-3-small", input=text)
    return resp.data[0].embedding


def _fetch_client_by_member_id(member_id: str) -> dict:
    """Fetch a full client record (with company_details join) by exact member_id."""
    rows = (
        db.get_client()
        .from_("client_details")
        .select("*, company_details(company_name, brand, industry, location)")
        .eq("member_id", member_id)
        .limit(1)
        .execute()
        .data or []
    )
    return rows[0] if rows else {}


def _fetch_ex_client_by_member_id(member_id: str) -> dict:
    """Fetch a record from ex_client_details by exact member_id, with company details attached."""
    rows = (
        db.get_client()
        .from_("ex_client_details")
        .select("*")
        .eq("member_id", member_id)
        .limit(1)
        .execute()
        .data or []
    )
    if not rows:
        return {}
    row = rows[0]
    cid = row.get("company_id")
    if cid:
        res = (
            db.get_client()
            .from_("ex_company_details")
            .select("company_name, brand, industry, location")
            .eq("company_id", cid)
            .limit(1)
            .execute()
        )
        row["company_details"] = (res.data or [{}])[0]
    else:
        row["company_details"] = {}
    return row


def _fetch_clients_by_company_id(company_id: str) -> list[dict]:
    """Fetch client records for a given company_id (with company_details join)."""
    return (
        db.get_client()
        .from_("client_details")
        .select("*, company_details(company_name, brand, industry, location)")
        .eq("company_id", company_id)
        .limit(5)
        .execute()
        .data or []
    )


def lookup_client(client_name: str, company_name: str) -> dict:
    """Search CRM for best matching client. Returns record or {}.

    Strategy:
    1. Partial name match via search_clients (fast)
    2. Semantic/embedding search — extracts member_id or company_id from
       metadata, then fetches the full structured client record
    """
    results = []
    if client_name:
        results = db.search_clients(name=client_name, limit=5)
    if not results and company_name:
        results = db.search_clients(company_name=company_name, limit=5)

    if not results:
        query = " ".join(filter(None, [client_name, company_name]))
        if query.strip():
            try:
                embedding = _embed(query)
                sem_hits = db.semantic_search(embedding, limit=8)
                # sem_hits rows: {id, content, metadata, similarity}
                # metadata: {"type": "client", "member_id": "..."} or
                #           {"type": "company", "company_id": "..."}
                for hit in sem_hits:
                    meta = hit.get("metadata") or {}
                    if meta.get("type") == "client" and meta.get("member_id"):
                        rec = _fetch_client_by_member_id(meta["member_id"])
                        if rec:
                            results.append(rec)
                if not results:
                    for hit in sem_hits:
                        meta = hit.get("metadata") or {}
                        if meta.get("type") == "company" and meta.get("company_id"):
                            recs = _fetch_clients_by_company_id(meta["company_id"])
                            if recs:
                                results.extend(recs)
                                break
            except Exception as e:
                logger.warning("Semantic search fallback failed: %s", e)

    if not results:
        # Fall back to ex-client tables
        if client_name:
            results = db.search_ex_clients(name=client_name, limit=5)
        if not results and company_name:
            results = db.search_ex_clients(company_name=company_name, limit=5)

    if not results:
        return {}

    if client_name:
        for r in results:
            name = r.get("name", "").lower()
            if client_name.lower() in name or name in client_name.lower():
                return r
    return results[0]


def _close_matches(results: list[dict], client_name: str) -> list[dict]:
    """Return candidates from results that match client_name.
    Returns ALL records with the exact same name (genuine duplicates → caller disambiguates).
    For partial matches, returns only the single best to avoid flooding with common substrings."""
    if not results:
        return []
    if not client_name:
        return results[:1]
    cl = client_name.lower().strip()
    exact = [r for r in results if r.get("name", "").lower().strip() == cl]
    if exact:
        return exact
    partial = [r for r in results if cl in r.get("name", "").lower() or r.get("name", "").lower() in cl]
    return partial[:1] if partial else results[:1]


def lookup_client_all(client_name: str, company_name: str) -> list[dict]:
    """Search both active and ex-client tables. Returns all close-match candidates,
    each tagged with '_source': 'active' or 'ex'. Returns [] if nothing found.
    Caller should disambiguate when len > 1."""
    # Active tables
    active_results = []
    if client_name:
        active_results = db.search_clients(name=client_name, limit=5)
    if not active_results and company_name:
        active_results = db.search_clients(company_name=company_name, limit=5)

    # Semantic fallback (embeddings may include both active and ex-clients)
    sem_ex_results = []
    if not active_results:
        query = " ".join(filter(None, [client_name, company_name]))
        if query.strip():
            try:
                embedding = _embed(query)
                sem_hits = db.semantic_search(embedding, limit=8)
                for hit in sem_hits:
                    meta = hit.get("metadata") or {}
                    if meta.get("type") == "client" and meta.get("member_id"):
                        rec = _fetch_client_by_member_id(meta["member_id"])
                        if rec:
                            active_results.append(rec)
                        else:
                            # Not in active table — check if it's an ex-client
                            ex_rec = _fetch_ex_client_by_member_id(meta["member_id"])
                            if ex_rec:
                                sem_ex_results.append(ex_rec)
                if not active_results:
                    for hit in sem_hits:
                        meta = hit.get("metadata") or {}
                        if meta.get("type") == "company" and meta.get("company_id"):
                            recs = _fetch_clients_by_company_id(meta["company_id"])
                            if recs:
                                active_results.extend(recs)
                                break
            except Exception as e:
                logger.warning("Semantic search fallback failed: %s", e)

    # Ex-client tables (text search)
    ex_results = []
    if client_name:
        ex_results = db.search_ex_clients(name=client_name, limit=5)
    if not ex_results and company_name:
        ex_results = db.search_ex_clients(company_name=company_name, limit=5)

    # Merge semantic ex-hits into ex_results (dedup by member_id)
    seen_ex_ids = {r.get("member_id") for r in ex_results}
    for r in sem_ex_results:
        if r.get("member_id") not in seen_ex_ids:
            ex_results.append(r)
            seen_ex_ids.add(r.get("member_id"))

    candidates = []
    for r in _close_matches(active_results, client_name):
        r["_source"] = "active"
        candidates.append(r)
    for r in _close_matches(ex_results, client_name):
        r["_source"] = "ex"
        candidates.append(r)
    return candidates


def send_to_make(payload: dict) -> None:
    webhook_url = os.environ.get("PAYMENT_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("PAYMENT_WEBHOOK_URL not set — skipping webhook")
        return
    resp = httpx.post(webhook_url, json=payload, timeout=30)
    if resp.status_code >= 400:
        logger.error("Payment webhook error %s: %s", resp.status_code, resp.text)
