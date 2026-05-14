# Titan RAG Bot

A Telegram bot that answers natural language questions about your CRM data using GPT-4o and Supabase pgvector.

## Features

- Semantic search across client and company records using vector embeddings
- Direct SQL execution for flexible queries (lists, counts, rankings, aggregations)
- Conversation memory per chat session
- Group chat support — only responds when mentioned (`@botusername`)
- Tools for clients, companies, sales data, and membership expiry

## Tech Stack

- **LLM:** GPT-4o (OpenAI)
- **Embeddings:** text-embedding-3-small (OpenAI)
- **Database:** Supabase (PostgreSQL + pgvector)
- **Bot framework:** python-telegram-bot

## Project Structure

```
titan-rag-bot/
├── bot.py               # Telegram bot — handles messages and conversation memory
├── agent.py             # GPT-4o agent with tool definitions and dispatch loop
├── database.py          # Supabase query functions
├── sync_embeddings.py   # Weekly script to re-embed all records into Supabase
└── requirements.txt
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

```env
SUPABASE_URL=your_supabase_project_url
SUPABASE_SERVICE_KEY=your_service_role_key
OPENAI_API_KEY=your_openai_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
```

3. Run the bot:

```bash
python bot.py
```

## Sync Embeddings

Run this whenever your CRM data changes to refresh the vector search index:

```bash
python sync_embeddings.py
```

Can also be scheduled via cron:

```
0 2 * * 1 cd ~/titan-rag-bot && python sync_embeddings.py
```

## Database Tables

| Table | Description |
|-------|-------------|
| `client_details` | Client records (name, email, phone, CAM, membership dates) |
| `company_details` | Company records (name, industry, location, brand) |
| `company_sales` | Monthly sales data per company |
| `embeddings` | pgvector embeddings for semantic search |

## Usage

In private chat, just send any message. In a group, mention the bot:

```
@botusername list all CAMs
@botusername top 5 companies by sales in 2024
@botusername which memberships expire this month?
```

Use `/clear` to reset conversation history.
