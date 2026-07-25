# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys, see "Key environment variables" below
```

## Running the Bot

```bash
# Start (foreground)
python bot.py

# Start with persistent log
nohup python bot.py >> bot.log 2>&1 &

# Watch logs live
tail -f bot.log
```

There are no tests, no linter config, and no build step. The entire runtime is a single `python bot.py` process. `Procfile` (`worker: python bot.py`) + `runtime.txt` (Python 3.11.9) target a Heroku-style worker dyno for deployment.

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

## Refreshing embeddings

Run after any bulk CRM data change:

```bash
python sync_embeddings.py
```
