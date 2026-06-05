import asyncio
import logging
import os
import re
import httpx
from html import escape
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

load_dotenv()

import agent
import coaching
import database
import database as db

# Allowlist cache: {chat_id: bool}, refreshed every 5 minutes
_allowed_cache: dict = {}
_cache_time: float = 0
_CACHE_TTL = 300  # seconds


def is_allowed(chat_id: int) -> bool:
    global _allowed_cache, _cache_time
    import time
    if time.time() - _cache_time > _CACHE_TTL:
        try:
            rows = db.get_client().from_("allowed_chats").select("chat_id").execute().data or []
            _allowed_cache = {int(row["chat_id"]) for row in rows}
            _cache_time = time.time()
        except Exception as e:
            logger_tmp = logging.getLogger(__name__)
            logger_tmp.error("Failed to fetch allowed_chats: %s", e)
            return True  # fail open if Supabase unreachable
    result = not _allowed_cache or chat_id in _allowed_cache
    logging.getLogger(__name__).info("is_allowed: chat_id=%s cache=%s result=%s", chat_id, _allowed_cache, result)
    return result


def md_to_html(text: str) -> str:
    """Convert markdown formatting to Telegram HTML, safely escaping data."""
    # Strip LaTeX block delimiters \[...\] and \(...\)
    text = re.sub(r'\\\[(.+?)\\\]', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\\\((.+?)\\\)', r'\1', text, flags=re.DOTALL)
    # Strip $...$ and $$...$$ math delimiters
    text = re.sub(r'\$\$(.+?)\$\$', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\$(.+?)\$', r'\1', text)
    # Convert common LaTeX commands to plain text
    text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'(\1 / \2)', text)
    text = re.sub(r'\\times', 'x', text)
    text = re.sub(r'\\div', '/', text)
    text = re.sub(r'\\approx', '~=', text)
    text = re.sub(r'\\cdot', 'x', text)
    text = re.sub(r'\\left\(', '(', text)
    text = re.sub(r'\\right\)', ')', text)
    text = re.sub(r'\\left\[', '[', text)
    text = re.sub(r'\\right\]', ']', text)
    text = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', text)  # any remaining \cmd{x} → x
    text = re.sub(r'\\[a-zA-Z]+', '', text)                  # bare \cmd → remove
    # Escape HTML entities in raw content
    text = escape(text)
    # ### headings and ## headings → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # **bold**
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    # *italic* (single asterisks not already converted)
    text = re.sub(r'\*([^*\n]+?)\*', r'<i>\1</i>', text)
    # `code`
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    return text

# Per-chat conversation history: {chat_id: [messages]}
_histories: dict = {}
MAX_HISTORY = 20

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_draft_counter = 0


def _next_draft_id() -> int:
    global _draft_counter
    _draft_counter += 1
    return _draft_counter


async def _send_draft(token: str, chat_id: int, draft_id: int, text: str = "") -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{token}/sendMessageDraft",
            json={"chat_id": chat_id, "draft_id": draft_id, "text": text},
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hello! I'm the Titan Database assistant.\n\n"
        "Here's what I can help you with:\n\n"
        "🔍 *Client & Company Lookup*\n"
        "• Who are the clients from Iyziq?\n"
        "• Show me health & wellness companies in KL\n"
        "• Which memberships expire this month?\n\n"
        "📊 *Sales & Performance*\n"
        "• What are the 2025 sales for Sabella Luxe?\n"
        "• Update Iyziq sales for March to RM15,000\n"
        "• /topsales — generate a slide for the top 100 companies by sales\n\n"
        "📝 *Coaching Summary*\n"
        "• /oneonone — upload a transcript to generate a 1-on-1 session summary\n\n"
        "🛠 *Other Commands*\n"
        "• /clear — clear conversation history\n"
        "• /updatedatabase — sync the database\n\n"
        "_In a group, mention me with @username to chat._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def clear(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    _histories.pop(update.message.chat_id, None)
    await update.message.reply_text("Conversation cleared.")


async def updatedatabase(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.message.chat_id):
        return
    webhook_url = os.environ.get("N8N_WEBHOOK_URL")
    if not webhook_url:
        await update.message.reply_text("Database update is not configured.")
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(webhook_url, json={"chat_id": update.message.chat_id})
        if resp.status_code in (200, 202):
            await update.message.reply_text("Database update started. This usually takes around 7 minutes.")
        else:
            logger.error("n8n returned status %s: %s", resp.status_code, resp.text)
            await update.message.reply_text(f"Failed to start update (status {resp.status_code}). Please check n8n.")
    except Exception as e:
        logger.error("n8n webhook error: %s", e, exc_info=True)
        await update.message.reply_text("Failed to reach n8n. Please try again.")


async def topsales(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.message.chat_id):
        return
    script_url = os.environ.get("TOP100_SLIDES_URL")
    if not script_url:
        await update.message.reply_text("Top 100 slides is not configured.")
        return
    await update.message.reply_text("Generating Top 100 Sales slides, please wait. The slides will be sent here when ready...")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            params = {"chat_id": update.message.chat_id}
            if update.message.message_thread_id:
                params["thread_id"] = update.message.message_thread_id
            resp = await client.get(script_url, params=params, follow_redirects=True)
            data = resp.json()
            if data.get("error"):
                logger.error("Apps Script error: %s", data["error"])
                await update.message.reply_text(f"Script error: {data['error']}")
    except httpx.TimeoutException:
        pass  # Script is still running, it will send the file directly when done
    except Exception as e:
        logger.error("topsales error: %s", e, exc_info=True)
        await update.message.reply_text("Failed to reach the script. Please try again.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.message.chat_id):
        await update.message.reply_text("You do not have access to this bot. Please contact the administrator.")
        return
    user_input = update.message.text.strip()
    if not user_input:
        return

    is_group = update.message.chat.type in ("group", "supergroup")

    # In group chats, only respond when mentioned
    if is_group:
        bot_username = context.bot.username
        if f"@{bot_username}" not in user_input:
            return
        user_input = user_input.replace(f"@{bot_username}", "").strip()
        if not user_input:
            return

    chat_id = update.message.chat_id
    history = _histories.get(chat_id, [])
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    if is_group:
        # Groups: use typing indicator + send full message
        await update.message.chat.send_action(ChatAction.TYPING)
        try:
            agent._current_chat_id = chat_id
            agent._current_thread_id = update.message.message_thread_id
            answer, history = agent.ask(user_input, history)
            _histories[chat_id] = history[-MAX_HISTORY * 2:]
        except Exception as e:
            logger.error("Agent error: %s", e, exc_info=True)
            answer = "Sorry, something went wrong. Please try again."

        formatted = md_to_html(answer)
        for i in range(0, len(formatted), 4096):
            await update.message.reply_text(formatted[i:i + 4096], parse_mode=ParseMode.HTML)
        while agent._pending_files:
            file_path = agent._pending_files.pop(0)
            if os.path.exists(file_path):
                with open(file_path, "rb") as f:
                    await update.message.reply_document(f, filename=os.path.basename(file_path))
                os.remove(file_path)

    else:
        # Private chats: stream response via sendMessageDraft
        draft_id = _next_draft_id()
        await _send_draft(token, chat_id, draft_id, "Thinking...")  # show placeholder

        full_text = ""
        last_sent_len = 0
        error = None

        try:
            agent._current_chat_id = chat_id
            agent._current_thread_id = update.message.message_thread_id
            async for chunk in agent.ask_stream(user_input, history):
                full_text += chunk
                if len(full_text) - last_sent_len >= 40:
                    await _send_draft(token, chat_id, draft_id, full_text)
                    last_sent_len = len(full_text)
        except Exception as e:
            logger.error("Agent error: %s", e, exc_info=True)
            error = "Sorry, something went wrong. Please try again."

        if error:
            await update.message.reply_text(error)
            return

        new_history = (history or []) + [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": full_text},
        ]
        _histories[chat_id] = new_history[-MAX_HISTORY * 2:]
        answer = full_text or "No response."

        # Finalize — sendMessage persists the message, draft disappears
        formatted = md_to_html(answer)
        for i in range(0, len(formatted), 4096):
            await update.message.reply_text(formatted[i:i + 4096], parse_mode=ParseMode.HTML)

        # Send any files generated by tools
        while agent._pending_files:
            file_path = agent._pending_files.pop(0)
            if os.path.exists(file_path):
                with open(file_path, "rb") as f:
                    await update.message.reply_document(f, filename=os.path.basename(file_path))
                os.remove(file_path)


AI_CONVERSATION, ASK_TRANSCRIPT = range(2)

_IDENTIFICATION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_clients",
            "description": "Search for clients by name and/or company name (partial match).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Client name (partial ok)"},
                    "company_name": {"type": "string", "description": "Company/business name (partial ok)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": "Fuzzy/semantic search across all client and company records. Use this when search_clients returns no results or the name might be spelled differently.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name or description to search for"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_and_process",
            "description": "Call this once you have identified the correct client and the user has confirmed. Triggers transcript processing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {"type": "string", "description": "The member_id of the confirmed client record"},
                    "client_name": {"type": "string", "description": "Full name of the confirmed client"},
                    "business_name": {"type": "string", "description": "Business/company name"},
                },
                "required": ["client_name", "business_name"],
            },
        },
    },
]

_IDENTIFICATION_SYSTEM = """You are an assistant helping identify a coaching session client from a transcript.

Your job:
1. Read the transcript excerpt and try to identify the client's name and company.
2. Search using search_clients. If it returns no results, fall back to semantic_search with the name or company — it handles fuzzy/misspelled names.
3. Tell the user what you found — show their name, company, and member ID — and ask them to confirm.
4. If multiple matches, present them clearly and ask which one.
5. Once the user confirms, call confirm_and_process immediately.

If the transcript has no client info, ask the user who the session is for, then search using both tools if needed.

Be concise and conversational. Don't ask unnecessary questions."""


async def _extract_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extract transcript text from a document or text message."""
    if update.message.document:
        doc = update.message.document
        fname = (doc.file_name or "").lower()
        if not (fname.endswith(".txt") or fname.endswith(".docx") or fname.endswith(".pdf")):
            return None
        file = await context.bot.get_file(doc.file_id)
        content = await file.download_as_bytearray()
        if fname.endswith(".txt"):
            return content.decode("utf-8", errors="ignore")
        elif fname.endswith(".docx"):
            import io, docx
            doc_obj = docx.Document(io.BytesIO(bytes(content)))
            return "\n".join(p.text for p in doc_obj.paragraphs if p.text.strip())
        elif fname.endswith(".pdf"):
            import io, pdfplumber
            with pdfplumber.open(io.BytesIO(bytes(content))) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
    elif update.message.text:
        return update.message.text.strip()
    return None


async def coaching_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.message.chat_id):
        await update.message.reply_text("You do not have access to this bot. Please contact the administrator.")
        return ConversationHandler.END

    transcript = await _extract_transcript(update, context)
    if transcript:
        context.user_data["transcript"] = transcript
        context.user_data["ai_messages"] = []
        context.user_data["search_cache"] = {}
        await update.message.chat.send_action(ChatAction.TYPING)
        return await _run_identification_agent(update, context)
    return ConversationHandler.END


async def coaching_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.message.chat_id):
        await update.message.reply_text("You do not have access to this bot. Please contact the administrator.")
        return ConversationHandler.END
    await update.message.reply_text("Please send the transcript file (.txt, .docx, or .pdf) or paste the transcript text.")
    return ASK_TRANSCRIPT


async def received_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    transcript = await _extract_transcript(update, context)
    if not transcript:
        await update.message.reply_text("Could not read the transcript. Please send a .txt, .docx, or .pdf file, or paste the text.")
        return ASK_TRANSCRIPT
    context.user_data["transcript"] = transcript
    context.user_data["ai_messages"] = []
    context.user_data["search_cache"] = {}
    await update.message.chat.send_action(ChatAction.TYPING)
    return await _run_identification_agent(update, context)


async def ai_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    messages = context.user_data.setdefault("ai_messages", [])
    messages.append({"role": "user", "content": update.message.text.strip()})
    await update.message.chat.send_action(ChatAction.TYPING)
    return await _run_identification_agent(update, context)


async def _run_identification_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Run the AI identification loop until it sends a message or calls confirm_and_process."""
    import json as _json
    from openai import OpenAI

    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    transcript = context.user_data.get("transcript", "")
    messages = context.user_data.setdefault("ai_messages", [])
    search_cache: dict = context.user_data.setdefault("search_cache", {})

    system_msg = {"role": "system", "content": _IDENTIFICATION_SYSTEM + f"\n\nTranscript excerpt:\n{transcript[:2500]}"}

    while True:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[system_msg] + messages,
            tools=_IDENTIFICATION_TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []

        # Append assistant turn (preserve tool_calls for the message history)
        assistant_entry = {"role": "assistant", "content": msg.content or ""}
        if tool_calls:
            assistant_entry["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ]
        messages.append(assistant_entry)

        if not tool_calls:
            # Plain text reply — send to user and wait for their response
            await update.message.reply_text(msg.content or "...")
            return AI_CONVERSATION

        # Process tool calls
        confirmed = False
        for tc in tool_calls:
            args = _json.loads(tc.function.arguments)

            if tc.function.name == "search_clients":
                results = database.search_clients(
                    name=args.get("name", ""),
                    company_name=args.get("company_name", ""),
                    limit=5,
                )
                for r in results:
                    search_cache[r.get("member_id", "")] = r
                tool_result = _json.dumps(results)

            elif tc.function.name == "semantic_search":
                import agent as _agent
                tool_result = _agent.semantic_search.func(args.get("query", ""), limit=5)

            elif tc.function.name == "confirm_and_process":
                member_id = args.get("member_id", "")
                client_name = args["client_name"]
                business_name = args["business_name"]
                client_record = search_cache.get(member_id, {})

                await update.message.reply_text(f"Got it! Processing summary for {client_name} ({business_name})...")
                await update.message.chat.send_action(ChatAction.TYPING)
                try:
                    loop = asyncio.get_event_loop()
                    markdown, _ = await loop.run_in_executor(
                        None, coaching.process_transcript, transcript, update.message.chat_id, client_name, business_name, client_record, update.message.message_thread_id
                    )
                    formatted = md_to_html(markdown)
                    for i in range(0, len(formatted), 4096):
                        await update.message.reply_text(formatted[i:i + 4096], parse_mode=ParseMode.HTML)
                    await update.message.reply_text("Summary has been sent to Make successfully.")
                except Exception as e:
                    logger.error("Coaching transcript error: %s", e, exc_info=True)
                    await update.message.reply_text("Failed to process transcript. Please try again.")

                context.user_data.clear()
                confirmed = True
                tool_result = "Processing triggered."

            else:
                tool_result = "Unknown tool."

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_result})

        if confirmed:
            return ConversationHandler.END
        # Loop back to let the AI respond after tool results



async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused error: %s", context.error, exc_info=True)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start", "Start the bot"),
            BotCommand("oneonone", "Generate a 1-on-1 coaching session summary"),
            BotCommand("topsales", "View top performing companies by sales"),
            BotCommand("updatedatabase", "Sync and update the database"),
            BotCommand("clear", "Clear conversation history"),
        ])

    app = Application.builder().token(token).post_init(post_init).build()

    coaching_conv = ConversationHandler(
        entry_points=[
            CommandHandler("oneonone", coaching_command),
        ],
        states={
            ASK_TRANSCRIPT: [
                MessageHandler(filters.Document.ALL | filters.TEXT & ~filters.COMMAND, received_transcript)
            ],
            AI_CONVERSATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ai_conversation)
            ],
        },
        fallbacks=[],
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("updatedatabase", updatedatabase))
    app.add_handler(CommandHandler("topsales", topsales))
    app.add_handler(coaching_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
