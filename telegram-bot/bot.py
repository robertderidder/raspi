#!/usr/bin/env python3
"""
Extensible Telegram command bot.

Add a new command by dropping a .py file into commands/ that defines:

    COMMAND = "name"           # what the user types: /name
    DESCRIPTION = "..."        # shown in /help (optional)
    MIN_ROLE = "guest"         # who may run it (optional, default "admin")
    async def handler(update, context) -> None: ...

A command that owns inline buttons adds two more:

    CALLBACK_PREFIX = "name"   # its callback_data namespace, "name:..."
    async def callback(update, context) -> None: ...

bot.py auto-discovers and registers all of that at startup -- nothing here
needs to change when you add a new command, buttons or not.

Access control lives in permissions.py. Note the default: a module that does
not declare MIN_ROLE is admin-only, so a new file is never silently exposed to
guests. See the README.
"""
import html
import importlib
import logging
import os
import pkgutil
import re

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import access_requests
import permissions

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
# The owner. Deliberately still called ALLOWED_CHAT_ID: scripts/auto_update.sh
# and scripts/login_checker.sh both source this same .env and use the variable
# as their sendMessage destination. Renaming it here would break both, silently.
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

COMMANDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "commands")


async def _report_error(update: Update) -> None:
    """Tell the caller a handler blew up, whether they typed or tapped.

    `update.message` is None for a callback query, so the obvious
    `update.message.reply_text` raises here -- inside the error path, replacing
    the real traceback with an AttributeError. Use effective_message, and stop
    the button's spinner first.
    """
    query = update.callback_query
    if query is not None:
        try:
            await query.answer("Something went wrong", show_alert=True)
        except Exception:
            pass  # cosmetic; the reply below is what matters
    message = update.effective_message
    if message is not None:
        try:
            await message.reply_text("⚠️ Something went wrong running that command.")
        except Exception:
            log.exception("Could not report the failure to the user")


def guarded(handler, command: str, min_role: str):
    """Wrap a handler so it only runs for someone allowed to run `command`.

    Three outcomes for someone who is not:

      unknown   Silence here. The catch-all in handler group 1 turns their
                message into an access request for the owner, so that lives in
                one place instead of being duplicated per command.
      blocked   Silence, always. A blocked id never learns the bot is awake.
      known     A short refusal -- they exist, they just cannot run this one.
    """
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        chat_id = chat.id if chat else None
        if chat_id is None:
            return

        if not permissions.is_known(chat_id):
            return
        if not permissions.may_run(chat_id, command, min_role):
            role = permissions.role_of(chat_id)
            if role == permissions.BLOCKED:
                return
            log.info("Refused /%s to chat_id=%s (role=%s)", command, chat_id, role)
            query = update.callback_query
            if query is not None:
                await query.answer("You do not have access to that.", show_alert=True)
            elif update.effective_message is not None:
                await update.effective_message.reply_text(
                    f"🔒 /{command} is not available to you (you are {role})."
                )
            return

        try:
            await handler(update, context)
        except Exception:
            log.exception("Handler for /%s failed", command)
            await _report_error(update)

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
        min_role = getattr(module, "MIN_ROLE", permissions.DEFAULT_MIN_ROLE)
        if min_role not in permissions.RANK:
            log.warning(
                "commands/%s.py declares MIN_ROLE=%r, which is not a role -- using %r",
                module_name, min_role, permissions.DEFAULT_MIN_ROLE,
            )
            min_role = permissions.DEFAULT_MIN_ROLE

        app.add_handler(CommandHandler(name, guarded(module.handler, name, min_role)))

        prefix = getattr(module, "CALLBACK_PREFIX", None)
        callback = getattr(module, "callback", None)
        if prefix and callback:
            # The buttons carry the same permission as the command that drew
            # them -- a stale keyboard is not a way around the check.
            app.add_handler(
                CallbackQueryHandler(
                    guarded(callback, name, min_role),
                    pattern=rf"^{re.escape(prefix)}:",
                )
            )
        elif prefix or callback:
            log.warning(
                "commands/%s.py has %s but not the other -- buttons will not work",
                module_name, "CALLBACK_PREFIX" if prefix else "callback",
            )

        registered.append({"name": name, "description": description, "min_role": min_role})
        log.info("Registered /%s (min_role=%s)", name, min_role)
    return sorted(registered, key=lambda c: c["name"])


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List what *this* caller can actually run -- nothing they cannot."""
    chat_id = update.effective_chat.id
    lines = ["<b>Available commands:</b>"]
    for entry in context.bot_data.get("commands", []):
        if not permissions.may_run(chat_id, entry["name"], entry["min_role"]):
            continue
        name, desc = entry["name"], entry["description"]
        lines.append(f"/{name} — {html.escape(desc)}" if desc else f"/{name}")
    lines.append("/whoami — Show your access level")
    await update.effective_message.reply_html("\n".join(lines))


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Your role and what it currently permits -- the first thing a confused
    guest will reach for, and the quickest way to confirm a permission change
    actually landed."""
    chat_id = update.effective_chat.id
    role = permissions.role_of(chat_id)
    lines = [f"👤 You are <b>{role}</b> (chat id <code>{chat_id}</code>)."]

    allowed, denied = [], []
    for entry in context.bot_data.get("commands", []):
        target = allowed if permissions.may_run(chat_id, entry["name"], entry["min_role"]) else denied
        marker = "✱" if permissions.grant_of(chat_id, entry["name"]) is not None else ""
        target.append(f"/{entry['name']}{marker}")

    lines.append(f"\n✅ {', '.join(allowed) if allowed else 'nothing'}")
    if denied:
        lines.append(f"🔒 {', '.join(denied)}")
    if any("✱" in item for item in allowed + denied):
        lines.append("\n<i>✱ set for you specifically, not by your role.</i>")
    await update.effective_message.reply_html("\n".join(lines))


async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every message from someone the bot has never decided on.

    Runs in handler group 1, so it never competes with the command handlers in
    group 0 -- they match first, and this still gets its turn afterwards. For
    anyone already known it returns immediately, which is the common case and
    has to stay cheap.
    """
    chat = update.effective_chat
    if chat is None or permissions.is_known(chat.id):
        return
    await access_requests.handle_stranger(update, context)


def main():
    permissions.init(ALLOWED_CHAT_ID)

    app = Application.builder().token(BOT_TOKEN).build()
    commands = load_commands(app)
    app.bot_data["commands"] = commands
    app.bot_data["owner_id"] = ALLOWED_CHAT_ID

    app.add_handler(CommandHandler("help", guarded(help_command, "help", permissions.GUEST)))
    app.add_handler(CommandHandler("whoami", guarded(whoami_command, "whoami", permissions.GUEST)))

    # Approving a stranger is the privilege boundary, so it is not a command
    # module and not subject to the module contract -- it checks is_owner itself.
    app.add_handler(
        CallbackQueryHandler(
            access_requests.on_decision,
            pattern=rf"^{access_requests.PREFIX}:",
        )
    )
    app.add_handler(MessageHandler(filters.ALL, catch_all), group=1)

    log.info(
        "Bot starting: %d command(s), owner=%s, %d known user(s)",
        len(commands), ALLOWED_CHAT_ID, len(permissions.all_users()),
    )
    app.run_polling()


if __name__ == "__main__":
    main()
