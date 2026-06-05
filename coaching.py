import json
import os
import httpx
from openai import OpenAI
import database as db

_openai_client = None


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}


def _get_client_data(client_name: str, business_name: str, client_record: dict = None) -> dict:
    """Fetch comprehensive client and sales data from Supabase.

    If client_record is provided (confirmed by user), it is used directly.
    Otherwise falls back to name-based search.
    """
    def fmt(v): return f"RM{float(v):,.2f}"

    if not client_record:
        # Fallback: search by name, prefer record whose company matches business_name
        client_record = {}
        client_results = db.search_clients(name=client_name, limit=5)
        if client_results:
            for c in client_results:
                company_data = c.get("company_details") or {}
                cname = company_data.get("company_name", "") if isinstance(company_data, dict) else ""
                if business_name.lower() in cname.lower() or cname.lower() in business_name.lower():
                    client_record = c
                    break
            if not client_record:
                client_record = client_results[0]

    member_id = client_record.get("member_id", "")
    membership_tier = "Titan Circle Plus" if str(member_id).upper().startswith("TCP") else "Titanium Circle"
    cam = client_record.get("cam", "N/A")
    start_date = client_record.get("start_date", "N/A")
    expiry_date = client_record.get("expiry_date", "N/A")
    pax = client_record.get("pax", "N/A")

    # Prefer company_id from the confirmed record for accurate sales lookup
    company_id_from_record = client_record.get("company_id", "")

    # Look up full company details
    company = db.get_company_details(business_name)
    company_info = company[0] if company else {}

    # If company not found via business_name, try from the client record
    if not company_info and client_record.get("company_details"):
        company_data = client_record.get("company_details")
        if isinstance(company_data, dict):
            company_info = company_data

    # All clients under this company
    all_clients = db.search_clients(company_name=business_name, limit=20)
    client_lines = []
    for c in (all_clients or []):
        tier = "Titan Circle Plus" if str(c.get("member_id","")).upper().startswith("TCP") else "Titanium Circle"
        client_lines.append(f"  - {c.get('name','')} ({c.get('member_id','')}) | CAM: {c.get('cam','')} | Expiry: {c.get('expiry_date','')} | Tier: {tier}")
    client_list = "\n".join(client_lines) if client_lines else "  Tiada data"

    # Monthly sales 2025 and 2026 — prefer company_id from the confirmed client record
    company_id = company_id_from_record or company_info.get("company_id", "")
    monthly_rows = db.execute_sql(f"""
        SELECT year, month, type, amount
        FROM company_sales
        WHERE company_id = '{company_id}'
        AND year IN (2025, 2026)
        AND type IN ('actual', 'target')
        ORDER BY year, month
    """) if company_id else []

    sales2025 = sum(float(r["amount"] or 0) for r in (monthly_rows or []) if isinstance(r, dict) and r.get("year") == 2025 and r.get("type") == "actual")
    sales2026 = sum(float(r["amount"] or 0) for r in (monthly_rows or []) if isinstance(r, dict) and r.get("year") == 2026 and r.get("type") == "actual")
    target2026 = sum(float(r["amount"] or 0) for r in (monthly_rows or []) if isinstance(r, dict) and r.get("year") == 2026 and r.get("type") == "target")

    monthly_lines = []
    for r in (monthly_rows or []):
        if isinstance(r, dict):
            monthly_lines.append(f"  {MONTH_NAMES.get(r.get('month'), r.get('month'))} {r.get('year')} ({r.get('type')}): {fmt(r.get('amount') or 0)}")
    monthly_breakdown = "\n".join(monthly_lines) if monthly_lines else "  Tiada data"

    return {
        "sales2025": fmt(sales2025),
        "sales2026": fmt(sales2026),
        "target2026": fmt(target2026),
        "company_info": company_info,
        "client_list": client_list,
        "monthly_breakdown": monthly_breakdown,
        "member_id": member_id,
        "membership_tier": membership_tier,
        "cam": cam,
        "start_date": start_date,
        "expiry_date": expiry_date,
        "pax": pax,
    }


SYSTEM_PROMPT = """You are a brutally honest professional assistant trained to support RichWorks Consulting Group in summarizing one-on-one coaching sessions between Datuk Wira and Titan members. Your role is to analyze full session transcripts and generate a clear, 1-page business case summary based on the Titan 101 format.

You will be provided with:
- The full transcript of the coaching session
- Client and company information from the CRM database
- Detailed monthly sales data for 2025 and 2026
- Client membership details

USE the sales data and client information to enrich your analysis — especially for currentState, suggestions, and followUp. Reference actual figures when relevant.

Generate a JSON object with EXACTLY these fields (all content in Bahasa Malaysia):
- clientName: full name of the client
- businessName: name of the client's business
- date: session date in YYYY-MM-DD format
- sales2025: total actual sales 2025 as provided
- sales2026: YTD actual sales 2026 as provided
- target2026: sales target 2026 as provided
- currentState: Terangkan situasi bisnes sekarang berdasarkan transkrip DAN data jualan. Bandingkan prestasi semasa dengan tahun lepas. Fahami perjalanan klien dan apakah perubahan atau perkembangan bisnes yang sedang berlaku.
- challenges: Challenges/pain points as a newline-separated list (no numbers, one item per line)
- rootCause: Kenal pasti kepercayaan, corak pemikiran, atau isu dalaman sebenar yang menghalang kemajuan.
- guidance: Nasihat atau strategi utama yang diberikan oleh Datuk Wira dalam sesi.
- action1 to action5: 5 agreed action items (use "" if fewer than 5)
- task1 to task5: specific task details for each action (newline-separated list, no numbers)
- deadline1 to deadline5: deadlines ("" if not specified)
- followUp: Catatan untuk CAM – perkara perlu diperhatikan, follow-up penting, atau potensi halangan. Sertakan pemerhatian berdasarkan data jualan jika relevan.
- suggestions: Berdasarkan keseluruhan perbualan DAN data jualan bulanan, berikan jangkaan munasabah terhadap prestasi bisnes ini. Adakah klien boleh capai target 2026? Apakah trend jualan? Anggaran revenue tahun ini? Justifikasi logik dan pertimbangan risiko.

Rules:
- Semua penulisan mesti dalam Bahasa Malaysia yang profesional tetapi mudah difahami.
- Jangan mereka-reka maklumat. Hanya gunakan maklumat dari transkrip dan data CRM.
- Jika sesuatu maklumat tidak jelas, tulis: [Maklumat tidak jelas].
- Jika terdapat kenyataan atau emosi penting dari klien, masukkan sebagai petikan (quote).
- Gaya bahasa perlu jelas, tidak terlalu teknikal, dan bukan robotik.
- Be detailed and elaborative. Total content approximately 4000 characters."""


def process_transcript(transcript: str, chat_id: int, client_name: str = None, business_name: str = None, client_record: dict = None, thread_id: int = None) -> tuple[str, dict]:
    """
    Process a coaching session transcript.
    Returns (formatted_markdown, payload_dict).
    """
    client = _get_openai()

    # Step 1: Extract date from transcript — always at the top, DD/MM/YYYY format
    extract = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Extract the session date from this coaching transcript. The date is at the top of the transcript and uses DD/MM/YYYY format (Malaysian). Convert it to YYYY-MM-DD. Return ONLY a JSON object with key: date. If no date is found, return an empty string."},
            {"role": "user", "content": transcript[:500]},
        ],
        response_format={"type": "json_object"},
    )
    extracted = json.loads(extract.choices[0].message.content)
    if not client_name:
        client_name = "Unknown"
    if not business_name:
        business_name = "Unknown"
    session_date = extracted.get("date", "")

    # Step 2: Fetch comprehensive data from Supabase using provided names
    data = _get_client_data(client_name, business_name, client_record or {})
    company_info = data["company_info"]

    # Step 3: Build rich context for GPT
    user_prompt = f"""=== MAKLUMAT KLIEN (dari CRM) ===
Nama Klien: {client_name}
Member ID: {data.get('member_id', 'N/A')}
Tier Keahlian: {data.get('membership_tier', 'N/A')}
CAM (Pengurus Akaun): {data.get('cam', 'N/A')}
Tarikh Mula Keahlian: {data.get('start_date', 'N/A')}
Tarikh Tamat Keahlian: {data.get('expiry_date', 'N/A')}
Bilangan Pax: {data.get('pax', 'N/A')}

Nama Bisnes: {business_name}
Industri: {company_info.get('industry', 'N/A')}
Lokasi: {company_info.get('location', 'N/A')}
Jenis Bisnes: {company_info.get('nature_of_business', 'N/A')}
Tempoh Keahlian: {company_info.get('total_duration', 'N/A')}
Partner in Circle: {company_info.get('partner_in_circle', 'N/A')}
Made in Spire: {company_info.get('made_in_spire', 'N/A')}
Tarikh Sesi: {session_date}

=== AHLI SYARIKAT ===
{data['client_list']}

=== DATA JUALAN ===
Jumlah Jualan 2025: {data['sales2025']}
Jualan Semasa 2026 (YTD): {data['sales2026']}
Target Jualan 2026: {data['target2026']}

Pecahan Bulanan:
{data['monthly_breakdown']}

=== TRANSKRIP SESI ===
{transcript}"""

    summary = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )

    payload = json.loads(summary.choices[0].message.content)

    # Enforce Supabase sales figures
    payload["sales2025"] = data["sales2025"]
    payload["sales2026"] = data["sales2026"]
    payload["target2026"] = data["target2026"]
    payload["telegramChatId"] = str(chat_id)
    if thread_id:
        payload["telegramThreadId"] = str(thread_id)

    # Step 4: Format markdown preview for Telegram
    markdown = f"""**RINGKASAN SESI COACHING**

---

**1. CLIENT OVERVIEW**
Nama Klien: {payload.get('clientName', '')}
Nama Bisnes: {payload.get('businessName', '')}
Tarikh Sesi: {payload.get('date', '')}
Jualan 2025: {payload.get('sales2025', '')}
Jualan Semasa 2026: {payload.get('sales2026', '')}
Target Jualan 2026: {payload.get('target2026', '')}

---

**2. CURRENT STATE**
{payload.get('currentState', '')}

---

**3. CHALLENGES / PAIN POINTS**
{payload.get('challenges', '')}

---

**4. ROOT CAUSE**
{payload.get('rootCause', '')}

---

**5. GUIDANCE / STRATEGY**
{payload.get('guidance', '')}

---

**6. CLIENT'S AGREED ACTIONS**
{_format_actions(payload)}

---

**7. CAM FOLLOW-UP REMARK**
{payload.get('followUp', '')}

---

**8. AI INSIGHTS & SUGGESTIONS**
{payload.get('suggestions', '')}"""

    # Step 5: Send to Make webhook
    webhook_url = os.environ.get("COACHING_WEBHOOK_URL")
    if webhook_url:
        httpx.post(webhook_url, json=payload, timeout=30)

    return markdown, payload


def _format_actions(payload: dict) -> str:
    lines = []
    for i in range(1, 6):
        action = payload.get(f"action{i}", "")
        task = payload.get(f"task{i}", "")
        deadline = payload.get(f"deadline{i}", "")
        if action:
            line = f"**{action}**"
            if task:
                line += f"\n{task}"
            if deadline:
                line += f"\nDeadline: {deadline}"
            lines.append(line)
    return "\n\n".join(lines)
