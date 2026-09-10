"""/pause — temporarily disable Pi-hole blocking, or turn it back on.

    /pause          -> disable for the default window (5m)
    /pause 30s|10m  -> disable for that long
    /pause off      -> re-enable immediately

`pihole` re-enables itself when the timer runs out, so a forgotten
/pause cannot leave blocking off indefinitely. Runs unprivileged: the
bot user is in the `pihole` group, so no sudo is needed.
"""
import re
import subprocess

COMMAND = "pause"
DESCRIPTION = "Pause Pi-hole blocking (e.g. /pause 10m, /pause off)"
MIN_ROLE = "guest"

DEFAULT_DURATION = "5m"
# Pi-hole accepts a bare number (seconds) or a number with an s/m/h suffix.
DURATION_RE = re.compile(r"^\d+[smh]?$")


def _run(args):
    return subprocess.run(
        ["pihole"] + args, capture_output=True, text=True, timeout=20
    )


async def handler(update, context):
    arg = (context.args[0].lower() if context.args else DEFAULT_DURATION)

    if arg in ("off", "on", "enable"):
        result = _run(["enable"])
        message = "✅ Pi-hole blocking re-enabled."
    elif DURATION_RE.match(arg):
        result = _run(["disable", arg])
        message = f"⏸️ Pi-hole blocking paused for {arg}. It re-enables itself after that."
    else:
        await update.message.reply_html(
            "Usage: <code>/pause [30s|10m|2h]</code> or <code>/pause off</code>"
        )
        return

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "unknown error").strip()
        await update.message.reply_text(f"⚠️ pihole command failed:\n{error[:300]}")
        return

    await update.message.reply_text(message)
