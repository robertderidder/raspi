"""/update — trigger the existing auto_update.sh on demand.

Since this reboots the Pi, it requires a confirmation step:
    /update            -> asks you to confirm
    /update confirm    -> actually runs it (must be within 60s)

The script itself already handles logging, notifying on completion,
and rebooting — this handler just launches it as a detached background
process so the bot can reply immediately instead of hanging until the
whole update (and reboot) finishes.
"""
import subprocess
import time

COMMAND = "update"
DESCRIPTION = "Run system + Pi-hole update, then reboot (needs confirmation)"

# Point this at wherever the script actually lives on your Pi.
SCRIPT_PATH = "/home/robert/scripts/auto_update.sh"

CONFIRM_WINDOW_SECONDS = 60
_pending = {}  # chat_id -> timestamp of the unconfirmed /update request


async def handler(update, context):
    chat_id = update.effective_chat.id
    args = context.args

    if args and args[0].lower() == "confirm":
        requested_at = _pending.get(chat_id)
        if not requested_at or (time.time() - requested_at) > CONFIRM_WINDOW_SECONDS:
            await update.message.reply_text(
                "No pending update request (or it expired). Send /update first."
            )
            return

        _pending.pop(chat_id, None)

        # start_new_session detaches this from the bot's process group, so
        # it keeps running (and can reboot the Pi) independent of the bot.
        subprocess.Popen(
            ["bash", SCRIPT_PATH],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await update.message.reply_text(
            "🔄 Update started in the background.\n"
            "You'll get a message here when it's done, then the Pi will "
            "reboot — the bot (and everything else) will be offline for "
            "a minute or two after that."
        )
        return

    _pending[chat_id] = time.time()
    await update.message.reply_html(
        "⚠️ This will update system packages + Pi-hole, then <b>reboot</b> "
        "the Pi.\n\nSend <code>/update confirm</code> within 60 seconds to proceed."
    )
