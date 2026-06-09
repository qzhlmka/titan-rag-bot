import json
import os
import httpx
from datetime import date
from openai import OpenAI

import database as db

_openai_client = None


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


REPORT_SYSTEM_PROMPT = """You are generating a professional executive summary of a day's 1-on-1 business coaching sessions for internal leadership including Datuk Wira.

Return ONLY a JSON object with EXACTLY these fields (all content in Bahasa Malaysia unless specified):

{
  "TARIKH": "Session date in YYYY-MM-DD",
  "JUMLAH_KLIEN": "Total number of clients as integer",
  "INDUSTRI_TERLIBAT": "Comma-separated list of industries represented",
  "JULAT_JUALAN_SEMASA": "RM[X] – RM[X] revenue range for current year (2025)",
  "SASARAN_JUALAN": "RM[X] – RM[X] target revenue range for next year (2026)",
  "EXEC_SUMMARY": "2–3 sentence strategic overview. Highlight overall market demand strength, common structural challenges, and the leadership readiness theme.",

  "ISSUE_1_TITLE": "", "ISSUE_1_DESCRIPTION": "",
  "CLIENT_1A_NAME": "", "CLIENT_1A_COMPANY": "", "CLIENT_1A_EXAMPLE": "",
  "CLIENT_1B_NAME": "", "CLIENT_1B_COMPANY": "", "CLIENT_1B_EXAMPLE": "",

  "ISSUE_2_TITLE": "", "ISSUE_2_DESCRIPTION": "",
  "CLIENT_2A_NAME": "", "CLIENT_2A_COMPANY": "", "CLIENT_2A_EXAMPLE": "",
  "CLIENT_2B_NAME": "", "CLIENT_2B_COMPANY": "", "CLIENT_2B_EXAMPLE": "",

  "ISSUE_3_TITLE": "", "ISSUE_3_DESCRIPTION": "",
  "CLIENT_3A_COMPANY": "", "CLIENT_3A_EXAMPLE": "",
  "CLIENT_3B_COMPANY": "", "CLIENT_3B_EXAMPLE": "",

  "ISSUE_4_TITLE": "", "ISSUE_4_DESCRIPTION": "",
  "CLIENT_4A_COMPANY": "", "CLIENT_4A_EXAMPLE": "",
  "CLIENT_4B_COMPANY": "", "CLIENT_4B_EXAMPLE": "",

  "ISSUE_5_TITLE": "", "ISSUE_5_DESCRIPTION": "",
  "CLIENT_5A_COMPANY": "", "CLIENT_5A_EXAMPLE": "",
  "CLIENT_5B_COMPANY": "", "CLIENT_5B_EXAMPLE": "",

  "ISSUE_6_TITLE": "", "ISSUE_6_DESCRIPTION": "",
  "CLIENT_6A_COMPANY": "", "CLIENT_6A_EXAMPLE": "",
  "CLIENT_6B_COMPANY": "", "CLIENT_6B_EXAMPLE": "",

  "ISSUE_7_TITLE": "", "ISSUE_7_DESCRIPTION": "",
  "CLIENT_7A_COMPANY": "", "CLIENT_7A_EXAMPLE": "",
  "CLIENT_7B_COMPANY": "", "CLIENT_7B_EXAMPLE": "",

  "ISSUE_8_TITLE": "", "ISSUE_8_DESCRIPTION": "",
  "CLIENT_8A_COMPANY": "", "CLIENT_8A_EXAMPLE": "",
  "CLIENT_8B_COMPANY": "", "CLIENT_8B_EXAMPLE": "",

  "ISSUE_9_TITLE": "", "ISSUE_9_DESCRIPTION": "",
  "CLIENT_9A_COMPANY": "", "CLIENT_9A_EXAMPLE": "",
  "CLIENT_9B_COMPANY": "", "CLIENT_9B_EXAMPLE": "",

  "ISSUE_10_TITLE": "", "ISSUE_10_DESCRIPTION": "",
  "CLIENT_10A_COMPANY": "", "CLIENT_10A_EXAMPLE": "",
  "CLIENT_10B_COMPANY": "", "CLIENT_10B_EXAMPLE": "",

  "STRATEGY_AREA_1": "", "STRATEGY_INTERVENTION_1": "",
  "STRATEGY_AREA_2": "", "STRATEGY_INTERVENTION_2": "",
  "STRATEGY_AREA_3": "", "STRATEGY_INTERVENTION_3": "",
  "STRATEGY_AREA_4": "", "STRATEGY_INTERVENTION_4": "",
  "STRATEGY_AREA_5": "", "STRATEGY_INTERVENTION_5": "",
  "STRATEGY_AREA_6": "", "STRATEGY_INTERVENTION_6": "",
  "STRATEGY_AREA_7": "", "STRATEGY_INTERVENTION_7": "",

  "HIGH_PERFORMER_1_COMPANY": "", "HIGH_PERFORMER_1_ANALYSIS": "",
  "HIGH_PERFORMER_2_COMPANY": "", "HIGH_PERFORMER_2_ANALYSIS": "",
  "HIGH_PERFORMER_3_COMPANY": "", "HIGH_PERFORMER_3_ANALYSIS": "",

  "RISK_CLIENT_1_COMPANY": "", "RISK_CLIENT_1_ANALYSIS": "",
  "RISK_CLIENT_2_COMPANY": "", "RISK_CLIENT_2_ANALYSIS": "",

  "LEADERSHIP_INSIGHTS": "2–3 sentence closing insight for Datuk Wira and internal leadership. Focus on the core bottleneck theme across all clients this session."
}

Rules:
- Be strategic, sharp, and presentation-ready.
- ALWAYS fill all 10 issue slots, all 7 strategy slots, and all 3 high performer slots. If sessions are limited, break broad issues into more specific sub-themes, elaborate deeper on each client's situation, or project forward based on available data.
- For HIGH_PERFORMER fields: if no client is clearly exceeding targets, identify the one(s) with the best relative momentum or highest potential and explain why.
- Never leave a TITLE, AREA, or COMPANY field empty — always provide a value.
- Use integer (not string) for JUMLAH_KLIEN."""


def _clean(value: str) -> str:
    """Strip markdown bold/italic from GPT field values to avoid clashing with _format_markdown."""
    import re
    value = re.sub(r'\*\*(.+?)\*\*', r'\1', value)
    value = re.sub(r'\*(.+?)\*', r'\1', value)
    value = re.sub(r'_(.+?)_', r'\1', value)
    return value


def _format_markdown(p: dict) -> str:
    def g(key): return _clean(p.get(key, ""))
    issues = []
    for n in range(1, 11):
        title = g(f"ISSUE_{n}_TITLE")
        if not title:
            continue
        desc = g(f"ISSUE_{n}_DESCRIPTION")
        examples = []
        for letter in ("A", "B"):
            name = g(f"CLIENT_{n}{letter}_NAME") or g(f"CLIENT_{n}{letter}_COMPANY")
            company = g(f"CLIENT_{n}{letter}_COMPANY")
            example = g(f"CLIENT_{n}{letter}_EXAMPLE")
            if name or example:
                label = name
                if company and company != name:
                    label += f" ({company})"
                examples.append(f"  • {label}: {example}")
        block = f"**{n}. {title}**\n_{desc}_"
        if examples:
            block += "\n" + "\n".join(examples)
        issues.append(block)

    strategies = []
    for n in range(1, 8):
        area = g(f"STRATEGY_AREA_{n}")
        intervention = g(f"STRATEGY_INTERVENTION_{n}")
        if area:
            strategies.append(f"🔹 **{area}**\n    {intervention}")

    high = []
    for n in range(1, 4):
        company = g(f"HIGH_PERFORMER_{n}_COMPANY")
        analysis = g(f"HIGH_PERFORMER_{n}_ANALYSIS")
        if company:
            high.append(f"  • **{company}**: {analysis}")

    risk = []
    for n in range(1, 3):
        company = g(f"RISK_CLIENT_{n}_COMPANY")
        analysis = g(f"RISK_CLIENT_{n}_ANALYSIS")
        if company:
            risk.append(f"  • **{company}**: {analysis}")

    return f"""**🧾 EXECUTIVE SUMMARY – 1-ON-1 BUSINESS COACHING**

📅 Tarikh: {g('TARIKH')}
👥 Jumlah Klien: {p.get('JUMLAH_KLIEN', '')}
🏭 Industri: {g('INDUSTRI_TERLIBAT')}
💰 Julat Jualan 2025: {g('JULAT_JUALAN_SEMASA')}
🎯 Sasaran 2026: {g('SASARAN_JUALAN')}

_{g('EXEC_SUMMARY')}_

---

**🔍 TOP 10 ISU UTAMA**

{chr(10).join(issues) if issues else 'Tiada data'}

---

**🎯 HALA TUJU STRATEGIK HARI INI**

{chr(10).join(strategies) if strategies else 'Tiada data'}

---

**📈 POTENSI IMPAK HASIL (AI ESTIMATE)**

🟢 *Projected High Performers:*
{chr(10).join(high) if high else '  Tiada data'}

🔴 *Projected Risk Clients:*
{chr(10).join(risk) if risk else '  Tiada data'}

---

**💡 LEADERSHIP INSIGHTS**
_{g('LEADERSHIP_INSIGHTS')}_"""


def generate_daily_report(target_date: str = None, chat_id: int = None, thread_id: int = None) -> tuple[str, int]:
    """
    Generate an executive summary for all coaching sessions on target_date.
    Returns (formatted_markdown, session_count).
    """
    if not target_date:
        target_date = date.today().isoformat()

    sessions = db.get_sessions_by_date(target_date)
    if not sessions:
        return f"No coaching sessions recorded for {target_date}.", 0

    session_summaries = []
    for i, s in enumerate(sessions, 1):
        payload = s.get("payload") or {}
        session_summaries.append(
            f"--- Session {i} ---\n"
            f"Client: {s.get('client_name', payload.get('clientName', 'N/A'))}\n"
            f"Business: {s.get('business_name', payload.get('businessName', 'N/A'))}\n"
            f"CAM: {s.get('cam', 'N/A')}\n"
            f"Date: {s.get('session_date', payload.get('date', 'N/A'))}\n"
            f"Sales 2025: {payload.get('sales2025', 'N/A')}\n"
            f"Sales 2026 (YTD): {payload.get('sales2026', 'N/A')}\n"
            f"Target 2026: {payload.get('target2026', 'N/A')}\n"
            f"Current State: {payload.get('currentState', 'N/A')}\n"
            f"Challenges: {payload.get('challenges', 'N/A')}\n"
            f"Root Cause: {payload.get('rootCause', 'N/A')}\n"
            f"Guidance: {payload.get('guidance', 'N/A')}\n"
            f"Follow-Up: {payload.get('followUp', 'N/A')}\n"
            f"Suggestions: {payload.get('suggestions', 'N/A')}\n"
        )

    user_prompt = f"Today's date: {target_date}\nTotal sessions: {len(sessions)}\n\n" + "\n".join(session_summaries)

    client = _get_openai()
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": REPORT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )

    structured = json.loads(response.choices[0].message.content)

    import logging
    logging.getLogger(__name__).info("Daily report JSON keys with values: %s",
        {k: v for k, v in structured.items() if v})

    # Add Telegram routing fields
    structured["telegramChatId"] = str(chat_id) if chat_id else ""
    if thread_id:
        structured["telegramThreadId"] = str(thread_id)

    # Send to Make webhook
    webhook_url = os.environ.get("DAILY_REPORT_WEBHOOK_URL")
    if webhook_url:
        httpx.post(webhook_url, json=structured, timeout=30)

    return _format_markdown(structured), len(sessions)
