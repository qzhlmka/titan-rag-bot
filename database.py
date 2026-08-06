import os
import threading
from supabase import create_client, Client

_local = threading.local()


def get_client() -> Client:
    if not getattr(_local, "client", None):
        _local.client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
    return _local.client


def semantic_search(query_embedding: list[float], limit: int = 5) -> list[dict]:
    result = get_client().rpc(
        "match_documents",
        {"query_embedding": query_embedding, "match_count": limit, "filter": {}},
    ).execute()
    return result.data or []


def search_clients(
    name: str = None,
    company_name: str = None,
    cam: str = None,
    expiry_before: str = None,
    expiry_after: str = None,
    limit: int = 50,
) -> list[dict]:
    # Resolve company_name → company_id server-side so filtering is accurate
    company_ids = None
    if company_name:
        res = (
            get_client()
            .from_("company_details")
            .select("company_id")
            .ilike("company_name", f"%{company_name}%")
            .execute()
        )
        company_ids = [r["company_id"] for r in (res.data or [])]
        if not company_ids:
            return []

    q = (
        get_client()
        .from_("client_details")
        .select("*, company_details(company_name, brand, industry, location)")
        .limit(limit)
    )
    if name:
        q = q.ilike("name", f"%{name}%")
    if cam:
        q = q.ilike("cam", f"%{cam}%")
    if expiry_before:
        q = q.lte("expiry_date", expiry_before)
    if expiry_after:
        q = q.gte("expiry_date", expiry_after)
    if company_ids:
        q = q.in_("company_id", company_ids)

    return q.execute().data or []


def search_ex_clients(
    name: str = None,
    company_name: str = None,
    limit: int = 50,
) -> list[dict]:
    company_ids = None
    if company_name:
        res = (
            get_client()
            .from_("ex_company_details")
            .select("company_id")
            .ilike("company_name", f"%{company_name}%")
            .execute()
        )
        company_ids = [r["company_id"] for r in (res.data or [])]
        if not company_ids:
            return []

    q = get_client().from_("ex_client_details").select("*").limit(limit)
    if name:
        q = q.ilike("name", f"%{name}%")
    if company_ids:
        q = q.in_("company_id", company_ids)

    rows = q.execute().data or []

    # No FK from ex_client_details → ex_company_details, so attach manually
    company_cache = {}
    for row in rows:
        cid = row.get("company_id")
        if cid and cid not in company_cache:
            res = (
                get_client()
                .from_("ex_company_details")
                .select("company_name, brand, industry, location")
                .eq("company_id", cid)
                .limit(1)
                .execute()
            )
            company_cache[cid] = (res.data or [{}])[0]
        row["company_details"] = company_cache.get(cid, {}) if cid else {}

    return rows


def get_company_details(company_name: str) -> list[dict]:
    result = (
        get_client()
        .from_("company_details")
        .select("*")
        .ilike("company_name", f"%{company_name}%")
        .limit(10)
        .execute()
    )
    return result.data or []


def get_sales_summary(
    company_name: str,
    year: int = None,
    month: int = None,
) -> list[dict]:
    mapping = (
        get_client()
        .from_("company_details")
        .select("company_id, company_name")
        .ilike("company_name", f"%{company_name}%")
        .limit(1)
        .execute()
    )
    if not mapping.data:
        return []

    company_id = mapping.data[0]["company_id"]
    q = (
        get_client()
        .from_("company_sales")
        .select("year, month, type, amount")
        .eq("company_id", company_id)
    )
    if year:
        q = q.eq("year", year)
    if month:
        q = q.eq("month", month)
    result = q.order("year", desc=True).order("month", desc=True).limit(500).execute()
    return result.data or []


def list_companies(
    industry: str = None,
    location: str = None,
    partner_in_circle: str = None,
    limit: int = 150,
) -> list[dict]:
    q = get_client().from_("company_details").select(
        "company_name, brand, industry, location, partner_in_circle, total_duration"
    ).limit(limit)
    if industry:
        q = q.ilike("industry", f"%{industry}%")
    if location:
        q = q.ilike("location", f"%{location}%")
    if partner_in_circle:
        q = q.ilike("partner_in_circle", f"%{partner_in_circle}%")
    result = q.execute()
    return result.data or []


def execute_sql(query: str):
    try:
        result = get_client().rpc("execute_readonly_sql", {"query": query.rstrip("; \n")}).execute()
        data = result.data
        if isinstance(data, list):
            return data
        if isinstance(data, str):
            import json
            return json.loads(data)
        return data or []
    except Exception as e:
        return f"SQL error: {e}"


def get_top_companies_by_sales(
    limit: int = 5,
    year: int = None,
    month: int = None,
) -> list[dict]:
    result = get_client().rpc(
        "get_top_companies_by_sales",
        {
            "p_limit": limit,
            "p_year": year,
            "p_month": month,
        },
    ).execute()
    return result.data or []


def save_payment(payload: dict) -> None:
    """Persist a confirmed payment to the payments table."""
    raw_amount = payload.get("installmentAmount", "")
    try:
        import re as _re
        amount = float(_re.sub(r"[^\d.]", "", str(raw_amount))) if raw_amount else None
    except ValueError:
        amount = None

    get_client().from_("payments").insert({
        "member_id": payload.get("memberId") or None,
        "name": payload.get("clientName") or None,
        "company_name": payload.get("companyName") or None,
        "amount": amount,
        "attachment_link": payload.get("attachmentLink") or None,
        "payment_month": payload.get("installmentMonth") or None,
        "payment_datetime": payload.get("paymentDateTime") or None,
        "cam": payload.get("cam") or None,
        "reference": payload.get("reference") or None,
        "account_number": payload.get("recipientAccount") or None,
        "bank_name": payload.get("bankName") or None,
        "account_name": payload.get("accountName") or None,
        "remark": payload.get("remark") or None,
        "source": "bot",
    }).execute()


def save_coaching_session(payload: dict, session_date: str, client_name: str, business_name: str, member_id: str, cam: str) -> None:
    get_client().from_("coaching_sessions").insert({
        "session_date": session_date,
        "client_name": client_name,
        "business_name": business_name,
        "member_id": member_id,
        "cam": cam,
        "payload": payload,
    }).execute()


def get_sessions_by_date(date_str: str) -> list[dict]:
    result = (
        get_client()
        .from_("coaching_sessions")
        .select("*")
        .eq("session_date", date_str)
        .order("created_at")
        .execute()
    )
    return result.data or []


def get_expiring_memberships(within_days: int = 30) -> list[dict]:
    from datetime import date, timedelta
    today = date.today().isoformat()
    future = (date.today() + timedelta(days=within_days)).isoformat()
    result = (
        get_client()
        .from_("client_details")
        .select("name, email, phone, expiry_date, company_details(company_name)")
        .gte("expiry_date", today)
        .lte("expiry_date", future)
        .order("expiry_date")
        .limit(500)
        .execute()
    )
    return result.data or []
