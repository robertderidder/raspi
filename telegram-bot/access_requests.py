"""What happens when someone the bot has never seen sends it a message.

The owner gets one message with three buttons:

    🔔 Access request
    Sam (@sam_x) · id 123456789
    [✅ Allow as guest]  [🛡️ Allow as admin]  [⛔ Block]

The stranger gets a single neutral acknowledgement and then nothing until a
decision is made. Blocked ids get nothing, ever.

Two things this has to survive, because the bot's token is one leak away from
being public and the owner's phone is on the other end of it:

  * A stranger spamming the bot must not spam the owner. One prompt per id,
    then an hour of silence before another -- see COOLDOWN_SECONDS.
  * Names and usernames are attacker-controlled and the bot sends HTML, so a
    display name of "<b>admin</b>" must not render as one. Everything
    interpolated here goes through html.escape.
"""
from __future__ import annotations

import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import permissions

log = logging.getLogger("access")

PREFIX = "req"
COOLDOWN_SECONDS = 3600

# chat_id -> monotonic time we last bothered the owner about them. In memory
# only: a restart forgets the cooldown, which costs at most one extra prompt
# and never grants anything.
_last_prompt: dict[int, float] = {}

# chat_id -> who they said they were, captured when the request was raised.
# The buttons cannot carry it (64 bytes of callback_data), and after a restart
# this is empty -- the decision still applies, the name just goes unrecorded.
_pending: dict[int, dict[str, str]] = {}


async def handle_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Raise an access request for an unknown chat, at most once an hour."""
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id

    now = time.monotonic()
    previous = _last_prompt.get(chat_id)
    if previous is not None and now - previous < COOLDOWN_SECONDS:
        return
    _last_prompt[chat_id] = now

    sender = update.effective_user
    name = (sender.full_name if sender else "") or chat.title or "unknown"
    username = (sender.username if sender else "") or ""
    _pending[chat_id] = {"name": name, "username": username}

    log.warning("Access request from chat_id=%s (%s @%s)", chat_id, name, username)

    handle = f" (@{html.escape(username)})" if username else ""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Allow as guest", callback_data=f"{PREFIX}:guest:{chat_id}"),
            InlineKeyboardButton("🛡️ Allow as admin", callback_data=f"{PREFIX}:admin:{chat_id}"),
        ],
        [InlineKeyboardButton("⛔ Block", callback_data=f"{PREFIX}:blocked:{chat_id}")],
    ])

    try:
        await context.bot.send_message(
            chat_id=permissions.owner_id(),
            text=(
                "🔔 <b>Access request</b>\n\n"
                f"{html.escape(name)}{handle}\n"
                f"chat id <code>{chat_id}</code>\n\n"
                "Guests get the everyday commands; admins get everything except "
                "changing permissions. You can fine-tune either later with /access."
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except TelegramError:
        log.exception("Could not send the access request to the owner")
        return

    # Only on the very first sighting -- after that they are waiting, and
    # repeating it just confirms to a stranger that someone is home.
    if previous is None:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏳ Access request sent. You'll hear back if it's approved.",
            )
        except TelegramError:
            log.warning("Could not acknowledge the request to chat_id=%s", chat_id)


async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply the owner's choice. Registered directly, not via guarded().

    The is_owner check below is the real privilege boundary. The message was
    only ever sent to the owner's chat, so in practice nobody else has this
    button -- but "in practice nobody else has it" is not an access check, and
    this handler grants admin.
    """
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return

    if not permissions.is_owner(chat.id):
        log.warning("Ignored an access decision from non-owner chat_id=%s", chat.id)
        await query.answer("Not yours to decide.", show_alert=True)
        return

    try:
        _, decision, target = query.data.split(":", 2)
        target_id = int(target)
    except ValueError:
        await query.answer("Malformed request.", show_alert=True)
        return

    if decision not in permissions.ASSIGNABLE:
        await query.answer("Unknown decision.", show_alert=True)
        return

    info = _pending.pop(target_id, {})
    permissions.set_role(
        target_id,
        decision,
        by=chat.id,
        name=info.get("name", ""),
        username=info.get("username", ""),
    )
    if decision == permissions.BLOCKED:
        # Never prompt about them again; the block is the answer.
        _last_prompt[target_id] = float("inf")
    else:
        _last_prompt.pop(target_id, None)

    await query.answer(f"Set to {decision}")

    label = html.escape(info.get("name") or str(target_id))
    verdict = {
        permissions.GUEST: f"✅ {label} added as <b>guest</b>.",
        permissions.ADMIN: f"🛡️ {label} added as <b>admin</b>.",
        permissions.BLOCKED: f"⛔ {label} blocked. They will not be able to reach the bot again.",
    }[decision]

    # Rewrite the prompt in place so the buttons cannot be pressed twice and
    # the chat records what was decided.
    try:
        await query.edit_message_text(
            f"{verdict}\n\nchat id <code>{target_id}</code>\nManage this with /access.",
            parse_mode="HTML",
        )
    except TelegramError:
        log.warning("Could not rewrite the access prompt")

    if decision == permissions.BLOCKED:
        return

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ You've been given access to the Pi bot.\n\n"
                "Send /help to see what you can run, or /whoami for your access level."
            ),
        )
    except TelegramError:
        log.warning("Approved chat_id=%s but could not notify them", target_id)
