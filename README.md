# Titan RAG Bot

A Telegram bot that answers natural language questions about your CRM data using GPT-4o, LangChain, and Supabase pgvector. It can also generate PowerPoint sales presentations on demand.

## Features

- Natural language queries across client and company records
- Semantic search using vector embeddings (pgvector)
- Direct SQL execution for precise queries — lists, counts, rankings, aggregations
- Conversation memory per chat session
- Streaming responses in private chats (text appears word by word)
- Group chat support — only responds when mentioned (`@botusername`)
- PowerPoint sales report generation — sent directly as a file in chat
- Easily extensible with new LangChain tools

## Tech Stack

- **LLM:** GPT-4o (OpenAI)
- **Embeddings:** text-embedding-3-small (OpenAI)
- **Agent framework:** LangChain
- **Database:** Supabase (PostgreSQL + pgvector)
- **Bot framework:** python-telegram-bot
- **Slides:** python-pptx

## Project Structure

```
titan-rag-bot/
├── bot.py               # Telegram bot — message handling, streaming, file sending
├── agent.py             # LangChain agent with all tool definitions
├── database.py          # Supabase query functions
├── slides.py            # PowerPoint report generation
├── sync_embeddings.py   # Script to re-embed all records into Supabase
├── requirements.txt
├── .env.example
└── README.md
```

## Setup

1. Clone the repo and install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

3. Run the bot:

```bash
python bot.py
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Service role key (not anon key) — found in Project Settings → API |
| `OPENAI_API_KEY` | OpenAI API key |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `KMP_DUPLICATE_LIB_OK` | Set to `TRUE` to fix OpenMP conflict from LangChain dependencies |

## Sync Embeddings

Run this whenever your CRM data changes to refresh the vector search index:

```bash
python sync_embeddings.py
```

Schedule via cron to run weekly:

```
0 2 * * 1 cd ~/titan-rag-bot && python sync_embeddings.py
```

## Available Tools

The LangChain agent automatically picks the right tool based on the query:

| Tool | Purpose |
|------|---------|
| `semantic_search` | Fuzzy/descriptive queries — "find similar companies to X" |
| `search_clients` | Search clients by name, company, CAM, expiry date |
| `get_company_details` | Full details of a company |
| `get_sales_summary` | Monthly actual sales for a company |
| `list_companies` | List companies filtered by industry/location |
| `get_top_companies_by_sales` | Rank companies by total actual sales |
| `get_expiring_memberships` | Clients with memberships expiring soon |
| `execute_sql` | Run any SELECT query — for anything the above tools can't handle |
| `generate_sales_slides` | Generate and send a PowerPoint sales report |

## Database Tables

| Table | Description |
|-------|-------------|
| `client_details` | Client records (name, email, phone, CAM, membership dates) |
| `company_details` | Company records (name, industry, location, brand) |
| `company_sales` | Monthly sales data per company (types: actual, target, min_target, total, ytd) |
| `embeddings` | pgvector embeddings for semantic search |

## Usage Examples

In private chat, send any message directly. In a group, mention the bot:

```
@botusername list all CAMs
@botusername top 5 companies by sales in 2025
@botusername which memberships expire this month?
@botusername find companies similar to Iyziq
@botusername generate a sales slide for Maskhal 2025
```

Use `/clear` to reset conversation history.

## Adding New Tools

With LangChain, adding a tool is just a decorated function:

```python
@tool
def my_new_tool(param: str) -> str:
    """Description of what this tool does and when to use it."""
    # your logic here
    return json.dumps(result)
```

Then add it to the `TOOLS` list in `agent.py`.
