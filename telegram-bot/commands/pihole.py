"""/pihole — Pi-hole & system health report (last 24h)."""
import os
import subprocess

COMMAND = "pihole"
DESCRIPTION = "Pi-hole stats & system health (last 24h)"

SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "pihole_report.sh"
)


async def handler(update, context):
    result = subprocess.run(
        ["bash", SCRIPT_PATH],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        await update.message.reply_text(f"⚠️ Script failed:\n{result.stderr[:500]}")
        return
    await update.message.reply_html(result.stdout)
