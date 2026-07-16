import asyncio
import logging
import os
import re
import httpx
from html import escape
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction, ParseMode
from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeDefault
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler

load_dotenv()

import agent
import coaching
import database
import database as db
import payment
import report

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
            return chat_id in _allowed_cache  # fail closed — use last known cache
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
    # *italic* and _italic_ (only match at word boundaries to avoid company names with underscores)
    text = re.sub(r'\*([^*\n]+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<i>\1</i>', text)
    # `code`
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Markdown tables → clean Telegram list
    text = _convert_tables(text)
    # Balance unclosed tags
    text = _balance_html_tags(text)
    return text


def _balance_html_tags(text: str) -> str:
    """Close any unclosed <i>, <b>, <code> tags."""
    for tag in ['i', 'b', 'code']:
        opens = len(re.findall(f'<{tag}>', text))
        closes = len(re.findall(f'</{tag}>', text))
        if opens > closes:
            text += f'</{tag}>' * (opens - closes)
    return text


def _split_html(text: str, max_len: int = 4096) -> list:
    """Split HTML into Telegram-safe chunks at natural boundaries."""
    chunks = []
    while len(text) > max_len:
        # Prefer splitting at a section/paragraph boundary
        split_at = -1
        for sep in ['\n\n---\n\n', '\n\n', '\n']:
            pos = text.rfind(sep, 0, max_len)
            if pos > max_len // 3:
                split_at = pos + len(sep)
                break
        if split_at == -1:
            # Last resort: walk back to avoid cutting inside a tag
            split_at = max_len
            last_open = text.rfind('<', 0, split_at)
            last_close = text.rfind('>', 0, split_at)
            if last_open > last_close:
                split_at = last_open
        chunks.append(_balance_html_tags(text[:split_at]))
        # Strip orphaned closing tags at the start of the remainder
        text = re.sub(r'^(\s*</(b|i|code)>\s*)+', '', text[split_at:])
    if text:
        chunks.append(_balance_html_tags(text))
    return chunks


def _convert_tables(text: str) -> str:
    """Convert markdown tables to a readable Telegram-friendly format."""
    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect table header row (contains |)
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|[\s\-|:]+\|\s*$", lines[i + 1]):
            headers = [h.strip() for h in line.strip().strip("|").split("|")]
            i += 2  # skip separator row
            rows = []
            while i < len(lines) and "|" in lines[i]:
                cols = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cols)
                i += 1
            # Format: if 2 columns, render as "🔹 <b>col1</b> — col2"
            if len(headers) == 2:
                for row in rows:
                    c1 = row[0] if len(row) > 0 else ""
                    c2 = row[1] if len(row) > 1 else ""
                    result.append(f"🔹 <b>{c1}</b>\n    {c2}")
            else:
                # Multi-column: render as indented block per row
                for row in rows:
                    parts = [f"<b>{headers[j]}</b>: {row[j]}" for j in range(min(len(headers), len(row)))]
                    result.append("• " + " | ".join(parts))
        else:
            result.append(line)
            i += 1
    return "\n".join(result)

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
        "Tap /menu to see all actions as buttons, or use commands directly:\n\n"
        "🔍 *Client & Company Lookup*\n"
        "• Who are the clients from Iyziq?\n"
        "• Show me health & wellness companies in KL\n"
        "• Which memberships expire this month?\n\n"
        "📊 *Sales & Performance*\n"
        "• What are the 2025 sales for Sabella Luxe?\n"
        "• Update Iyziq sales for March to RM15,000\n"
        "• /topsales — generate a slide for the top 100 companies by sales\n\n"
        "📝 *Coaching Summary*\n"
        "• /oneonone — upload a transcript to generate a 1-on-1 session summary\n"
        "• /dailyreport [YYYY-MM-DD] — generate executive summary of coaching sessions (defaults to today)\n\n"
        "💳 *Payments*\n"
        "• /payment — upload a receipt to extract and submit payment details to Google Sheet\n\n"
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


DAILYREPORT_DATE = 100


async def dailyreport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.message.chat_id):
        await update.message.reply_text("You do not have access to this bot. Please contact the administrator.")
        return ConversationHandler.END
    _cancel_other_conversations(update)
    if context.args:
        return await _run_dailyreport(update, context.args[0])
    await update.message.reply_text(
        "Which date would you like the report for?\nReply with a date (YYYY-MM-DD) or type *today*.",
        parse_mode=ParseMode.MARKDOWN
    )
    return DAILYREPORT_DATE


async def dailyreport_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from datetime import datetime
    text = update.message.text.strip()
    if text.lower() == "today":
        return await _run_dailyreport(update, None)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(
            "Invalid format. Please reply with a date in *YYYY-MM-DD* format (e.g. 2026-06-09) or type *today*.",
            parse_mode=ParseMode.MARKDOWN
        )
        return DAILYREPORT_DATE
    return await _run_dailyreport(update, text)


async def _send_markdown_summary(msg_obj, text, success_msg: str, error_msg: str) -> None:
    """Best-effort send of a markdown summary as HTML with plain-text fallback. Never raises."""
    if not text:
        try:
            await msg_obj.reply_text(error_msg)
        except Exception:
            pass
        return
    sent_ok = False
    try:
        formatted = md_to_html(text)
        for chunk in _split_html(formatted):
            await msg_obj.reply_text(chunk, parse_mode=ParseMode.HTML)
        sent_ok = True
    except Exception as e:
        logger.error("Failed to send summary to Telegram: %s", e, exc_info=True)
        try:
            await msg_obj.reply_text(text[:4000])
        except Exception:
            pass
    try:
        await msg_obj.reply_text(success_msg if sent_ok else error_msg)
    except Exception:
        pass


async def _run_dailyreport(update: Update, target_date: str) -> int:
    await update.message.reply_text(f"Generating daily coaching report for {target_date or 'today'}, please wait...")
    await update.message.chat.send_action(ChatAction.TYPING)
    chat_id = update.message.chat_id
    thread_id = update.message.message_thread_id
    try:
        content, count = await asyncio.to_thread(
            report.generate_daily_report, target_date, chat_id, thread_id
        )
    except Exception as e:
        logger.error("Daily report error: %s", e, exc_info=True)
        await update.message.reply_text("Failed to generate report. Please try again.")
        return ConversationHandler.END

    if count == 0:
        try:
            await update.message.reply_text(content)
        except Exception:
            pass
        return ConversationHandler.END

    # Make has already been notified at this point. Send the preview (best-effort).
    await _send_markdown_summary(
        update.message,
        content,
        "Daily report has been sent to Make successfully.",
        "Report was sent to Make successfully, but could not display the preview here.",
    )
    return ConversationHandler.END


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
        for chunk in _split_html(formatted):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
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
        for chunk in _split_html(formatted):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)

        # Send any files generated by tools
        while agent._pending_files:
            file_path = agent._pending_files.pop(0)
            if os.path.exists(file_path):
                with open(file_path, "rb") as f:
                    await update.message.reply_document(f, filename=os.path.basename(file_path))
                os.remove(file_path)


AI_CONVERSATION, ASK_TRANSCRIPT, ASK_PROOF_PHOTO, ASK_SESSION_DATE = range(4)
PAYMENT_RECEIPT, PAYMENT_REMARK, PAYMENT_CONFIRM, PAYMENT_EDIT, PAYMENT_EDIT_VALUE, PAYMENT_CLIENT_SELECT = 200, 201, 202, 203, 204, 205

# Module-level references to ConversationHandlers, populated in main().
# Used so entry points can kill sibling conversations before starting a new one.
_coaching_conv: "ConversationHandler | None" = None
_payment_conv: "ConversationHandler | None" = None
_dailyreport_conv: "ConversationHandler | None" = None


def _cancel_other_conversations(update: Update) -> None:
    """Force-end every active conversation for this user/chat.

    Call this at the top of every command entry point so that starting
    /payment while mid-coaching (or vice-versa) doesn't leave a zombie
    conversation that keeps intercepting the user's next messages.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    key = (chat_id, user_id) if user_id else (chat_id,)
    for conv in (_coaching_conv, _payment_conv, _dailyreport_conv):
        if conv is not None:
            conv._conversations.pop(key, None)

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
            "description": "Call this once you have identified the correct client(s) and the user has confirmed. Supports one or two clients for joint sessions under the same company.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {"type": "string", "description": "The member_id of the first confirmed client"},
                    "client_name": {"type": "string", "description": "Full name of the first confirmed client"},
                    "business_name": {"type": "string", "description": "Business/company name"},
                    "member_id_2": {"type": "string", "description": "The member_id of the second client (joint session only)"},
                    "client_name_2": {"type": "string", "description": "Full name of the second client (joint session only)"},
                },
                "required": ["client_name", "business_name"],
            },
        },
    },
]

_IDENTIFICATION_SYSTEM = """You are an assistant helping identify coaching session client(s) from a transcript.

Your job:
1. Read the transcript excerpt and try to identify the client name(s) and company.
2. If TWO clients appear in the session (joint session, same company), identify both.
3. Search using search_clients for each client. If no results, fall back to semantic_search.
4. Tell the user what you found — show name, company, and member ID — and ask them to confirm.
5. If multiple matches, present them clearly and ask which one.
6. Once the user confirms, call confirm_and_process immediately. For joint sessions, include both client_name and client_name_2.

If the transcript has no client info, ask the user who the session is for.

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
    _cancel_other_conversations(update)
    context.user_data.pop("transcript_parts", None)
    context.user_data.pop("transcript", None)
    context.user_data.pop("ai_messages", None)
    context.user_data.pop("search_cache", None)
    await update.message.reply_text(
        "Please send the transcript file (.txt, .docx, or .pdf) or paste the transcript text.\n\n"
        "If pasting text in multiple parts, send each part and tap ▶️ Process Now when finished."
    )
    return ASK_TRANSCRIPT


async def received_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # File uploads: process immediately (entire content arrives at once)
    if update.message.document:
        transcript = await _extract_transcript(update, context)
        if not transcript:
            await update.message.reply_text("Could not read the transcript. Please send a .txt, .docx, or .pdf file, or paste the text.")
            return ASK_TRANSCRIPT
        context.user_data["transcript"] = transcript
        context.user_data["ai_messages"] = []
        context.user_data["search_cache"] = {}
        await update.message.chat.send_action(ChatAction.TYPING)
        return await _run_identification_agent(update, context)

    # Text paste: accumulate parts, show Process button after each
    part = (update.message.text or "").strip()
    if not part:
        return ASK_TRANSCRIPT
    parts = context.user_data.setdefault("transcript_parts", [])
    parts.append(part)
    total_chars = sum(len(p) for p in parts)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Process Now", callback_data="transcript_process")]])
    await update.message.reply_text(
        f"✅ Part {len(parts)} received ({total_chars:,} characters total).\n"
        "Paste more text or tap Process Now when finished.",
        reply_markup=kb,
    )
    return ASK_TRANSCRIPT


async def transcript_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User taps Process Now — join all accumulated parts and run the agent."""
    query = update.callback_query
    await query.answer()
    parts = context.user_data.pop("transcript_parts", [])
    if not parts:
        await query.message.reply_text("No transcript received yet. Please paste the transcript first.")
        return ASK_TRANSCRIPT
    transcript = "\n\n".join(parts)
    context.user_data["transcript"] = transcript
    context.user_data["ai_messages"] = []
    context.user_data["search_cache"] = {}
    await query.edit_message_text(f"Processing {len(transcript):,} characters...")
    await query.message.chat.send_action(ChatAction.TYPING)
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

    # Works for both regular messages and callback query updates
    msg_obj = update.effective_message

    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    transcript = context.user_data.get("transcript", "")
    messages = context.user_data.setdefault("ai_messages", [])
    search_cache: dict = context.user_data.setdefault("search_cache", {})

    system_msg = {"role": "system", "content": _IDENTIFICATION_SYSTEM + f"\n\nTranscript excerpt:\n{transcript[:2500]}"}

    for _iteration in range(10):
        response = openai_client.chat.completions.create(
            model="gpt-5.4",
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
            for chunk in _split_html(md_to_html(msg.content or "...")):
                await msg_obj.reply_text(chunk, parse_mode=ParseMode.HTML)
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
                member_id_2 = args.get("member_id_2", "")
                client_name_2 = args.get("client_name_2")
                client_record_2 = search_cache.get(member_id_2, {}) if member_id_2 else {}

                label = f"{client_name} & {client_name_2}" if client_name_2 else client_name

                # Extract date — regex first, GPT fallback
                extracted_date = coaching._parse_date_from_text(transcript)
                if not extracted_date:
                    from openai import OpenAI as _OAI
                    from datetime import date as _dt
                    _oc = _OAI(api_key=os.environ["OPENAI_API_KEY"])
                    _r = _oc.chat.completions.create(
                        model="gpt-5.4",
                        messages=[
                            {"role": "system", "content": f"Today is {_dt.today().isoformat()}. Extract the session date from this coaching transcript. Convert it to YYYY-MM-DD. Return ONLY a JSON object with key: date. If no date is found, return {{\"date\": \"\"}}."},
                            {"role": "user", "content": transcript[:1000]},
                        ],
                        response_format={"type": "json_object"},
                    )
                    import json as _json2
                    extracted_date = _json2.loads(_r.choices[0].message.content).get("date", "")

                context.user_data["pending_coaching"] = {
                    "client_name": client_name,
                    "client_name_2": client_name_2,
                    "business_name": business_name,
                    "client_record": client_record,
                    "client_record_2": client_record_2,
                    "transcript": transcript,
                    "label": label,
                    "confirmed_date": extracted_date,
                }
                date_display = f"<b>{extracted_date}</b>" if extracted_date else "<i>No date detected — please provide the date</i>"
                await msg_obj.reply_text(
                    f"Got it — confirmed <b>{label}</b> ({business_name}).\n\n"
                    f"📅 Session date detected: {date_display}\n\n"
                    "Is this correct? Reply <b>yes</b> to confirm, or send the correct date in YYYY-MM-DD format (e.g. 2026-07-01).",
                    parse_mode=ParseMode.HTML,
                )
                confirmed = True
                tool_result = "Client confirmed. Waiting for date confirmation."

            else:
                tool_result = "Unknown tool."

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_result})

        if confirmed:
            return ASK_SESSION_DATE
        # Loop back to let the AI respond after tool results

    # Exhausted iterations without confirming
    logger.error("Identification agent hit max iterations without confirming.")
    await msg_obj.reply_text("Could not identify the client automatically. Please try again with /oneonone.")
    context.user_data.clear()
    return ConversationHandler.END


async def _run_coaching_with_photo(update, context, proof_photo_url: str = None) -> int:
    """Shared logic: process the pending coaching session (with optional proof photo URL)."""
    pending = context.user_data.pop("pending_coaching", {})
    if not pending:
        await update.effective_message.reply_text("Session expired. Please start again with /oneonone.")
        return ConversationHandler.END
    msg_obj = update.effective_message
    await msg_obj.chat.send_action(ChatAction.TYPING)
    await msg_obj.reply_text(f"Processing summary for {pending['label']} ({pending['business_name']})...")
    try:
        markdown, _ = await asyncio.to_thread(
            coaching.process_transcript,
            pending["transcript"],
            msg_obj.chat_id,
            pending["client_name"],
            pending["business_name"],
            pending["client_record"],
            msg_obj.message_thread_id,
            pending.get("client_name_2"),
            pending.get("client_record_2"),
            proof_photo_url,
            pending.get("confirmed_date"),
        )
    except Exception as e:
        logger.error("Coaching transcript error: %s", e, exc_info=True)
        await msg_obj.reply_text("Failed to process transcript. Please try again.")
        context.user_data.clear()
        return ConversationHandler.END

    # Make has already been notified at this point. Send the preview (best-effort).
    await _send_markdown_summary(
        msg_obj,
        markdown,
        "Summary has been sent to Make successfully.",
        "Summary was sent to Make successfully, but could not display the preview here.",
    )
    context.user_data.clear()
    return ConversationHandler.END


async def coaching_confirm_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User is confirming or correcting the session date before the proof photo step."""
    from datetime import datetime
    text = update.message.text.strip()
    pending = context.user_data.get("pending_coaching", {})

    if text.lower() in ("yes", "ya", "yep", "correct", "ok", "betul", "confirm", "y", "ye"):
        date = pending.get("confirmed_date") or datetime.today().strftime("%Y-%m-%d")
        pending["confirmed_date"] = date
        await update.message.reply_text(
            f"✅ Date confirmed: *{date}*\n\nPlease send a photo as proof of session to proceed.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_PROOF_PHOTO

    # Try YYYY-MM-DD directly
    try:
        datetime.strptime(text, "%Y-%m-%d")
        pending["confirmed_date"] = text
        await update.message.reply_text(
            f"✅ Date set to *{text}*\n\nPlease send a photo as proof of session to proceed.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_PROOF_PHOTO
    except ValueError:
        pass

    # Try natural language date via coaching parser
    parsed = coaching._parse_date_from_text(text)
    if parsed:
        pending["confirmed_date"] = parsed
        await update.message.reply_text(
            f"✅ Date set to *{parsed}*\n\nPlease send a photo as proof of session to proceed.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_PROOF_PHOTO

    await update.message.reply_text(
        "Please reply *yes* to confirm the date shown, or send the correct date in YYYY-MM-DD format (e.g. 2026-07-01).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_SESSION_DATE


async def coaching_proof_photo_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Catch-all for the proof photo step — block AI and re-prompt until a photo is received."""
    await update.effective_message.reply_text(
        "Please send a photo as proof of session to proceed. No other input will be accepted at this step."
    )
    return ASK_PROOF_PHOTO


async def coaching_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent a proof-of-session photo — upload to Supabase and pass the public URL to Make."""
    msg = update.message
    try:
        if msg.photo:
            tg_file = await context.bot.get_file(msg.photo[-1].file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            mime = "image/jpeg"
            filename = "proof.jpg"
        elif msg.document and (msg.document.mime_type or "").startswith("image/"):
            tg_file = await context.bot.get_file(msg.document.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            mime = msg.document.mime_type
            filename = msg.document.file_name or "proof.jpg"
        else:
            await msg.reply_text("Please send a photo or image file, or type /skip to proceed without one.")
            return ASK_PROOF_PHOTO
        proof_url = await asyncio.to_thread(payment.upload_receipt, raw, filename, mime)
        await msg.reply_text("Photo received. Generating summary...")
    except Exception as e:
        logger.error("Proof photo upload error: %s", e, exc_info=True)
        await msg.reply_text("Could not upload the photo. Please try again or type /skip.")
        return ASK_PROOF_PHOTO
    return await _run_coaching_with_photo(update, context, proof_url)



async def payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.message.chat_id):
        await update.message.reply_text("You do not have access to this bot.")
        return ConversationHandler.END
    _cancel_other_conversations(update)
    context.user_data.pop("payment", None)
    await update.message.reply_text(
        "Please upload the payment receipt (image or PDF).",
    )
    return PAYMENT_RECEIPT


async def payment_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    import io
    msg = update.message

    image_bytes = None
    mime_type = None
    filename = "receipt.jpg"
    raw_for_pdf = None

    # Photo sent as compressed image
    if msg.photo:
        photo = msg.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
        mime_type = "image/jpeg"
        filename = "receipt.jpg"

    # Document (PDF or image sent as file)
    elif msg.document:
        doc = msg.document
        fname = (doc.file_name or "receipt")
        mt = doc.mime_type or ""
        tg_file = await context.bot.get_file(doc.file_id)
        raw = bytes(await tg_file.download_as_bytearray())

        if mt.startswith("image/") or fname.lower().endswith((".jpg", ".jpeg", ".png")):
            image_bytes = raw
            mime_type = mt if mt.startswith("image/") else "image/jpeg"
            filename = fname
        elif fname.lower().endswith(".pdf") or mt == "application/pdf":
            raw_for_pdf = raw
            mime_type = "application/pdf"
            filename = fname
        else:
            await msg.reply_text("Please send an image (JPG/PNG) or PDF receipt.")
            return PAYMENT_RECEIPT
    else:
        await msg.reply_text("Please send an image (JPG/PNG) or PDF receipt.")
        return PAYMENT_RECEIPT

    await msg.chat.send_action(ChatAction.TYPING)

    # Upload to Supabase Storage for a permanent public link
    try:
        file_url = await asyncio.to_thread(
            payment.upload_receipt,
            image_bytes if image_bytes else raw_for_pdf,
            filename,
            mime_type,
        )
    except Exception as e:
        logger.error("Receipt upload error: %s", e, exc_info=True)
        file_url = ""

    # Extract payment data
    try:
        if raw_for_pdf:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(raw_for_pdf)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            extracted = await asyncio.to_thread(payment.extract_from_text, text)
        else:
            extracted = await asyncio.to_thread(payment.extract_from_image, image_bytes, mime_type)
    except Exception as e:
        logger.error("Payment extraction error: %s", e, exc_info=True)
        await msg.reply_text("Failed to read the receipt. Please try again.")
        return PAYMENT_RECEIPT

    client_name = extracted.get("clientName", "")
    company_name = extracted.get("companyName", "")
    candidates = await asyncio.to_thread(payment.lookup_client_all, client_name, company_name)

    if len(candidates) >= 2:
        context.user_data["payment_pending"] = {"mode": "initial", "extracted": extracted, "file_url": file_url}
        context.user_data["payment_candidates"] = candidates
        await update.message.reply_text(
            _disambiguation_text(candidates),
            parse_mode=ParseMode.HTML,
            reply_markup=_disambiguation_keyboard(candidates),
        )
        return PAYMENT_CLIENT_SELECT

    crm = candidates[0] if candidates else {}
    return await _show_payment_summary(update, context, extracted, file_url, crm)


async def payment_client_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles client disambiguation button press (from initial receipt flow or edit flow)."""
    query = update.callback_query
    await query.answer()

    if query.data == "pay_cancel_client":
        context.user_data.pop("payment_pending", None)
        context.user_data.pop("payment_candidates", None)
        await query.edit_message_text("Payment cancelled.")
        return ConversationHandler.END

    idx = int(query.data.split("_")[-1])
    candidates = context.user_data.pop("payment_candidates", [])
    pending = context.user_data.pop("payment_pending", {})
    crm = candidates[idx] if idx < len(candidates) else {}

    if pending.get("mode") == "edit":
        # Update the existing payment record with the selected client
        p = context.user_data.get("payment", {})
        p["memberId"] = crm.get("member_id", "")
        p["clientName"] = crm.get("name", "") or p.get("clientName", "")
        crm_company = (crm.get("company_details") or {}).get("company_name", "")
        if crm_company:
            p["companyName"] = crm_company
        if crm.get("cam"):
            p["cam"] = crm.get("cam", "")
        await query.edit_message_text(
            _payment_summary_text(p),
            parse_mode=ParseMode.HTML,
            reply_markup=_confirm_keyboard(),
        )
        return PAYMENT_CONFIRM

    await query.edit_message_text("Got it, looking up details...")
    return await _show_payment_summary(update, context, pending.get("extracted", {}), pending.get("file_url", ""), crm)


async def _show_payment_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, extracted: dict, file_url: str, crm: dict) -> int:
    client_name = extracted.get("clientName", "")
    company_name = extracted.get("companyName", "")

    crm_name = crm.get("name", "") if crm else ""
    crm_company = (crm.get("company_details") or {}).get("company_name", "") if crm else ""
    crm_cam = crm.get("cam", "") if crm else ""
    crm_member_id = crm.get("member_id", "") if crm else ""
    crm_found = "✅ found in CRM" if crm else "⚠️ not found in CRM"

    amount = extracted.get("installmentAmount", "")
    try:
        amount_display = f"RM {float(amount):,.2f}" if amount else "—"
    except (ValueError, TypeError):
        amount_display = f"RM {amount}"

    account_info = payment.lookup_account(extracted.get("recipientAccount", ""))

    context.user_data["payment"] = {
        "memberId": crm_member_id,
        "clientName": crm_name or client_name,
        "companyName": crm_company or company_name,
        "installmentAmount": amount,
        "attachmentLink": file_url,
        "installmentMonth": extracted.get("installmentMonth", ""),
        "cam": crm_cam,
        "reference": extracted.get("reference", ""),
        "remark": "",
        "recipientAccount": extracted.get("recipientAccount", ""),
        "bankName": account_info.get("bank", ""),
        "accountName": account_info.get("accountName", ""),
    }

    await update.effective_message.reply_text(
        _payment_summary_text(context.user_data["payment"]),
        parse_mode=ParseMode.HTML,
        reply_markup=_confirm_keyboard(),
    )
    return PAYMENT_CONFIRM


_PAYMENT_FIELDS = [
    ("memberId",          "Member ID"),
    ("clientName",        "Client Name"),
    ("companyName",       "Company Name"),
    ("installmentAmount", "Amount (RM)"),
    ("installmentMonth",  "Month"),
    ("reference",         "Reference"),
    ("cam",               "CAM"),
    ("remark",            "Remark"),
    ("bankName",          "Bank"),
    ("accountName",       "Account Name"),
]


def _payment_summary_text(p: dict) -> str:
    amount = p.get("installmentAmount", "")
    try:
        amount_display = f"RM {float(amount):,.2f}" if amount else "—"
    except (ValueError, TypeError):
        amount_display = f"RM {amount}"
    return (
        f"📋 <b>Payment Details</b>\n\n"
        f"🪪 Member ID: {p.get('memberId') or '—'}\n"
        f"👤 Client: {p.get('clientName') or '—'}\n"
        f"🏢 Company: {p.get('companyName') or '—'}\n"
        f"💰 Amount: {amount_display}\n"
        f"📅 Month: {p.get('installmentMonth') or '—'}\n"
        f"🔖 Reference: {p.get('reference') or '—'}\n"
        f"👨‍💼 CAM: {p.get('cam') or '—'}\n"
        f"📝 Remark: {p.get('remark') or '—'}\n"
        f"🏦 Bank: {p.get('bankName') or '—'}\n"
        f"🏷 Account: {p.get('accountName') or '—'}\n\n"
        f"⚠️ <i>Please make sure all details are correct before submitting.</i>"
    )


def _disambiguation_text(candidates: list) -> str:
    lines = ["<b>Multiple clients found with this name. Which one?</b>\n"]
    for c in candidates:
        tag = "🟢 Current member" if c.get("_source") == "active" else "🔴 Former member"
        company = (c.get("company_details") or {}).get("company_name", "—")
        member_id = c.get("member_id", "—")
        lines.append(f"{tag}: <b>{c.get('name', '—')}</b> — {company} (ID: {member_id})")
    return "\n".join(lines)


def _disambiguation_keyboard(candidates: list) -> InlineKeyboardMarkup:
    buttons = []
    for i, c in enumerate(candidates):
        tag = "🟢 Current" if c.get("_source") == "active" else "🔴 Former"
        member_id = c.get("member_id", "—")
        buttons.append([InlineKeyboardButton(f"{tag}: {c.get('name', '')} ({member_id})", callback_data=f"pay_client_{i}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="pay_cancel_client")])
    return InlineKeyboardMarkup(buttons)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="pay_confirm"),
        InlineKeyboardButton("✏️ Edit", callback_data="pay_edit"),
        InlineKeyboardButton("❌ Cancel", callback_data="pay_cancel"),
    ]])


def _edit_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, (_, label) in enumerate(_PAYMENT_FIELDS):
        row.append(InlineKeyboardButton(label, callback_data=f"pay_field_{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("← Back", callback_data="pay_back")])
    return InlineKeyboardMarkup(rows)


async def _notify_payment(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    """Send a payment notification to the configured group/topic."""
    notify_chat = os.environ.get("PAYMENT_NOTIFY_CHAT_ID")
    if not notify_chat:
        return
    notify_thread = os.environ.get("PAYMENT_NOTIFY_THREAD_ID")
    amount = payload.get("installmentAmount", "")
    month = payload.get("installmentMonth", "")
    client = payload.get("clientName", "")
    company = payload.get("companyName", "")
    cam = payload.get("cam", "")
    member_id = payload.get("memberId", "")
    reference = payload.get("reference", "")
    remark = payload.get("remark", "")
    attachment = payload.get("attachmentLink", "")

    lines = [
        "💳 <b>New Payment Submitted</b>",
        "",
        f"👤 <b>Client:</b> {client}" + (f" ({company})" if company else ""),
        f"🆔 <b>Member ID:</b> {member_id or '—'}",
        f"💰 <b>Amount:</b> RM {amount}",
        f"📅 <b>Month:</b> {month}",
        f"👨‍💼 <b>CAM:</b> {cam or '—'}",
        f"🔖 <b>Reference:</b> {reference or '—'}",
    ]
    if remark:
        lines.append(f"📝 <b>Remark:</b> {remark}")
    bank = payload.get("bankName", "")
    account_name = payload.get("accountName", "")
    if bank or account_name:
        lines.append(f"🏦 <b>Bank:</b> {bank or '—'} – {account_name or '—'}")
    if attachment:
        lines.append(f'🧾 <a href="{attachment}">View Receipt</a>')

    text = "\n".join(lines)
    kwargs = dict(chat_id=notify_chat, text=text, parse_mode=ParseMode.HTML)
    if notify_thread:
        kwargs["message_thread_id"] = int(notify_thread)
    try:
        await context.bot.send_message(**kwargs)
    except Exception as e:
        logger.error("Payment notification error: %s", e, exc_info=True)


async def payment_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles ✅ Confirm / ✏️ Edit / ❌ Cancel button presses."""
    query = update.callback_query

    if query.data == "pay_cancel":
        await query.answer()
        context.user_data.pop("payment", None)
        await query.edit_message_text("Payment update cancelled.")
        return ConversationHandler.END

    if query.data == "pay_edit":
        await query.answer()
        await query.edit_message_text("Which field would you like to edit?", reply_markup=_edit_keyboard())
        return PAYMENT_EDIT

    # pay_confirm — validate required fields
    p = context.user_data.get("payment", {})
    if not p.get("clientName", "").strip():
        await query.answer("Client Name is required before submitting.", show_alert=True)
        return PAYMENT_CONFIRM
    if not p.get("memberId", "").strip():
        await query.answer("Client not found in database. Please verify the client name before submitting.", show_alert=True)
        return PAYMENT_CONFIRM

    await query.answer()

    payload = context.user_data.pop("payment", {})
    payload["telegramChatId"] = str(query.message.chat_id)
    payload["telegramThreadId"] = str(query.message.message_thread_id or "")
    try:
        await asyncio.to_thread(payment.send_to_make, payload)
        await asyncio.to_thread(db.save_payment, payload)
        lines = [
            "✅ <b>Payment submitted successfully!</b>",
            "",
            f"👤 <b>Client:</b> {payload.get('clientName', '—')}" + (f" ({payload.get('companyName')})" if payload.get('companyName') else ""),
            f"🆔 <b>Member ID:</b> {payload.get('memberId') or '—'}",
            f"💰 <b>Amount:</b> RM {payload.get('installmentAmount') or '—'}",
            f"📅 <b>Month:</b> {payload.get('installmentMonth') or '—'}",
            f"👨‍💼 <b>CAM:</b> {payload.get('cam') or '—'}",
            f"🔖 <b>Reference:</b> {payload.get('reference') or '—'}",
        ]
        if payload.get("remark"):
            lines.append(f"📝 <b>Remark:</b> {payload['remark']}")
        if payload.get("bankName") or payload.get("accountName"):
            lines.append(f"🏦 <b>Bank:</b> {payload.get('bankName') or '—'} – {payload.get('accountName') or '—'}")
        if payload.get("attachmentLink"):
            lines.append(f'🧾 <a href="{payload["attachmentLink"]}">View Receipt</a>')
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML)
        await _notify_payment(context, payload)
    except Exception as e:
        logger.error("Payment webhook error: %s", e, exc_info=True)
        await query.edit_message_text("Failed to submit. Please try again.")
    return ConversationHandler.END


async def payment_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles field selection buttons and the Back button."""
    query = update.callback_query
    await query.answer()

    p = context.user_data.get("payment")
    if not p:
        await query.edit_message_text("Session expired. Please upload the receipt again with /payment.")
        return ConversationHandler.END

    if query.data == "pay_back":
        await query.edit_message_text(
            _payment_summary_text(p),
            parse_mode=ParseMode.HTML,
            reply_markup=_confirm_keyboard(),
        )
        return PAYMENT_CONFIRM

    idx = int(query.data.split("_")[-1])
    _, label = _PAYMENT_FIELDS[idx]
    p["_editing_field"] = idx
    await query.edit_message_text(f"Enter new value for <b>{label}</b>:", parse_mode=ParseMode.HTML)
    return PAYMENT_EDIT_VALUE


async def payment_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives the typed new value, updates the field, then re-runs CRM lookup to fill empty fields."""
    p = context.user_data.get("payment")
    if not p:
        await update.message.reply_text("Session expired. Please upload the receipt again with /payment.")
        return ConversationHandler.END
    idx = p.pop("_editing_field", None)
    if idx is None:
        return PAYMENT_CONFIRM
    key, _ = _PAYMENT_FIELDS[idx]
    p[key] = update.message.text.strip()

    # Re-run CRM lookup when client name or company name is edited
    if key in ("clientName", "companyName"):
        await update.message.chat.send_action(ChatAction.TYPING)
        candidates = await asyncio.to_thread(payment.lookup_client_all, p.get("clientName", ""), p.get("companyName", ""))
        if len(candidates) >= 2:
            context.user_data["payment_pending"] = {"mode": "edit"}
            context.user_data["payment_candidates"] = candidates
            await update.message.reply_text(
                _disambiguation_text(candidates),
                parse_mode=ParseMode.HTML,
                reply_markup=_disambiguation_keyboard(candidates),
            )
            return PAYMENT_CLIENT_SELECT
        crm = candidates[0] if candidates else {}
        if crm:
            crm_company = (crm.get("company_details") or {}).get("company_name", "")
            p["memberId"] = crm.get("member_id", "")
            if crm_company:
                p["companyName"] = crm_company
            if crm.get("cam"):
                p["cam"] = crm.get("cam", "")
        else:
            p["memberId"] = ""

    await update.message.reply_text(
        _payment_summary_text(p),
        parse_mode=ParseMode.HTML,
        reply_markup=_confirm_keyboard(),
    )
    return PAYMENT_CONFIRM


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 1-on-1 Coaching", callback_data="menu_oneonone"),
            InlineKeyboardButton("💳 Payment Receipt", callback_data="menu_payment"),
        ],
        [
            InlineKeyboardButton("📊 Daily Report", callback_data="menu_dailyreport"),
            InlineKeyboardButton("🏆 Top 100 Sales", callback_data="menu_topsales"),
        ],
        [
            InlineKeyboardButton("🔄 Update Database", callback_data="menu_updatedatabase"),
            InlineKeyboardButton("🗑 Clear History", callback_data="menu_clear"),
        ],
    ])
    await update.message.reply_text("What would you like to do?", reply_markup=kb)


async def _coaching_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    msg = query.message
    if not is_allowed(msg.chat_id):
        await msg.reply_text("You do not have access to this bot.")
        return ConversationHandler.END
    _cancel_other_conversations(update)
    context.user_data.pop("transcript_parts", None)
    context.user_data.pop("transcript", None)
    context.user_data.pop("ai_messages", None)
    context.user_data.pop("search_cache", None)
    await msg.reply_text("Please send the transcript file (.txt, .docx, or .pdf) or paste the transcript text.")
    return ASK_TRANSCRIPT


async def _dailyreport_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    msg = query.message
    if not is_allowed(msg.chat_id):
        await msg.reply_text("You do not have access to this bot.")
        return ConversationHandler.END
    _cancel_other_conversations(update)
    await msg.reply_text(
        "Which date would you like the report for?\nReply with a date (YYYY-MM-DD) or type *today*.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return DAILYREPORT_DATE


async def _payment_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    msg = query.message
    if not is_allowed(msg.chat_id):
        await msg.reply_text("You do not have access to this bot.")
        return ConversationHandler.END
    _cancel_other_conversations(update)
    context.user_data.pop("payment", None)
    await msg.reply_text("Please upload the payment receipt (image or PDF).")
    return PAYMENT_RECEIPT


async def _menu_simple_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles topsales / updatedatabase / clear tapped from the menu."""
    query = update.callback_query
    await query.answer()
    msg = query.message

    if query.data == "menu_clear":
        _histories.pop(msg.chat_id, None)
        await msg.reply_text("Conversation cleared.")
        return

    if not is_allowed(msg.chat_id):
        await msg.reply_text("You do not have access to this bot.")
        return

    if query.data == "menu_topsales":
        script_url = os.environ.get("TOP100_SLIDES_URL")
        if not script_url:
            await msg.reply_text("Top 100 slides is not configured.")
            return
        await msg.reply_text("Generating Top 100 Sales slides, please wait. The slides will be sent here when ready...")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                params = {"chat_id": msg.chat_id}
                if msg.message_thread_id:
                    params["thread_id"] = msg.message_thread_id
                resp = await client.get(script_url, params=params, follow_redirects=True)
                data = resp.json()
                if data.get("error"):
                    logger.error("Apps Script error: %s", data["error"])
                    await msg.reply_text(f"Script error: {data['error']}")
        except httpx.TimeoutException:
            pass
        except Exception as e:
            logger.error("topsales error: %s", e, exc_info=True)
            await msg.reply_text("Failed to reach the script. Please try again.")

    elif query.data == "menu_updatedatabase":
        webhook_url = os.environ.get("N8N_WEBHOOK_URL")
        if not webhook_url:
            await msg.reply_text("Database update is not configured.")
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(webhook_url, json={"chat_id": msg.chat_id})
            if resp.status_code in (200, 202):
                await msg.reply_text("Database update started. This usually takes around 7 minutes.")
            else:
                logger.error("n8n returned status %s: %s", resp.status_code, resp.text)
                await msg.reply_text(f"Failed to start update (status {resp.status_code}). Please check n8n.")
        except Exception as e:
            logger.error("n8n webhook error: %s", e, exc_info=True)
            await msg.reply_text("Failed to reach n8n. Please try again.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused error: %s", context.error, exc_info=True)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("menu", "Show all available actions"),
            BotCommand("start", "Start the bot"),
            BotCommand("oneonone", "Generate a 1-on-1 coaching session summary"),
            BotCommand("topsales", "View top performing companies by sales"),
            BotCommand("updatedatabase", "Sync and update the database"),
            BotCommand("dailyreport", "Generate coaching report. Usage: /dailyreport or /dailyreport YYYY-MM-DD"),
            BotCommand("payment", "Submit a payment receipt to Google Sheet"),
            BotCommand("clear", "Clear conversation history"),
        ])

    app = Application.builder().token(token).post_init(post_init).build()

    global _coaching_conv, _payment_conv, _dailyreport_conv

    _coaching_conv = ConversationHandler(
        entry_points=[
            CommandHandler("oneonone", coaching_command),
            CallbackQueryHandler(_coaching_menu_entry, pattern="^menu_oneonone$"),
        ],
        states={
            ASK_TRANSCRIPT: [
                MessageHandler(filters.Document.ALL, received_transcript),
                CallbackQueryHandler(transcript_process, pattern="^transcript_process$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_transcript),
            ],
            AI_CONVERSATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ai_conversation)
            ],
            ASK_SESSION_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, coaching_confirm_date),
            ],
            ASK_PROOF_PHOTO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, coaching_proof_photo),
                MessageHandler(filters.ALL & ~filters.COMMAND, coaching_proof_photo_nudge),
            ],
        },
        fallbacks=[],
        per_chat=True,
        allow_reentry=True,
    )
    coaching_conv = _coaching_conv

    _dailyreport_conv = ConversationHandler(
        entry_points=[
            CommandHandler("dailyreport", dailyreport),
            CallbackQueryHandler(_dailyreport_menu_entry, pattern="^menu_dailyreport$"),
        ],
        states={DAILYREPORT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, dailyreport_date)]},
        fallbacks=[CommandHandler("dailyreport", dailyreport)],
        per_chat=True,
        allow_reentry=True,
    )
    dailyreport_conv = _dailyreport_conv

    _payment_conv = ConversationHandler(
        entry_points=[
            CommandHandler("payment", payment_command),
            CallbackQueryHandler(_payment_menu_entry, pattern="^menu_payment$"),
        ],
        states={
            PAYMENT_RECEIPT: [MessageHandler(filters.PHOTO | filters.Document.ALL, payment_receipt)],
            PAYMENT_CLIENT_SELECT: [CallbackQueryHandler(payment_client_select, pattern="^pay_(client_\\d+|cancel_client)$")],
            PAYMENT_CONFIRM: [CallbackQueryHandler(payment_confirm, pattern="^pay_(confirm|edit|cancel)$")],
            PAYMENT_EDIT: [CallbackQueryHandler(payment_edit, pattern="^pay_(field_\\d+|back)$")],
            PAYMENT_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_edit_value)],
        },
        fallbacks=[CommandHandler("payment", payment_command)],
        per_chat=True,
        allow_reentry=True,
    )
    payment_conv = _payment_conv

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("updatedatabase", updatedatabase))
    app.add_handler(CommandHandler("topsales", topsales))
    app.add_handler(CallbackQueryHandler(_menu_simple_action, pattern="^menu_(topsales|updatedatabase|clear)$"))
    app.add_handler(dailyreport_conv)
    app.add_handler(payment_conv)
    app.add_handler(coaching_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
