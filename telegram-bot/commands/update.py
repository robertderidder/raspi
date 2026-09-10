"""/update — trigger the existing auto_update.sh on demand.

This reboots the Pi, so it asks first:

    /update   ->  a warning with [✅ Yes, update & reboot] [❌ Cancel]

The buttons are live for 60 seconds. The script itself already handles logging,
notifying on completion, and rebooting -- this handler just launches it as a
detached background process so the bot can reply immediately instead of hanging
until the whole update (and reboot) finishes.
"""
import logging
import os
import secrets
import subprocess
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("update")

COMMAND = "update"
DESCRIPTION = "Run system + Pi-hole update, then reboot (asks first)"
MIN_ROLE = "admin"
CALLBACK_PREFIX = "update"

# Lives alongside the bot, in telegram-bot/scripts/.
SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "auto_update.sh"
)

CONFIRM_WINDOW_SECONDS = 60

# One-shot token -> (chat_id, issued at). Keyed by a fresh nonce rather than by
# chat id so that a button from an earlier /update, still sitting in the chat
# history, cannot reboot the Pi an hour later: each /update mints its own token
# and pressing consumes it.
#
# In memory, so a bot restart empties it and any surviving button fails closed
# with "expired" -- which is the behaviour you want from the reboot button.
_pending: dict[str, tuple[int, float]] = {}


def _keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, update & reboot", callback_data=f"{CALLBACK_PREFIX}:go:{token}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"{CALLBACK_PREFIX}:cancel:{token}"),
    ]])


def _expire_stale(now: float) -> None:
    """Drop tokens nobody ever pressed, so the dict cannot grow unbounded."""
    for token, (_, issued) in list(_pending.items()):
        if now - issued > CONFIRM_WINDOW_SECONDS:
            del _pending[token]


async def handler(update, context):
    now = time.time()
    _expire_stale(now)

    token = secrets.token_urlsafe(8)
    _pending[token] = (update.effective_chat.id, now)

    await update.effective_message.reply_html(
        "⚠️ This will update system packages + Pi-hole, then <b>reboot</b> the Pi.\n\n"
        f"Confirm within {CONFIRM_WINDOW_SECONDS} seconds.",
        reply_markup=_keyboard(token),
    )


async def callback(update, context):
    query = update.callback_query
    _, action, token = query.data.split(":", 2)

    now = time.time()
    _expire_stale(now)
    entry = _pending.pop(token, None)

    if entry is None:
        await query.answer("That confirmation expired.")
        await query.edit_message_text(
            "⌛ Confirmation expired — nothing was done.\nSend /update again if you still want it."
        )
        return

    if action == "cancel":
        await query.answer("Cancelled")
        await query.edit_message_text("❌ Update cancelled. Nothing was changed.")
        return

    await query.answer("Starting")
    log.warning("Update confirmed by chat_id=%s", update.effective_chat.id)

    # start_new_session detaches this from the bot's process group, so it keeps
    # running (and can reboot the Pi) independent of the bot.
    subprocess.Popen(
        ["bash", SCRIPT_PATH],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await query.edit_message_text(
        "🔄 Update started in the background.\n\n"
        "You'll get a message here when it's done, then the Pi will reboot — "
        "the bot (and everything else) will be offline for a minute or two after that."
    )
