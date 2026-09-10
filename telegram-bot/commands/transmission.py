"""/transmission — progress report for transmission-daemon torrents.

Uses the transmission-remote CLI (ships with the transmission-daemon
Debian package) to talk to the daemon's RPC port, defaulting to
localhost:9091.

If your transmission-daemon has RPC authentication enabled (common on
the Debian package — it often prompts you to set a username/password
during setup), add this to your bot's .env:

    TRANSMISSION_AUTH=youruser:yourpassword

Leave it unset if RPC auth is disabled / localhost is whitelisted.
"""
import html
import os
import re
import subprocess

COMMAND = "transmission"
DESCRIPTION = "Show transmission-daemon torrent progress"
MIN_ROLE = "guest"

STATUS_ICONS = {
    "Downloading": "⬇️",
    "Seeding": "🌱",
    "Idle": "⏸️",
    "Stopped": "⏹️",
    "Verifying": "🔍",
    "Queued": "⏳",
}


def _parse_list(output: str):
    """Parse `transmission-remote -l` table output into a list of dicts."""
    rows = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("ID") or stripped.startswith("Sum:"):
            continue
        # Columns are separated by 2+ spaces; single spaces inside a
        # field (e.g. "700 MB", "3 min") are preserved.
        fields = re.split(r"\s{2,}", stripped)
        if len(fields) < 9:
            continue
        id_, done, have, eta, up, down, ratio, status, name = fields[:9]
        rows.append({
            "id": id_.rstrip("*"),
            "done": done,
            "have": have,
            "eta": eta,
            "status": status,
            "name": name,
        })
    return rows


async def handler(update, context):
    cmd = ["transmission-remote"]
    auth = os.environ.get("TRANSMISSION_AUTH")
    if auth:
        cmd += ["-n", auth]
    cmd += ["-l"]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "unknown error").strip()
        await update.message.reply_text(f"⚠️ Couldn't reach transmission-daemon:\n{error[:300]}")
        return

    torrents = _parse_list(result.stdout)
    if not torrents:
        await update.message.reply_text("No torrents found.")
        return

    lines = ["<b>🧲 Transmission</b>"]
    for t in torrents:
        icon = STATUS_ICONS.get(t["status"], "•")
        name = html.escape(t["name"])
        lines.append(
            f"\n{icon} <b>{name}</b>\n"
            f"   {t['done']} done • {t['have']} • {t['status']} • ETA {t['eta']}"
        )
    await update.message.reply_html("\n".join(lines))
