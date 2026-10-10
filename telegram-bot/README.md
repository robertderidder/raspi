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

Every command is wrapped in a permission check before it runs — see
[Access control](#access-control). Your bot token is a secret, and if it ever
leaks, that check is what stops a stranger from querying your Pi.

## Commands

| | Min role | |
|---|---|---|
| `/help` | guest | Only the commands *you* can actually run |
| `/whoami` | guest | Your access level, and what it permits |
| `/ping` | guest | Liveness check and uptime |
| `/pihole` | guest | Pi-hole stats for the last 24 h, plus CPU temp, load, disk, RAM |
| `/pause [10m]` | guest | Pause Pi-hole blocking; `/pause off` to re-enable early |
| `/transmission` | guest | Torrent progress from `transmission-remote` |
| `/update` | admin | System + Pi-hole update, then reboot. Asks with a confirm button, live for 60 s |
| `/backup` | admin | Run the Pi-hole config backup now; `/backup check` to dry-run it |
| `/access` | **owner** | Add, remove and tune everyone else's permissions |

## Access control

Four roles, ranked `blocked < guest < admin < owner`.

| Role | Who | Can |
|---|---|---|
| `owner` | you | everything, including `/access` |
| `admin` | a trusted second person | everything except changing permissions |
| `guest` | a roommate | the everyday commands |
| `blocked` | a denied stranger | nothing — dropped silently, with no further prompts |

**You are the owner, structurally.** The owner id is `ALLOWED_CHAT_ID` from `.env`
and is checked *before* the state file is ever read — it is never written to it.
Delete, corrupt or truncate `allowed_chats.json` and everyone else loses access
while yours is untouched. There is no sequence of button presses that demotes you
or promotes anyone else to owner.

### Adding someone

You do not edit a file. When an unknown chat messages the bot, you get:

```
🔔 Access request
Sam (@sam_x) · chat id 123456789
[✅ Allow as guest]  [🛡️ Allow as admin]  [⛔ Block]
```

They get one neutral acknowledgement and hear nothing more until you decide.
A stranger cannot spam you: one prompt per chat id, then an hour of silence
before another, and a blocked id never produces a prompt again.

### Changing what someone can do

`/access` is a menu of buttons — pick a person, set their role, or open
**⚙️ Per-command…** to switch individual commands on and off for them alone.
Those per-command **grants** override the role in either direction, so a guest
can have `/update` without becoming an admin. A grant that matches what their
role already gives is dropped rather than stored, so the file only ever records
decisions you actually made.

One thing a grant cannot do: reach an owner-only command. `/access` hands out
roles, so being able to grant it away would be a way to promote yourself. Those
commands are refused in `permissions.may_run` and not even offered in the menu.

State lives in `allowed_chats.json` (gitignored, written atomically), and every
change is appended to `scripts/logs/access.log` — when someone can run something
you did not expect, that log is the answer; the JSON only shows the end state.

## Layout

```
bot.py                  long-polls Telegram, auto-discovers commands/
permissions.py          roles, grants, and the allowed_chats.json state file
access_requests.py      the "a stranger messaged your bot" approval flow
.env                    BOT_TOKEN + ALLOWED_CHAT_ID   (never committed)
.env.example            template
allowed_chats.json      everyone but you, written by /access   (never committed)
requirements.txt
commands/               one file per command
scripts/
  pihole_report.sh      formats the /pihole report; prints HTML to stdout
  login_checker.sh      SSH login watcher, run by login-checker.service
  auto_update.sh        monthly apt + pihole update, run by cron and /update
  pihole_backup.sh      daily config backup, run by pihole-backup.timer and /backup
  logs/                 append-only logs from the above, plus access.log
```

`ALLOWED_CHAT_ID` keeps its name because `auto_update.sh` and `login_checker.sh`
both source this same `.env` and use it as their `sendMessage` destination.
Renaming it would break both, silently. The bot never writes `.env`.

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
sudo cp ~/raspi/system/etc/systemd/system/pihole-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot login-checker pihole-backup.timer

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
MIN_ROLE = "guest"          # optional — defaults to "admin"

async def handler(update, context):
    await update.effective_message.reply_text("Your response here")
```

> **`MIN_ROLE` defaults to `admin`, not `guest`.** A new file dropped into
> `commands/` is not silently handed to the roommate — opening a command up is a
> visible one-line decision in that file. Use `"owner"` for anything that should
> never be delegated at all.

To give a command inline buttons, add a callback namespace and a second handler:

```python
CALLBACK_PREFIX = "whatever"        # its callback_data namespace

async def callback(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(f"You pressed {query.data}")
```

`bot.py` registers the callback with the *same* permission as the command, so a
stale keyboard is not a way around the check. `commands/update.py` is the worked
example — note the one-shot nonce in its `callback_data`, which is what stops a
button from an earlier `/update` still being live an hour later.

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
