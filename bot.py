import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

import agent

# Per-chat conversation history: {chat_id: [messages]}
_histories: dict = {}
MAX_HISTORY = 20  # max user+assistant turns to keep

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_input = update.message.text.strip()
    if not user_input:
        return

    # In group chats, only respond when the bot is mentioned
    if update.message.chat.type in ("group", "supergroup"):
        bot_username = context.bot.username
        if f"@{bot_username}" not in user_input:
            return
        # Strip the mention before passing to the agent
        user_input = user_input.replace(f"@{bot_username}", "").strip()
        if not user_input:
            return

    await update.message.chat.send_action(ChatAction.TYPING)

    chat_id = update.message.chat_id
    history = _histories.get(chat_id, [])

    try:
        answer, history = agent.ask(user_input, history)
        _histories[chat_id] = history[-MAX_HISTORY * 2:]
    except Exception as e:
        logger.error("Agent error: %s", e, exc_info=True)
        answer = "Sorry, something went wrong while processing your request. Please try again."

    # Telegram caps messages at 4096 chars — split if needed
    if len(answer) <= 4096:
        await update.message.reply_text(answer)
    else:
        for i in range(0, len(answer), 4096):
            await update.message.reply_text(answer[i:i + 4096])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused error: %s", context.error, exc_info=True)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
