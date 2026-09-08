#!/usr/bin/env python3
"""
Extensible Telegram command bot.

Add a new command by dropping a .py file into commands/ that defines:

    COMMAND = "name"           # what the user types: /name
    DESCRIPTION = "..."        # shown in /help (optional)
    async def handler(update, context) -> None: ...

bot.py auto-discovers and registers every command in that folder at
startup — nothing here needs to change when you add a new one.
"""
import importlib
import logging
import os
import pkgutil

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

COMMANDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "commands")


def guarded(handler):
    """Wrap a command handler so it only ever responds to your chat id.

    Anyone else who messages the bot (e.g. if the token ever leaks) is
    silently ignored rather than getting a reply or an error.
    """
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id != ALLOWED_CHAT_ID:
            log.warning("Ignored message from unauthorized chat_id=%s", chat_id)
            return
        try:
            await handler(update, context)
        except Exception:
            log.exception("Command handler failed")
            await update.message.reply_text("⚠️ Something went wrong running that command.")
    return wrapped


def load_commands(app: Application):
    """Discover and register every command module in commands/."""
    registered = []
    for _, module_name, _ in pkgutil.iter_modules([COMMANDS_DIR]):
        module = importlib.import_module(f"commands.{module_name}")
        if not hasattr(module, "COMMAND") or not hasattr(module, "handler"):
            log.warning("Skipping commands/%s.py: missing COMMAND or handler", module_name)
            continue
        name = module.COMMAND
        description = getattr(module, "DESCRIPTION", "")
        app.add_handler(CommandHandler(name, guarded(module.handler)))
        registered.append((name, description))
        log.info("Registered command /%s", name)
    return registered


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["<b>Available commands:</b>"]
    for name, desc in sorted(context.bot_data.get("commands", [])):
        lines.append(f"/{name} — {desc}" if desc else f"/{name}")
    await update.message.reply_html("\n".join(lines))


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    commands = load_commands(app)
    app.bot_data["commands"] = commands
    app.add_handler(CommandHandler("help", guarded(help_command)))
    log.info("Bot starting with %d command(s) loaded", len(commands))
    app.run_polling()


if __name__ == "__main__":
    main()
