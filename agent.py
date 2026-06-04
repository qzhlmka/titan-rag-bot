import json
import os
import httpx
from typing import Optional
from openai import OpenAI
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, AIMessage
import database as db
import slides as slide_gen

_openai_client = None


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


def embed(text: str) -> list[float]:
    resp = _get_openai().embeddings.create(model="text-embedding-3-small", input=text)
    return resp.data[0].embedding


# ── Tools ─────────────────────────────────────────────────────────────────────

@tool
def semantic_search(query: str, limit: int = 5) -> str:
    """Search client and company records using natural language.
    Use for vague or descriptive queries like 'health companies in KL' or 'find someone named Ahmad'.
    For 'find similar to X' queries: first call get_company_details to get X's attributes,
    then build a rich descriptive query from industry/location/business type — never search with just the name."""
    embedding = embed(query)
    results = db.semantic_search(embedding, limit=limit)
    return json.dumps(results, default=str)


@tool
def search_clients(
    name: Optional[str] = None,
    company_name: Optional[str] = None,
    cam: Optional[str] = None,
    expiry_before: Optional[str] = None,
    expiry_after: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Search clients by name, company, CAM (account manager), or membership expiry date.
    Use ISO date format YYYY-MM-DD for dates."""
    results = db.search_clients(
        name=name, company_name=company_name, cam=cam,
        expiry_before=expiry_before, expiry_after=expiry_after, limit=limit,
    )
    return json.dumps(results, default=str)


@tool
def get_company_details(company_name: str) -> str:
    """Get full details of a company by name, including industry, location, brand, and business type."""
    results = db.get_company_details(company_name)
    return json.dumps(results, default=str)


@tool
def get_sales_summary(
    company_name: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> str:
    """Get actual sales data for a specific company, optionally filtered by a single year and/or a single month.
    Only use this for a specific month or full year — NOT for month ranges. For ranges (e.g. Jan–Jun 2025), use execute_sql instead."""
    results = db.get_sales_summary(company_name=company_name, year=year, month=month)
    return json.dumps(results, default=str)


@tool
def list_companies(
    industry: Optional[str] = None,
    location: Optional[str] = None,
    partner_in_circle: Optional[str] = None,
    limit: int = 150,
) -> str:
    """List companies filtered by industry, location, or partner status."""
    results = db.list_companies(industry=industry, location=location,
                                partner_in_circle=partner_in_circle, limit=limit)
    return json.dumps(results, default=str)


@tool
def get_top_companies_by_sales(
    limit: int = 5,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> str:
    """Get companies ranked by total actual sales, highest first.
    Use for queries like 'top companies by sales' or 'best performing companies'."""
    results = db.get_top_companies_by_sales(limit=limit, year=year, month=month)
    return json.dumps(results, default=str)


@tool
def get_expiring_memberships(within_days: int = 30) -> str:
    """Get clients whose memberships are expiring within a number of days from today."""
    results = db.get_expiring_memberships(within_days=within_days)
    return json.dumps(results, default=str)


@tool
def execute_sql(query: str) -> str:
    """Run a read-only SELECT query against the database.
    Use for any query the other tools cannot handle — distinct lists, counts, aggregations, groupings, rankings, joins, etc.

    Tables and views:
    - client_company_view: Pre-joined view of clients + their company info. USE THIS for any query involving both client and company data.
      Columns: member_id, client_name, cam (Client Account Manager), email, phone, ic_no,
               start_date, expiry_date (text YYYY-MM-DD), pax (number of people from same company),
               membership_tier (computed: 'Titan Circle Plus' or 'Titanium Circle'),
               company_id, company_name, brand, industry, nature_of_business, location,
               total_duration (membership length), partner_in_circle (has partner in Titanium Circle),
               made_in_spire (came from Spire), entiti_bisnes (business entity type)

    - company_details: company_id, company_name, brand, industry, nature_of_business, location,
        total_duration (how long the company has been a member),
        made_in_spire (whether the client/company comes from Spire),
        register_no (business registration number),
        entiti_bisnes (business entity type e.g. Sdn Bhd),
        partner_in_circle (whether the company has a partner in Titanium Circle)
    - client_details: member_id, name, company_id,
        cam (Client Account Manager — staff managing this client),
        email, phone, ic_no,
        start_date (membership start date, stored as text YYYY-MM-DD),
        expiry_date (membership expiry date, stored as text YYYY-MM-DD),
        pax (number of people from the same company under this membership)
    - company_sales: company_id, year, month, type, amount
      NOTE: company_sales has no company_name — join to company_details on company_id to get it.

    Ex-clients/companies (those who have left):
    - ex_client_company_view: same as client_company_view but for ex-clients, plus moved_at (date they left). USE THIS for ex-client queries.
    - ex_client_details: member_id, name, company_id, cam, email, phone, ic_no, start_date, expiry_date, pax, moved_at
    - ex_company_details: company_id, company_name, brand, industry, nature_of_business, location, total_duration, made_in_spire, entiti_bisnes, partner_in_circle, moved_at
    - ex_company_sales: company_id, year, month, type, amount, moved_at

    When the user asks about "clients" achieving sales targets, they mean companies (sales are per company, not per person).

    CRITICAL — avoid double-counting sales:
    - NEVER join client_company_view with company_sales to calculate totals. A company with 2 clients produces 2 rows in client_company_view, so joining with company_sales will double the sales figure.
    - For any sales calculation or aggregation, always join company_details with company_sales (not client_company_view).
    - Only use client_company_view when you need client-specific fields (name, email, member_id, cam, etc.) WITHOUT any sales aggregation.
    - If you need both client info AND sales totals in one result, use a subquery: calculate sales from company_details+company_sales first, then join the result to client info.

    Example — companies with sales > 10M in 2026:
    SELECT cd.company_name, SUM(cs.amount) AS total_sales
    FROM company_details cd
    JOIN company_sales cs ON cd.company_id = cs.company_id
    WHERE cs.type = 'actual' AND cs.year = 2026
    GROUP BY cd.company_id, cd.company_name
    HAVING SUM(cs.amount) > 10000000
    ORDER BY total_sales DESC

    Example — clients from companies with sales > 10M:
    SELECT ccv.client_name, ccv.company_name, ccv.membership_tier, SUM(cs.amount) AS total_sales
    FROM client_company_view ccv
    JOIN company_sales cs ON ccv.company_id = cs.company_id
    WHERE cs.type = 'actual' AND cs.year = 2026
    GROUP BY ccv.member_id, ccv.client_name, ccv.company_name, ccv.membership_tier
    HAVING SUM(cs.amount) > 10000000
    ORDER BY total_sales DESC

    Example — count clients by membership tier:
    SELECT membership_tier, COUNT(*) AS tier_count
    FROM client_company_view
    GROUP BY membership_tier
    ORDER BY tier_count DESC

    Example — clients who left today:
    SELECT client_name, company_name, membership_tier, moved_at
    FROM ex_client_company_view
    WHERE moved_at::date = CURRENT_DATE
    ORDER BY moved_at DESC

    IMPORTANT: Never reference computed aliases (like tier_count, total_sales) as column names — only use real columns from the schema above. Always compute aggregations with COUNT(*), SUM(), etc.
    IMPORTANT: Never add LIMIT unless the user explicitly asks for a specific number of results. Always return all matching rows.
    IMPORTANT: company_sales.type has values: 'actual', 'target', 'min_target', 'total', 'ytd'.
    Always filter WHERE type = 'actual' unless the user explicitly asks for targets or other types."""
    results = db.execute_sql(query.rstrip("; \n"))
    return json.dumps(results, default=str)


@tool
def update_client_sales(
    year: int,
    month: int,
    amount: float,
    name: Optional[str] = None,
    member_id: Optional[str] = None,
) -> str:
    """Update a client's sales record in the CRM system.
    Use when the user wants to update, add, or record sales for a specific client.
    Always look up the client in the database first to get both member_id and full name.
    month should be a number (1-12). Only 2026 is allowed — reject any other year."""
    if year != 2026:
        return "Sales updates are only allowed for 2026."

    webhook_url = os.environ.get("CRM_WEBHOOK_URL")
    if not webhook_url:
        return "CRM webhook is not configured."

    if not member_id and not name:
        return "Please provide either a member ID or client name."

    # Look up client by name or member_id
    if name and not member_id:
        results = db.search_clients(name=name, limit=1)
        if not results:
            return f"No client found with name '{name}'. Please check the name and try again."
        member_id = results[0].get("member_id")
        name = results[0].get("name")
    elif member_id:
        rows = db.execute_sql(
            f"SELECT member_id, name FROM client_details WHERE member_id ILIKE '%{member_id}%' LIMIT 1"
        )
        if rows and isinstance(rows, list):
            member_id = rows[0].get("member_id", member_id)
            name = rows[0].get("name", name)

    # Fetch full year's sales for the client's company
    sales = []
    company_rows = db.execute_sql(
        f"SELECT cs.month, cs.amount FROM company_sales cs "
        f"JOIN client_details cl ON cl.company_id = cs.company_id "
        f"WHERE cl.member_id = '{member_id}' AND cs.year = {year} AND cs.type = 'actual' "
        f"ORDER BY cs.month"
    )
    month_names = {
        1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "may", 6: "jun",
        7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec"
    }

    # Build sales dict from existing data
    sales_dict = {}
    if company_rows and isinstance(company_rows, list):
        for r in company_rows:
            key = f"{month_names[r['month']]} {year}"
            sales_dict[key] = float(r["amount"] or 0)

    # Override with the updated figure
    sales_dict[f"{month_names[month]} {year}"] = amount

    payload = {
        "member_id": member_id,
        "name": name,
        "year": year,
        "month": month,
        "amount": amount,
        **sales_dict,
    }

    try:
        resp = httpx.post(webhook_url, json=payload, timeout=30)
        if resp.status_code in (200, 201, 202):
            return f"Sales updated successfully for {name} ({member_id}) — {month}/{year}: RM {amount:,.2f}"
        else:
            return f"CRM returned error {resp.status_code}: {resp.text}"
    except Exception as e:
        return f"Failed to reach CRM: {str(e)}"


# Pending files to be sent by bot.py after the agent responds
_pending_files: list = []


@tool
def generate_sales_slides(company_name: str, year: Optional[int] = None) -> str:
    """Generate a PowerPoint sales report for a company and send it to the user.
    Use when the user asks to create slides, a presentation, or a report for a company's sales."""
    path = slide_gen.create_sales_slides(company_name=company_name, year=year)
    _pending_files.append(path)
    return f"Sales presentation for {company_name} has been generated and will be sent to you now."


def _trigger_slide_script(env_key: str, chat_id: int, thread_id: int = None, extra: dict = None) -> str:
    """Helper to call a Google Apps Script Web App and return status."""
    import logging
    logging.getLogger(__name__).info("_trigger_slide_script: chat_id=%s thread_id=%s extra=%s", chat_id, thread_id, extra)
    url = os.environ.get(env_key)
    if not url:
        return f"Slide script is not configured ({env_key})."
    if not chat_id:
        return "Chat ID not available — cannot trigger slide script."
    params = {"chat_id": chat_id}
    if thread_id:
        params["thread_id"] = thread_id
    if extra:
        params.update(extra)
    try:
        resp = httpx.get(url, params=params, follow_redirects=True, timeout=60)
        data = resp.json()
        if data.get("error"):
            return f"Script error: {data['error']}"
        return "ok"
    except Exception as e:
        return f"Failed to reach script: {str(e)}"


# Chat context for slide tools — set by bot.py before invoking agent
_current_chat_id: int = None
_current_thread_id: int = None


@tool
def generate_top100_sales_slide() -> str:
    """Generate the Top 100 companies by sales slide and send it to the chat.
    Use when the user asks for the top 100 sales slide or ranking presentation."""
    result = _trigger_slide_script("TOP100_SLIDES_URL", _current_chat_id, _current_thread_id)
    if result == "ok":
        return "Top 100 Sales slide is being generated and will be sent here when ready."
    return result


@tool
def generate_company_sales_slide(company_name: str) -> str:
    """Generate an individual sales slide for a specific company and send it to the chat.
    Use when the user asks for a sales slide, sales report, or sales presentation for a specific company (no chart)."""
    result = _trigger_slide_script("COMPANY_SALES_SLIDE_URL", _current_chat_id, _current_thread_id, {"company_name": company_name})
    if result == "ok":
        return f"Sales slide for {company_name} is being generated and will be sent here when ready."
    return result


@tool
def generate_company_sales_chart_slide(company_name: str) -> str:
    """Generate an individual sales slide WITH a chart for a specific company and send it to the chat.
    Use when the user asks for a sales slide with chart, or a sales and chart presentation for a specific company."""
    result = _trigger_slide_script("COMPANY_SALES_CHART_SLIDE_URL", _current_chat_id, _current_thread_id, {"company_name": company_name})
    if result == "ok":
        return f"Sales and chart slide for {company_name} is being generated and will be sent here when ready."
    return result


@tool
def generate_company_chart_slide(company_name: str) -> str:
    """Generate a chart-only slide for a specific company and send it to the chat.
    Use when the user asks for a chart slide or chart presentation for a specific company (no sales table)."""
    result = _trigger_slide_script("COMPANY_CHART_SLIDE_URL", _current_chat_id, _current_thread_id, {"company_name": company_name})
    if result == "ok":
        return f"Chart slide for {company_name} is being generated and will be sent here when ready."
    return result


@tool
def generate_top100_chart_slide() -> str:
    """Generate the Top 100 companies chart slide and send it to the chat.
    Use when the user asks for the top 100 chart slide or top 100 chart presentation."""
    result = _trigger_slide_script("TOP100_CHART_SLIDE_URL", _current_chat_id, _current_thread_id)
    if result == "ok":
        return "Top 100 Chart slide is being generated and will be sent here when ready."
    return result


TOOLS = [
    semantic_search,
    search_clients,
    get_company_details,
    get_sales_summary,
    list_companies,
    get_top_companies_by_sales,
    get_expiring_memberships,
    execute_sql,
    generate_sales_slides,
    update_client_sales,
    generate_top100_sales_slide,
    generate_company_sales_slide,
    generate_company_sales_chart_slide,
    generate_company_chart_slide,
    generate_top100_chart_slide,
]

SYSTEM_PROMPT = """You are a helpful assistant for Titan Database — a CRM containing client and company records.

Always use tools to fetch real data before answering. Never guess or fabricate records.

Tool selection guidelines:
- Use semantic_search for fuzzy or descriptive queries — e.g. "find companies similar to vfive", "wellness companies near KL".
  For "find similar to X": first call get_company_details to get X's attributes, then build a descriptive query from those for semantic_search.
- Use execute_sql for structured/precise queries — lists, counts, rankings, groupings. e.g. "list all CAMs", "top 5 sales", "count clients per company".
- Use dedicated tools (search_clients, get_company_details, etc.) when they directly match the query.

Name matching rules:
- Users often use nicknames, short names, or partial names (e.g. "dr sam" instead of "Dr Samhan Clinic", "iyziq" instead of "Iyziq Sdn Bhd"). Always attempt partial matching — all database tools support partial name search.
- If a lookup returns no results, do NOT give up. Try semantic_search with the name or description to find the closest match, then retry the original query using the correct full name.
- Be flexible and infer what the user means — "vfive's clients" means search clients by company_name="vfive"; "sales for iyziq" means get_sales_summary with company_name="iyziq".

Other rules:
- Be concise but complete. Use bullet points or tables when listing multiple records.
- This is a Telegram bot. Use standard markdown: **bold** for headers/labels, *italic* for emphasis, `code` for values. Never use LaTeX math notation. Write all formulas in plain text. Example: Growth = (New - Old) / Old x 100.
- Dates are in YYYY-MM-DD format in the database.
- Always display sales amounts with the RM prefix (e.g. RM1,234.56), never use "MYR".
- Sales data has types: 'actual', 'target', 'min_target', 'total', 'ytd'. Always use type = 'actual' for real sales figures.
- For sales queries spanning a month range (e.g. "Jan to Jun 2025"), always use execute_sql with a BETWEEN or >= / <= filter on year and month — never call get_sales_summary multiple times.
- For any query involving calculations (growth %, comparisons between periods, YTD), always use execute_sql — it is more powerful and flexible than dedicated tools.
- When querying multiple companies at once, use execute_sql with ILIKE conditions joined by OR.
- The database contains sales data from 2016 to 2026. Do NOT add any year filter unless the user explicitly asks for a specific year or period. Always query all available years by default.
- If the user asks about the latest available sales data or what period the data covers, run: SELECT year, month FROM company_sales WHERE type = 'actual' AND amount > 0 ORDER BY year DESC, month DESC LIMIT 1
- NEVER say "no data available" without first trying execute_sql as a fallback. If a dedicated tool returns nothing, always retry with execute_sql using ILIKE for partial name matching before giving up.
- If no results are found after all retries, say so clearly.
- Member ID prefixes indicate membership tier: IDs starting with "TCP" are Titan Circle Plus (TCP) members — the higher-value tier. IDs starting with "T" (but not TCP) are Titanium Circle / Titan members — the standard tier."""

_agent_executor = None


def _get_executor() -> AgentExecutor:
    global _agent_executor
    if _agent_executor is None:
        llm = ChatOpenAI(model="gpt-4o", temperature=0, streaming=True)
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])
        agent = create_openai_tools_agent(llm, TOOLS, prompt)
        _agent_executor = AgentExecutor(agent=agent, tools=TOOLS, verbose=False)
    return _agent_executor


def _to_lc_history(history) -> list:
    msgs = []
    for msg in (history or []):
        if msg["role"] == "user":
            msgs.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            msgs.append(AIMessage(content=msg["content"]))
    return msgs


def ask(user_message: str, history=None) -> tuple:
    result = _get_executor().invoke({
        "input": user_message,
        "chat_history": _to_lc_history(history),
    })
    reply = result["output"]
    new_history = (history or []) + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": reply},
    ]
    return reply, new_history


async def ask_stream(user_message: str, history=None):
    """Async generator yielding text chunks of the final response."""
    async for event in _get_executor().astream_events(
        {"input": user_message, "chat_history": _to_lc_history(history)},
        version="v2",
    ):
        if event["event"] == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            if chunk.content and not getattr(chunk, "tool_call_chunks", None):
                yield chunk.content
