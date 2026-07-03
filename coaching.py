import json
import os
import re
import httpx
from datetime import date as _date
from openai import OpenAI
import database as db

_MONTHS = {
    'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
    'jan':1,'feb':2,'mar':3,'apr':4,'jun':6,'jul':7,'aug':8,
    'sep':9,'sept':9,'oct':10,'nov':11,'dec':12,
}

def _parse_date_from_text(text: str) -> str:
    """Extract session date from the first 500 chars of a transcript using regex."""
    chunk = text[:500]

    # DD/MM/YYYY or D/M/YYYY
    m = re.search(r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b', chunk)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    # "Month DD, YYYY" or "Month DD YYYY" e.g. "June 10, 2026" / "June 10 2026"
    m = re.search(r'\b([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\b', chunk)
    if m and m.group(1).lower() in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"

    # "DD Month YYYY" e.g. "10 June 2026"
    m = re.search(r'\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b', chunk)
    if m and m.group(2).lower() in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"

    # "Month DD" with no year — infer current year e.g. "June 10"
    m = re.search(r'\b([A-Za-z]+)\s+(\d{1,2})\b', chunk)
    if m and m.group(1).lower() in _MONTHS:
        return f"{_date.today().year}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"

    return ""

_openai_client = None


def _clean(value: str) -> str:
    """Strip markdown bold/italic from GPT field values."""
    if not value:
        return value
    value = re.sub(r'\*\*(.+?)\*\*', r'\1', value, flags=re.DOTALL)
    value = re.sub(r'\*(.+?)\*', r'\1', value)
    value = re.sub(r'_(.+?)_', r'\1', value)
    return value


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
    monthly_rows = db.get_client().from_("company_sales").select("year,month,type,amount") \
        .eq("company_id", company_id).in_("year", [2025, 2026]).in_("type", ["actual", "target"]) \
        .order("year").order("month").execute().data if company_id else []

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


def process_transcript(transcript: str, chat_id: int, client_name: str = None, business_name: str = None, client_record: dict = None, thread_id: int = None, client_name_2: str = None, client_record_2: dict = None, proof_photo_url: str = None, confirmed_date: str = None) -> tuple[str, dict]:
    """
    Process a coaching session transcript.
    Returns (formatted_markdown, payload_dict).
    """
    client = _get_openai()

    # Step 1: Use user-confirmed date (already validated), skip re-extraction
    import logging as _logging
    if confirmed_date:
        session_date = confirmed_date
    else:
        session_date = _parse_date_from_text(transcript)
        _logging.getLogger(__name__).info("Date extraction — regex result: %r", session_date)
        if not session_date:
            extract = client.chat.completions.create(
                model="gpt-5.4",
                messages=[
                    {"role": "system", "content": f"Today is {_date.today().isoformat()}. Extract the session date from this coaching transcript. The date may appear in any format. Convert it to YYYY-MM-DD. Return ONLY a JSON object with key: date. If no date is found, return {{\"date\": \"\"}}."},
                    {"role": "user", "content": transcript[:1000]},
                ],
                response_format={"type": "json_object"},
            )
            extracted = json.loads(extract.choices[0].message.content)
            session_date = extracted.get("date", "")
        if not session_date:
            session_date = _date.today().isoformat()
    if not client_name:
        client_name = "Unknown"
    if client_name_2:
        client_name = f"{client_name}\n{client_name_2}"
    if not business_name:
        business_name = "Unknown"

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
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )

    payload = json.loads(summary.choices[0].message.content)

    # Enforce extracted date if GPT missed it
    if not payload.get("date") and session_date:
        payload["date"] = session_date

    # Enforce Supabase sales figures
    payload["sales2025"] = data["sales2025"]
    payload["sales2026"] = data["sales2026"]
    payload["target2026"] = data["target2026"]
    payload["telegramChatId"] = str(chat_id)
    if thread_id:
        payload["telegramThreadId"] = str(thread_id)
    if proof_photo_url:
        payload["proofPhotoUrl"] = proof_photo_url

    # Step 4: Format markdown preview for Telegram
    def g(key): return _clean(payload.get(key, '') or '')
    markdown = f"""**RINGKASAN SESI COACHING**

---

**1. CLIENT OVERVIEW**
Nama Klien: {g('clientName')}
Nama Bisnes: {g('businessName')}
Tarikh Sesi: {g('date')}
Jualan 2025: {g('sales2025')}
Jualan Semasa 2026: {g('sales2026')}
Target Jualan 2026: {g('target2026')}

---

**2. CURRENT STATE**
{g('currentState')}

---

**3. CHALLENGES / PAIN POINTS**
{g('challenges')}

---

**4. ROOT CAUSE**
{g('rootCause')}

---

**5. GUIDANCE / STRATEGY**
{g('guidance')}

---

**6. CLIENT'S AGREED ACTIONS**
{_format_actions(payload)}

---

**7. CAM FOLLOW-UP REMARK**
{g('followUp')}

---

**8. AI INSIGHTS & SUGGESTIONS**
{g('suggestions')}"""

    # Step 5: Send to Make webhook
    webhook_url = os.environ.get("COACHING_WEBHOOK_URL")
    if webhook_url:
        httpx.post(webhook_url, json=payload, timeout=30)

    # Step 6: Save session to Supabase for daily report
    try:
        db.save_coaching_session(
            payload=payload,
            session_date=session_date or str(__import__("datetime").date.today()),
            client_name=client_name or "",
            business_name=business_name or "",
            member_id=data.get("member_id", ""),
            cam=data.get("cam", ""),
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to save coaching session: %s", e)

    return markdown, payload


def _format_actions(payload: dict) -> str:
    lines = []
    for i in range(1, 6):
        action = _clean(payload.get(f"action{i}", "") or "")
        task = _clean(payload.get(f"task{i}", "") or "")
        deadline = _clean(payload.get(f"deadline{i}", "") or "")
        if action:
            line = f"**{action}**"
            if task:
                line += f"\n{task}"
            if deadline:
                line += f"\nDeadline: {deadline}"
            lines.append(line)
    return "\n\n".join(lines)
