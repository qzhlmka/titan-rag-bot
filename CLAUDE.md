# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
# Start (foreground)
python bot.py

# Start with persistent log
nohup python bot.py >> bot.log 2>&1 &

# Watch logs live
tail -f bot.log
```

There are no tests, no linter config, and no build step. The entire runtime is a single `python bot.py` process.

## Architecture

This is a **Python Telegram Bot (PTB v21)** CRM assistant. The entry point is `bot.py` — all Telegram handlers, ConversationHandler state machines, and orchestration live there. The other modules are pure logic called via `asyncio.to_thread` for blocking I/O.

### Module responsibilities

| File | Role |
|------|------|
| `bot.py` | All PTB handlers, ConversationHandler state machines, `md_to_html` formatter |
| `agent.py` | LangChain AgentExecutor + tool definitions for freeform CRM queries |
| `coaching.py` | Transcript parsing, GPT summary generation, Make webhook for 1-on-1 sessions |
| `payment.py` | Receipt extraction (GPT vision), CRM client lookup (active + ex), Make webhook |
| `report.py` | Daily coaching report aggregation and Make webhook |
| `database.py` | All Supabase query functions — single Supabase client per thread via `threading.local` |
| `slides.py` | PowerPoint generation via external Google Apps Script URLs |
| `sync_embeddings.py` | Re-embeds all CRM records into the `embeddings` pgvector table |
| `import_payments.py` | One-time CSV importer for historical payments (`python import_payments.py payments.csv`) |
| `bank_accounts.json` | Maps bank account numbers to bank name/account name for payment flow |

### Conversation state machines

Three `ConversationHandler`s run in `bot.py`:

**Coaching (`/oneonone`)** — states `0–3` (`AI_CONVERSATION, ASK_TRANSCRIPT, ASK_PROOF_PHOTO, ASK_SESSION_DATE`):
1. Accept transcript (text/doc) → AI agent identifies client → ask to confirm date → require proof photo → generate GPT summary → fire Make webhook

**Daily report (`/dailyreport`)** — state `100` (`DAILYREPORT_DATE`):
1. Optional date prompt → fetch sessions from Supabase → aggregate → fire Make webhook

**Payment (`/payment`)** — states `200–205` (`PAYMENT_RECEIPT, PAYMENT_REMARK, PAYMENT_CONFIRM, PAYMENT_EDIT, PAYMENT_EDIT_VALUE, PAYMENT_CLIENT_SELECT`):
1. Accept receipt (photo/PDF) → GPT extracts fields → look up client in active + ex tables → show confirmation summary → Confirm/Edit → fire Make webhook

### Client lookup in payment flow

`payment.lookup_client_all()` searches both active (`client_details`) and ex-client (`ex_client_details`) tables, then merges semantic search results. Returns candidates tagged with `_source: "active"` or `_source: "ex"`. When `len(candidates) >= 2`, `bot.py` shows a disambiguation keyboard (🟢 Current / 🔴 Former) before proceeding.

`ex_client_details` has **no FK** to `ex_company_details` — company details are always fetched with a separate query and attached manually.

### Semantic search

Embeddings are stored in the `embeddings` table (pgvector). Embeddings are created per-client and per-company when active. When a client moves to ex tables, their embedding stays in place — so semantic search may surface ex-client `member_id`s. `lookup_client_all` handles this by checking the ex table when `_fetch_client_by_member_id` returns empty.

### Access control

`is_allowed(chat_id)` checks `allowed_chats` table with a 5-minute in-memory cache. All command handlers call this at entry. The bot uses the **service role key** which bypasses Supabase RLS — safe because the key never leaves the server.

### Message formatting

All Telegram replies use `ParseMode.HTML`. `md_to_html()` in `bot.py` converts GPT markdown output to Telegram-safe HTML (strips LaTeX, converts tables, balances unclosed tags). Long responses are split via `_split_html()` at natural paragraph boundaries.

### Blocking I/O in async handlers

All Supabase and OpenAI calls block — wrap them with `asyncio.to_thread(fn, *args)` in handlers. Callbacks from `CallbackQueryHandler` must use `update.effective_message` (not `update.message`, which is `None` in callback context).

## Key environment variables

| Variable | Purpose |
|----------|---------|
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Supabase project (service role bypasses RLS) |
| `OPENAI_API_KEY` | GPT + embeddings |
| `TELEGRAM_BOT_TOKEN` | PTB bot token |
| `COACHING_WEBHOOK_URL` | Make webhook for 1-on-1 summaries |
| `DAILY_REPORT_WEBHOOK_URL` | Make webhook for daily reports |
| `PAYMENT_WEBHOOK_URL` | Make webhook for payment receipts |
| `PAYMENT_NOTIFY_CHAT_ID` | Telegram group to notify on payment confirm |
| `PAYMENT_NOTIFY_THREAD_ID` | Topic thread ID for payment notifications (optional) |
| `N8N_WEBHOOK_URL` | n8n webhook for `/updatedatabase` command |
| `CRM_WEBHOOK_URL` | CRM webhook used by `update_client_sales` agent tool |
| `TOP100_SLIDES_URL` | Google Apps Script URL for Top 100 sales slide |
| `COMPANY_SALES_SLIDE_URL` | Google Apps Script URL for company sales slide |
| `COMPANY_SALES_CHART_SLIDE_URL` | Google Apps Script URL for company sales+chart slide |
| `COMPANY_CHART_SLIDE_URL` | Google Apps Script URL for company chart-only slide |
| `TOP100_CHART_SLIDE_URL` | Google Apps Script URL for Top 100 chart slide |
| `KMP_DUPLICATE_LIB_OK` | Set to `TRUE` to suppress OpenMP conflict from LangChain |

## Agent model

The LangChain `AgentExecutor` in `agent.py` uses `gpt-5.4-mini` (not GPT-4o). GPT-4o is used only for coaching transcript summaries (`coaching.py`) and receipt extraction (`payment.py`). The agent is a module-level singleton — it is lazily initialized on first `/ask` and then reused for the process lifetime.

Agent tools beyond standard CRM queries:
- `update_client_sales` — writes sales via `CRM_WEBHOOK_URL`; **2026-only** guard is in the tool itself
- `generate_top100_sales_slide`, `generate_company_sales_slide`, `generate_company_sales_chart_slide`, `generate_company_chart_slide`, `generate_top100_chart_slide` — all call the respective Google Apps Script URLs with the current `chat_id`/`thread_id`; these are set on module-level globals (`_current_chat_id`, `_current_thread_id`) by `bot.py` before each agent invocation

## Database tables

Beyond the tables in the execute_sql docstring, the following are used by the bot:

| Table | Purpose |
|-------|---------|
| `payments` | One row per payment — from bot or historical CSV import. Fields: `member_id`, `name`, `company_name`, `amount`, `payment_month`, `cam`, `reference`, `account_number`, `bank_name`, `account_name`, `remark`, `source` (`'bot'`/`'import'`), `created_at` |
| `coaching_sessions` | One row per completed 1-on-1. Fields: `session_date`, `client_name`, `business_name`, `member_id`, `cam`, `payload` (jsonb), `created_at` |
| `allowed_chats` | Whitelisted Telegram `chat_id` values |
| `embeddings` | pgvector embeddings for semantic search |

## Deployment

Deployed on **Railway** from the GitHub repo (`main` branch). Railway reads the `Procfile` (`worker: python bot.py`) and `runtime.txt` — it runs as a worker process (not a web service). No port is bound. Pushing to `main` redeploys.

## Refreshing embeddings

Run after any bulk CRM data change:

```bash
python sync_embeddings.py
```
