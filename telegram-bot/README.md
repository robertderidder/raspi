# Telegram command bot

The control surface for the Pi. Query it and drive it from Telegram, on demand,
instead of a fixed daily cron push.

Everything scriptable on the Pi lives in this folder — the bot commands, the
shell scripts they call, and the long-running watchers. There is no `~/scripts`
directory; if a new script is worth keeping, it belongs in `scripts/` here with
a command in `commands/` to trigger it.

## How it works

`bot.py` long-polls Telegram — no port forwarding, no public IP, no webhook. It
auto-discovers every module in `commands/` at startup, so adding a command is
dropping in a file; `bot.py` never changes.

Every command is wrapped so it **only ever responds to `ALLOWED_CHAT_ID`**.
Messages from anyone else are logged and dropped. Your bot token is a secret, and
if it ever leaks that whitelist is what stops a stranger from querying your Pi.

## Commands

| | |
|---|---|
| `/help` | Auto-generated from every loaded module |
| `/ping` | Liveness check and uptime |
| `/pihole` | Pi-hole stats for the last 24 h, plus CPU temp, load, disk, RAM |
| `/pause [10m]` | Pause Pi-hole blocking; `/pause off` to re-enable early |
| `/transmission` | Torrent progress from `transmission-remote` |
| `/update` | System + Pi-hole update, then reboot. Needs `/update confirm` within 60 s |

## Layout

```
bot.py                  long-polls Telegram, auto-discovers commands/
.env                    BOT_TOKEN + ALLOWED_CHAT_ID   (never committed)
.env.example            template
requirements.txt
commands/               one file per command
scripts/
  pihole_report.sh      formats the /pihole report; prints HTML to stdout
  login_checker.sh      SSH login watcher, run by login-checker.service
  auto_update.sh        weekly apt + pihole update, run by cron and /update
  logs/                 append-only logs from the two above
```

The shell scripts read `BOT_TOKEN` from this folder's `.env` rather than keeping
their own copy, so there is one file to rotate. They locate it relative to
themselves, so moving the whole folder does not break them.

## Setup

1. **Bot token** — message [@BotFather](https://t.me/BotFather), run `/newbot`.
2. **Chat ID** — message [@userinfobot](https://t.me/userinfobot).

```bash
cd ~/telegram-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
chmod +x scripts/*.sh
cp .env.example .env
nano .env                      # BOT_TOKEN and ALLOWED_CHAT_ID

python bot.py                  # test in the foreground; message it /ping
```

Then install the services. The unit files are tracked in the parent repo under
`system/`, already pointing at the right user and paths:

```bash
sudo cp ~/raspi/system/etc/systemd/system/telegram-bot.service /etc/systemd/system/
sudo cp ~/raspi/system/etc/systemd/system/login-checker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot login-checker

journalctl -u telegram-bot -f
```

`login_checker.sh` needs journal access and `jq` to name Tailscale devices:

```bash
sudo usermod -aG systemd-journal robert
sudo apt install jq
```

`pihole_report.sh` needs to read Pi-hole's query database:

```bash
sudo usermod -aG pihole robert
sudo apt install bc sqlite3
```

## Adding a command

Create `commands/whatever.py`:

```python
COMMAND = "whatever"
DESCRIPTION = "One line, shown in /help"

async def handler(update, context):
    await update.message.reply_text("Your response here")
```

```bash
sudo systemctl restart telegram-bot
```

`/whatever` is live and appears in `/help`. To call a shell script, put it in
`scripts/` and `subprocess.run` it the way `commands/pihole.py` does — resolve
the path relative to `__file__`, never hardcode `/home/robert`.

> `COMMAND` must be unique. Two modules declaring the same name will both
> register, and only one of them will ever answer — a copied template file with
> its `COMMAND` left unchanged is the easy way to lose a command silently.

## Watch out

`python-telegram-bot` logs every HTTP request at INFO, and the bot token is in
the request URL — so **the token appears in `journalctl -u telegram-bot`** in
plain text. That is fine for a single-user box where only you can read the
journal, but do not paste those logs anywhere, and rotate the token if you do.
