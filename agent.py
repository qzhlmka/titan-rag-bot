import json
import os
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
    """Get actual sales data for a specific company, optionally filtered by year and/or month."""
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

    Tables: client_details, company_details, company_sales.
    Key columns:
    - client_details: member_id, name, company_id, cam, email, phone, ic_no, start_date, expiry_date, pax
    - company_details: company_id, company_name, brand, industry, nature_of_business, location, total_duration, made_in_spire, register_no, entiti_bisnes, partner_in_circle
    - company_sales: company_id, year, month, type, amount

    IMPORTANT: company_sales.type has values: 'actual', 'target', 'min_target', 'total', 'ytd'.
    Always filter WHERE type = 'actual' unless the user explicitly asks for targets or other types."""
    results = db.execute_sql(query.rstrip("; \n"))
    return json.dumps(results, default=str)


# Pending files to be sent by bot.py after the agent responds
_pending_files: list = []


@tool
def generate_sales_slides(company_name: str, year: Optional[int] = None) -> str:
    """Generate a PowerPoint sales report for a company and send it to the user.
    Use when the user asks to create slides, a presentation, or a report for a company's sales."""
    path = slide_gen.create_sales_slides(company_name=company_name, year=year)
    _pending_files.append(path)
    return f"Sales presentation for {company_name} has been generated and will be sent to you now."


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
- This is a Telegram bot. Never use LaTeX math notation (no \[...\] or \(...\)). Write all formulas and calculations in plain text. Example: Growth = (New - Old) / Old x 100.
- Dates are in YYYY-MM-DD format in the database.
- Amounts are in MYR unless stated otherwise.
- Sales data has types: 'actual', 'target', 'min_target', 'total', 'ytd'. Always use type = 'actual' for real sales figures.
- The database contains sales data from 2016 to 2026. Do NOT add any year filter unless the user explicitly asks for a specific year or period. Always query all available years by default.
- If no results are found, say so clearly.
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
