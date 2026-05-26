import asyncio
import logging
import os
import re
import httpx
from html import escape
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

import agent


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
        "Ask me anything about your clients and companies, for example:\n"
        "• *Who are the clients from Iyziq?*\n"
        "• *Show me health & wellness companies in KL*\n"
        "• *Which memberships expire this month?*\n"
        "• *What are the 2024 sales for Sabella Luxe?*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def clear(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    _histories.pop(update.message.chat_id, None)
    await update.message.reply_text("Conversation cleared.")


async def updatedatabase(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    webhook_url = os.environ.get("N8N_WEBHOOK_URL")
    if not webhook_url:
        await update.message.reply_text("Database update is not configured.")
        return
    await update.message.reply_text("Database update in progress, please wait...")
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(webhook_url)
        if resp.status_code == 200:
            await update.message.reply_text("Database updated successfully.")
        else:
            logger.error("n8n returned status %s: %s", resp.status_code, resp.text)
            await update.message.reply_text(f"Database update failed (status {resp.status_code}). Please check n8n.")
    except httpx.TimeoutException:
        await update.message.reply_text("Database update timed out. Please check n8n.")
    except Exception as e:
        logger.error("n8n webhook error: %s", e, exc_info=True)
        await update.message.reply_text("Failed to reach n8n. Please try again.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused error: %s", context.error, exc_info=True)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("updatedatabase", updatedatabase))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
