"""/ping — quick liveness check. Use this file as a template for new commands."""
import subprocess

COMMAND = "ping"
DESCRIPTION = "Check the bot and Pi are alive"


async def handler(update, context):
    uptime = subprocess.run(
        ["uptime", "-p"], capture_output=True, text=True
    ).stdout.strip()
    await update.message.reply_text(f"🏓 pong — {uptime}")
