# Telegram command bot

Extensible Telegram bot for querying your Pi/home server on demand,
instead of a fixed daily cron push.

## How it works

`bot.py` long-polls Telegram for new messages — no port forwarding or
public IP needed. It auto-discovers every command module in `commands/`
at startup. To add a new command later, just drop a new `.py` file into
`commands/` following the pattern in `commands/ping.py` — you don't need
to touch `bot.py`.

Every command is wrapped so it **only ever responds to your own chat
ID**; messages from anyone else are silently ignored. This matters
because your bot token is a secret — if it ever leaks, this whitelist
is what stops a stranger from querying your Pi.

## Setup

1. **Get a bot token** — message [@BotFather](https://t.me/BotFather) on
   Telegram, run `/newbot`, follow the prompts. It gives you a token
   like `123456789:AAExample...`.

2. **Get your chat ID** — message [@userinfobot](https://t.me/userinfobot)
   on Telegram, it replies with your numeric chat ID.

3. **Copy this folder to the Pi**, e.g. `/home/pi/telegram-bot`.

4. **Set up the environment:**
   ```bash
   cd /home/pi/telegram-bot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   chmod +x scripts/pihole_report.sh
   cp .env.example .env
   nano .env   # fill in BOT_TOKEN and ALLOWED_CHAT_ID
   ```

5. **Test it manually first:**
   ```bash
   source venv/bin/activate
   python bot.py
   ```
   Then message your bot `/ping` and `/pihole` on Telegram. Ctrl+C to
   stop once it works.

6. **Install as a systemd service** so it starts on boot and restarts
   if it crashes:
   ```bash
   sudo cp telegram-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now telegram-bot
   ```

   Check it's running:
   ```bash
   sudo systemctl status telegram-bot
   journalctl -u telegram-bot -f
   ```

## Adding a new command

Create `commands/whatever.py`:

```python
COMMAND = "whatever"
DESCRIPTION = "One-line description shown in /help"

async def handler(update, context):
    await update.message.reply_text("Your response here")
```

Restart the service (`sudo systemctl restart telegram-bot`) and the new
`/whatever` command is live — it also appears automatically in `/help`.

Your existing bash reporting scripts still work fine — just have the
handler call them with `subprocess.run(...)`, same as `commands/pihole.py`
does with your original Pi-hole script.
