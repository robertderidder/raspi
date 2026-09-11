"""/backup — run the Pi-hole Teleporter backup now, instead of waiting for 04:30.

    /backup         export, scrub, commit, push
    /backup check    what the last run did, and when the next one is due

Unlike /update this needs no confirmation: it only ever writes into
pihole-backup/ and pushes, so the worst case is a commit you did not need.

The script is given --no-notify because this handler reports the outcome in the
same chat; without it you would get the reply and the script's own Telegram
message for the same run.
"""
import asyncio
import logging
import os

log = logging.getLogger("backup")

COMMAND = "backup"
DESCRIPTION = "Back up the Pi-hole config to the repo now"
MIN_ROLE = "admin"

SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "pihole_backup.sh"
)

# The Zero 2 W is slow and the push waits on the network. The timer's own run has
# no deadline, but an interactive one must not hang the chat forever.
TIMEOUT_SECONDS = 300


async def _run(*args) -> tuple[int, str]:
    """Run the backup script without blocking the bot's event loop.

    subprocess.run would stall every other command for the whole export --
    noticeable on a 425 MB box where this takes tens of seconds.
    """
    process = await asyncio.create_subprocess_exec(
        "bash", SCRIPT_PATH, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"timed out after {TIMEOUT_SECONDS}s"
    return process.returncode, stdout.decode("utf-8", "replace").strip()


def _tail(output: str, lines: int = 12) -> str:
    """The last few log lines, which is where the outcome always is."""
    return "\n".join(output.splitlines()[-lines:]) or "(no output)"


async def handler(update, context):
    if context.args and context.args[0].lower() in ("check", "status"):
        code, output = await _run("--dry-run", "--no-notify")
        verdict = "would commit changes" if code == 0 else "check failed"
        await update.effective_message.reply_html(
            f"🔍 <b>Backup check</b> — {verdict}\n\n<pre>{_tail(output)}</pre>"
        )
        return

    message = await update.effective_message.reply_text("💾 Backing up Pi-hole…")

    code, output = await _run("--no-notify")
    if code == 0:
        text = f"✅ <b>Pi-hole backup done</b>\n\n<pre>{_tail(output)}</pre>"
    else:
        log.warning("pihole_backup.sh exited %s", code)
        text = (
            f"⚠️ <b>Pi-hole backup exited {code}</b>\n\n<pre>{_tail(output)}</pre>\n\n"
            "A push failure still leaves the commit safe locally — it goes out "
            "on the next run."
        )

    await message.edit_text(text, parse_mode="HTML")
