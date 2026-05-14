import json
import os
from openai import OpenAI
import database as db

_openai = None


def get_openai() -> OpenAI:
    global _openai
    if _openai is None:
        _openai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai


def embed(text: str) -> list[float]:
    resp = get_openai().embeddings.create(
        model="text-embedding-3-small",
        input=text,
    )
    return resp.data[0].embedding


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": (
                "Search client and company records using natural language. "
                "Use this for vague or descriptive queries like 'health companies in KL' "
                "or 'find someone named Ahmad'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "limit": {"type": "integer", "description": "Max results to return (default 5)", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_clients",
            "description": (
                "Search clients by name, company, CAM (account manager), or membership expiry date. "
                "Use ISO date format YYYY-MM-DD for dates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Client name (partial match)"},
                    "company_name": {"type": "string", "description": "Company name (partial match)"},
                    "cam": {"type": "string", "description": "Client Account Manager name"},
                    "expiry_before": {"type": "string", "description": "Filter memberships expiring before this date (YYYY-MM-DD)"},
                    "expiry_after": {"type": "string", "description": "Filter memberships expiring after this date (YYYY-MM-DD)"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_details",
            "description": "Get full details of a company by name, including industry, location, brand, and business type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string", "description": "Company name (partial match)"},
                },
                "required": ["company_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sales_summary",
            "description": "Get sales data for a company, optionally filtered by year and/or month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string", "description": "Company name"},
                    "year": {"type": "integer", "description": "Filter by year (e.g. 2024)"},
                    "month": {"type": "integer", "description": "Filter by month (1-12)"},
                },
                "required": ["company_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_companies",
            "description": "List companies filtered by industry, location, or partner status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "industry": {"type": "string", "description": "Industry type (e.g. 'Health & Wellness', 'Fashion')"},
                    "location": {"type": "string", "description": "Location or state"},
                    "partner_in_circle": {"type": "string", "description": "Partner in circle status"},
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_companies_by_sales",
            "description": "Get companies ranked by total sales amount, highest first. Use for queries like 'top companies by sales', 'best performing companies', 'highest revenue companies'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of companies to return (default 5)"},
                    "year": {"type": "integer", "description": "Filter by year (e.g. 2024)"},
                    "month": {"type": "integer", "description": "Filter by month (1-12)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_expiring_memberships",
            "description": "Get clients whose memberships are expiring within a number of days from today.",
            "parameters": {
                "type": "object",
                "properties": {
                    "within_days": {"type": "integer", "description": "Number of days from today (default 30)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": (
                "Run a read-only SELECT query directly against the database. "
                "Use this for any query the other tools cannot handle — distinct lists, counts, aggregations, groupings, rankings, joins, etc. "
                "Tables: client_details, company_details, company_sales. "
                "Key columns — client_details: member_id, name, company_id, cam, email, phone, ic_no, start_date, expiry_date, pax. "
                "company_details: company_id, company_name, brand, industry, nature_of_business, location, total_duration, made_in_spire, register_no, entiti_bisnes, partner_in_circle. "
                "company_sales: company_id, year, month, type, amount. "
                "IMPORTANT: company_sales.type has values: 'actual', 'target', 'min_target', 'total', 'ytd'. "
                "Always filter WHERE type = 'actual' unless the user explicitly asks for targets or other types."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A valid PostgreSQL SELECT statement"},
                },
                "required": ["query"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are a helpful assistant for Titan Database — a CRM containing client and company records.

Always use tools to fetch real data before answering. Never guess or fabricate records.

Tool selection guidelines:
- Use semantic_search for fuzzy or descriptive queries where you don't know the exact record — e.g. "find companies similar to vfive", "wellness companies near KL", "clients that look like high-value members".
  - For "find similar to X" queries: first call get_company_details or search_clients to get X's attributes (industry, location, nature of business), then build a rich descriptive query from those attributes for semantic_search. Never search with just the name.
- Use execute_sql for any structured, precise, or aggregation query — lists, counts, rankings, groupings, filters by date/value. Examples: "list all CAMs", "top 5 sales", "count clients per company".
- Use the dedicated tools (search_clients, get_company_details, get_sales_summary, list_companies, get_expiring_memberships, get_top_companies_by_sales) as shortcuts when they directly match the query.

Other rules:
- Be concise but complete. Format responses clearly using bullet points or tables when listing multiple records.
- Dates are in YYYY-MM-DD format in the database.
- Amounts in sales data are in MYR unless stated otherwise.
- Sales data has multiple types: 'actual', 'target', 'min_target', 'total', 'ytd'. Always use type = 'actual' for real sales figures unless the user asks for targets.
- If no results are found, say so clearly.
"""


def _dispatch_tool(name: str, args: dict) -> str:
    if name == "semantic_search":
        embedding = embed(args["query"])
        results = db.semantic_search(embedding, limit=args.get("limit", 5))
        return json.dumps(results, default=str)

    if name == "search_clients":
        results = db.search_clients(
            name=args.get("name"),
            company_name=args.get("company_name"),
            cam=args.get("cam"),
            expiry_before=args.get("expiry_before"),
            expiry_after=args.get("expiry_after"),
            limit=args.get("limit", 10),
        )
        return json.dumps(results, default=str)

    if name == "get_company_details":
        results = db.get_company_details(args["company_name"])
        return json.dumps(results, default=str)

    if name == "get_sales_summary":
        results = db.get_sales_summary(
            company_name=args["company_name"],
            year=args.get("year"),
            month=args.get("month"),
        )
        return json.dumps(results, default=str)

    if name == "list_companies":
        results = db.list_companies(
            industry=args.get("industry"),
            location=args.get("location"),
            partner_in_circle=args.get("partner_in_circle"),
            limit=args.get("limit", 20),
        )
        return json.dumps(results, default=str)

    if name == "get_top_companies_by_sales":
        results = db.get_top_companies_by_sales(
            limit=args.get("limit", 5),
            year=args.get("year"),
            month=args.get("month"),
        )
        return json.dumps(results, default=str)

    if name == "get_expiring_memberships":
        results = db.get_expiring_memberships(within_days=args.get("within_days", 30))
        return json.dumps(results, default=str)

    if name == "execute_sql":
        results = db.execute_sql(args["query"])
        return json.dumps(results, default=str)

    return json.dumps({"error": f"Unknown tool: {name}"})


def ask_stream(user_message: str, history=None):
    """Generator yielding text chunks. StopIteration.value contains updated history."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    while True:
        response = get_openai().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append(msg)
            for call in msg.tool_calls:
                args = json.loads(call.function.arguments)
                result = _dispatch_tool(call.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
        else:
            stream = get_openai().chat.completions.create(
                model="gpt-4o",
                messages=messages,
                stream=True,
            )
            full_reply = ""
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full_reply += delta
                    yield delta
            new_history = (history or []) + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": full_reply},
            ]
            return new_history


def ask(user_message: str, history: list[dict] = None) -> tuple[str, list[dict]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    while True:
        response = get_openai().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append(msg)
            for call in msg.tool_calls:
                args = json.loads(call.function.arguments)
                result = _dispatch_tool(call.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
        else:
            reply = msg.content or "No response."
            new_history = (history or []) + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply},
            ]
            return reply, new_history
