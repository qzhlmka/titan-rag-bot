import os
from datetime import date
from openai import OpenAI

import database as db

_openai_client = None


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


REPORT_SYSTEM_PROMPT = """Please analyze the documents and produce a professional, leadership-level executive summary report using this format:

---

### 🧾 Executive Summary – 1-on-1 Business Coaching

Date: [Insert Coaching Date]
Total Clients: [X]
Industries Represented: [List sectors]
Revenue Range (2025): RM[X] – RM[X]
Target Revenue (2026): RM[X] – RM[X]

---

### 🔍 Top 10 Common Issues

For each issue:
* Issue Title (e.g., Tiada Fokus Produk Utama)
* 1-line description
* 2–3 client examples with name, business name, and elaborated context

---

### 🎯 Strategic Direction Advised Today

List 5–7 dynamic strategic areas based on real interventions from the coaching (do not use a fixed template).
Each row should have:
* Area of Focus (e.g. Operational Duplication, Investor Pitch Strategy, SOP Realignment, TikTok Productisation, Team Restructure)
* Intervention Given (brief, 1-line action or guidance)

Format as a markdown table.

---

### 📈 Potential Revenue Impact (AI Estimate)

* Projected High Performers: List clients likely to exceed 2026 targets, with reasons
* Projected Risk Clients: List clients at risk of underperforming, with reasons

---

This summary should be strategic, sharply written, and presentation-ready for internal leadership, including Datuk Wira. Use concise, confident language and always support insights with real client examples."""


def generate_daily_report(target_date: str = None) -> tuple[str, int]:
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
    )

    return response.choices[0].message.content or "Failed to generate report.", len(sessions)
