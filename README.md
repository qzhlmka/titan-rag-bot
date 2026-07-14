# Titan RAG Bot

A Telegram CRM assistant for RichWorks Consulting Group (Titan program). Handles one-on-one coaching session summaries, daily reports, payment receipt processing, and natural language CRM queries — all backed by GPT-4o, LangChain, and Supabase.

## Features

- **One-on-one coaching flow** — paste a transcript, bot identifies the client via CRM (with semantic search fallback for typos), confirms the session date, collects a mandatory proof-of-session photo, generates a structured Bahasa Malaysia summary, and fires it to Make
- **Daily coaching report** — aggregates all sessions for a given date and sends the report to Make
- **Payment receipt processing** — upload a receipt image, GPT extracts the fields, CRM lookup fills client details, and the confirmed payload is sent to Make (recorded to Google Sheets)
- **Natural language CRM queries** — ask anything about clients, companies, sales figures, expiring memberships
- **Semantic search** — vector embeddings (pgvector) power fuzzy client/company lookup
- **Access control** — only whitelisted Telegram chats can use the bot
- **Conversation memory** per chat session with `/clear` to reset
- **Group chat support** — responds when mentioned

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Greet and show available commands |
| `/menu` | Inline keyboard with all actions |
| `/oneonone` | Start a one-on-one coaching session summary flow |
| `/dailyreport [YYYY-MM-DD]` | Generate daily coaching report (defaults to today) |
| `/payment` | Submit a payment receipt |
| `/topsales` | View top-performing companies by sales |
| `/updatedatabase` | Sync and refresh the Supabase embeddings |
| `/clear` | Clear conversation history for this chat |

## Bot Flows

### `/oneonone` — One-on-One Coaching
1. User pastes or uploads a session transcript (text or document)
2. AI agent identifies the client from the CRM — uses partial name match, then semantic search fallback if there are typos
3. Bot extracts the session date (regex → GPT → today's date if none found) and asks the user to confirm
4. Bot requests a **mandatory proof-of-session photo** (keeps prompting until one is received; commands can still escape the flow)
5. Photo is uploaded to Supabase Storage and a public URL is attached to the payload
6. GPT-4o generates a structured summary (client overview, current state, challenges, root cause, guidance, action items with deadlines, CAM follow-up notes, AI insights) in Bahasa Malaysia
7. Payload (including `proofPhotoUrl` and `confirmedDate`) is sent to the **Make coaching webhook**
8. Formatted summary is sent back to Telegram; session is saved to Supabase for daily reporting

### `/dailyreport`
1. Fetches all coaching sessions for the target date from Supabase
2. Aggregates them into a report and sends to the **Make daily report webhook**
3. Formatted summary sent back to Telegram

### `/payment`
1. User uploads a receipt photo, image, or PDF document
2. GPT-4o extracts: client name, company, amount, installment month, transaction reference, recipient bank account
3. Bank account matched against `bank_accounts.json`
4. Client looked up in both active (`client_details`) and ex-client (`ex_client_details`) CRM tables — partial name match → company match → semantic search fallback
5. If multiple candidates are found across active and ex-client tables, bot shows a **disambiguation keyboard** (🟢 Current member / 🔴 Former member) and waits for selection
6. Bot shows a summary with **Confirm / Edit** inline buttons; any field can be corrected (editing client name re-triggers the CRM lookup and disambiguation if needed)
7. On confirm, payload sent to **Make payment webhook** and receipt uploaded to Supabase Storage
8. Notification posted to a configured Telegram group/topic

## Tech Stack

- **LLM:** GPT-4o (OpenAI)
- **Embeddings:** text-embedding-3-small (OpenAI)
- **Agent framework:** LangChain AgentExecutor
- **Database:** Supabase (PostgreSQL + pgvector)
- **Bot framework:** python-telegram-bot v21
- **Automation:** Make (Integromat) webhooks → Google Sheets / downstream flows

## Project Structure

```
titan-rag-bot/
├── bot.py               # Telegram bot — all handlers, conversation state machines
├── agent.py             # LangChain agent with CRM tool definitions
├── coaching.py          # Transcript processing, GPT summary, Make webhook
├── report.py            # Daily report aggregation and Make webhook
├── payment.py           # Receipt extraction, CRM lookup, semantic search
├── database.py          # Supabase query functions
├── slides.py            # PowerPoint report generation
├── sync_embeddings.py   # Re-embed all CRM records into Supabase pgvector
├── bank_accounts.json   # Bank account → bank name/account name lookup
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
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Service role key (Project Settings → API) |
| `OPENAI_API_KEY` | OpenAI API key |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `COACHING_WEBHOOK_URL` | Make webhook URL for one-on-one coaching summaries |
| `DAILY_REPORT_WEBHOOK_URL` | Make webhook URL for daily coaching reports |
| `PAYMENT_WEBHOOK_URL` | Make webhook URL for payment receipts |
| `PAYMENT_NOTIFY_CHAT_ID` | Telegram chat ID to notify on payment confirmation |
| `PAYMENT_NOTIFY_THREAD_ID` | Telegram topic thread ID for payment notifications (optional) |
| `N8N_WEBHOOK_URL` | n8n webhook URL (optional integration) |
| `CRM_WEBHOOK_URL` | CRM webhook URL (optional integration) |
| `TOP100_SLIDES_URL` | Script URL for top 100 sales slide |
| `COMPANY_SALES_SLIDE_URL` | Script URL for company sales slide |
| `KMP_DUPLICATE_LIB_OK` | Set to `TRUE` to suppress OpenMP conflict from LangChain |

## Sync Embeddings

Run this whenever CRM data changes to refresh the vector search index:

```bash
python sync_embeddings.py
```

Schedule via cron to run weekly:

```
0 2 * * 1 cd ~/titan-rag-bot && python sync_embeddings.py
```

## LangChain Tools

The AI agent automatically picks the right tool based on the query:

| Tool | Purpose |
|------|---------|
| `semantic_search` | Fuzzy/descriptive queries — "find companies similar to X" |
| `search_clients` | Search clients by name, company, CAM, expiry date |
| `get_company_details` | Full details for a company |
| `get_sales_summary` | Monthly actual sales for a company |
| `list_companies` | List companies filtered by industry/location |
| `get_top_companies_by_sales` | Rank companies by total actual sales |
| `get_expiring_memberships` | Clients with memberships expiring soon |
| `execute_sql` | Run any SELECT query for ad-hoc data needs |
| `generate_sales_slides` | Generate and send a PowerPoint sales report |

## Database Tables

| Table | Description |
|-------|-------------|
| `client_details` | Active client records (name, email, phone, CAM, membership dates, tier) |
| `company_details` | Active company records (name, industry, location, brand) |
| `ex_client_details` | Former client records (same structure as `client_details`) |
| `ex_company_details` | Former company records (same structure as `company_details`) |
| `company_sales` | Monthly sales per company (actual, target, min_target, ytd) |
| `embeddings` | pgvector embeddings for semantic search |
| `allowed_chats` | Whitelisted Telegram chat IDs |
| `coaching_sessions` | Saved one-on-one session records for daily reporting |

## Usage Examples

In private chat, send any message directly. In a group, mention the bot:

```
@botusername list all CAMs
@botusername top 5 companies by sales in 2025
@botusername which memberships expire this month?
@botusername find companies similar to Iyziq
```

Use `/clear` to reset conversation history.
