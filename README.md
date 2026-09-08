# raspi

Setup notes and configuration for the always-on Raspberry Pi: network-wide DNS
with Pi-hole, a file/media server, a torrent daemon, and a Telegram bot that is
the control surface for all of it.

Nothing on this box is exposed to the internet. Every service binds to localhost
and is published to the tailnet with `tailscale serve` — no port forwarding, no
dynamic-DNS, no reverse proxy.

Kept as its own repository so it can be cloned directly onto the Pi without
dragging along the laptop's dotfiles. It is also wired into
[`dotfiles`](https://github.com/robertderidder/dotfiles) as a submodule at
`raspi/`, so there is one place to look for everything.

---

## The machine

| | |
|---|---|
| Hardware | Raspberry Pi Zero 2 W Rev 1.0 — 4 cores, **425 MB RAM**, 128 GB SD card |
| OS | Raspberry Pi OS 12 (bookworm), 32-bit (`armv7l`) — Debian-based, `apt` |
| User | `robert`, with passwordless sudo (`/etc/sudoers.d/010_pi-nopasswd`) |
| LAN | `192.168.0.0/24` |
| Tailscale | `raspi` = `100.66.228.36` on `tailf54e3b.ts.net`. Exit node **and** subnet router for the LAN. Tailscale SSH is on. |
| Reached from the laptop | `ssh raspi` (see `.ssh/config` in `dotfiles`) |

That RAM figure is the constraint behind most decisions here. 425 MB is why
Pi-hole runs natively instead of in Docker, why there is a 512 MB swapfile, and
why heavier services (Jellyfin, Immich, Vaultwarden) have not been added.

`log2ram` keeps `/var/log` in a 128 MB tmpfs and flushes it to disk daily, so
the SD card is not worn out by log writes.

---

## What runs on it

| Service | Port | Purpose |
|---|---|---|
| **pihole-FTL** | `53`, `80`, `443` | DNS ad-blocking + the admin web UI. Installed natively, **not** in Docker. |
| **unbound** | `127.0.0.1:5335` | Recursive resolver. Pi-hole's only upstream — no queries go to Cloudflare or Google. |
| **copyparty** | `127.0.0.1:3923` | File browser and music/video streaming over HTTP. Serves `~/copyparty`. |
| **transmission-daemon** | `127.0.0.1:9091` | Torrents, downloading straight into `~/copyparty/torrents` so copyparty picks them up. |
| **telegram-bot** | — | Long-polls Telegram. The way the Pi is queried and controlled day to day. |
| **login-checker** | — | Tails `journalctl -u ssh`, alerts to Telegram on every login attempt. |
| **raspotify** | — | Spotify Connect endpoint, playing through the WM8960 audio HAT. |
| **tailscaled** | — | Mesh VPN. The only way in. |

### Reaching them

`tailscale serve` puts the localhost-only services on the tailnet over HTTPS,
with a real certificate and no open ports:

```
https://raspi.tailf54e3b.ts.net        -> localhost:80    Pi-hole admin
https://raspi.tailf54e3b.ts.net:3923   -> localhost:3923  copyparty
https://raspi.tailf54e3b.ts.net:9091   -> localhost:9091  transmission
```

```bash
tailscale serve status                        # what is published
tailscale serve --bg --https=9091 http://localhost:9091/   # publish something
tailscale serve --https=9091 off              # unpublish
```

---

## The Telegram bot

**Everything scriptable lives in `telegram-bot/`.** There is no `~/scripts`
directory — the bot repo is the single home for the shell scripts, the systemd
units that run them, and the commands that trigger them on demand.

```
telegram-bot/
  bot.py                    long-polls Telegram, auto-discovers commands/
  .env                      BOT_TOKEN + ALLOWED_CHAT_ID  (never committed)
  commands/
    ping.py                 /ping         liveness + uptime
    pihole.py               /pihole       24h stats + CPU temp, disk, RAM
    pihole_pause.py         /pause 10m    pause blocking, self-expiring
    transmission.py         /transmission torrent progress
    update.py               /update       system update + reboot, needs confirm
  scripts/
    pihole_report.sh        the report /pihole formats and sends
    login_checker.sh        long-lived SSH watcher (login-checker.service)
    auto_update.sh          weekly apt + pihole update, then reboot (cron)
    logs/                   append-only logs from the two above
```

`bot.py` scans `commands/` at startup, so adding a command is dropping in a file
— nothing else changes:

```python
COMMAND = "whatever"
DESCRIPTION = "One line, shown in /help"

async def handler(update, context):
    await update.message.reply_text("...")
```

```bash
sudo systemctl restart telegram-bot     # picks up the new command
journalctl -u telegram-bot -f
```

Every handler is wrapped so the bot **only ever replies to `ALLOWED_CHAT_ID`**.
Anyone else is ignored silently. That whitelist is what limits the damage if the
token leaks.

The two shell scripts read `BOT_TOKEN` from the bot's `.env` rather than keeping
their own copy, so there is one file to rotate.

---

## Timers, services and schedules

Nothing here is a systemd timer — the recurring work is one cron job. The rest
are long-lived services.

### Enabled units that are mine

| Unit | Scope | What it does |
|---|---|---|
| `telegram-bot.service` | system | The bot. `Restart=on-failure`. |
| `login-checker.service` | system | SSH login alerts. Follows the journal forever. |
| `copyparty.service` | system | `copyparty -c ~/.config/copyparty/copyparty.conf`. |
| `transmission-daemon.service` | system | Debian's unit, with a drop-in override forcing `User=robert` so downloads land in `~/copyparty/torrents` with usable permissions. |
| `tailscale.service` | **user** | Taildrop auto-receive: `tailscale file get --wait --loop` into `~/copyparty/tailscale`. Files sent from a phone appear in copyparty. |

> The user service needs `loginctl enable-linger robert` to run without an
> active login session. That is already set — if user services mysteriously stop
> surviving reboots, check it first.

### Cron

```cron
0 3 * * 1 /home/robert/telegram-bot/scripts/auto_update.sh
```

Monday 03:00: `apt update && upgrade`, `pihole -up`, `pihole -g`, a Telegram
notification, then reboot. Also reachable on demand as `/update` → `/update
confirm`.

> This entry used to read `23 59 * * 1`. Cron's field order is
> `minute hour dom month dow`, so that parsed as *minute 23 of hour 59* — an
> hour that never happens. **The weekly update had never once run**; the only
> entries in `auto_update.log` are manual invocations. Fixed to `0 3 * * 1`.

### Stock Debian timers

`apt-daily` / `apt-daily-upgrade` (download and install security updates),
`logrotate`, `man-db`, `dpkg-db-backup`, `fstrim`, `e2scrub_all`,
`systemd-tmpfiles-clean`, and `log2ram-daily` (flush the log tmpfs to disk at
23:55). None of these were configured by hand.

```bash
systemctl list-timers --all       # everything, with next/last fire times
systemctl list-unit-files --state=enabled --type=service
systemctl --user list-unit-files --state=enabled
```

---

## Setting it up from scratch

The order below is the order it actually happened, with the dead ends removed.

### 1. Base OS and remote access

Flash Raspberry Pi OS (bookworm), then:

```bash
sudo apt update && sudo apt full-upgrade -y && sudo apt autoremove -y && sudo apt clean

curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --advertise-exit-node --advertise-routes=192.168.0.0/24
sudo tailscale set --operator=$USER     # so `tailscale serve` works without sudo
sudo tailscale set --ssh
```

Approve the exit node and the subnet route in the Tailscale admin console —
advertising alone does nothing until they are approved there.

Harden SSH and confirm it:

```bash
sudo sshd -T | grep -iE "passwordauthentication|permitrootlogin|pubkeyauthentication"
```

WiFi power saving makes an idle Pi drop off the tailnet. Turn it off:

```bash
sudo iw dev wlan0 set power_save off
```

### 2. Protect the SD card

```bash
. /etc/os-release
sudo wget -O /usr/share/keyrings/azlux-archive-keyring.gpg https://azlux.fr/repo.gpg
sudo tee /etc/apt/sources.list.d/azlux.list >/dev/null <<EOF
deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ $VERSION_CODENAME main
EOF
sudo apt update && sudo apt install log2ram
sudo reboot
```

### 3. Pi-hole, with unbound as its upstream

```bash
curl -sSL https://install.pi-hole.net | bash
sudo apt install unbound -y
sudo wget -O /var/lib/unbound/root.hints https://www.internic.net/domain/named.root
sudo nano /etc/unbound/unbound.conf.d/pi-hole.conf     # tracked at system/etc/unbound/
sudo systemctl restart unbound

dig @127.0.0.1 -p 5335 google.com                      # must resolve
```

Then set Pi-hole's only upstream to `127.0.0.1#5335` in the admin UI, and point
the router's DHCP at the Pi for DNS.

```bash
pihole -v            # versions
pihole -g            # refresh blocklists
pihole -up           # update Pi-hole itself
pihole disable 10m   # works unprivileged once you are in the `pihole` group
sudo pihole -d       # debug dump, when something is genuinely wrong
```

Give your user read access to the query database so the bot's report works:

```bash
sudo usermod -aG pihole robert
```

### 4. copyparty

Installed with pipx, not apt — the Debian package is far behind.

```bash
sudo apt install pipx ffmpeg
pipx install copyparty
pipx inject copyparty Pillow      # thumbnails
pipx ensurepath                   # puts ~/.local/bin on PATH
```

Config at `.config/copyparty/copyparty.conf`, unit at
`system/etc/systemd/system/copyparty.service`:

```bash
sudo cp system/etc/systemd/system/copyparty.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now copyparty
tailscale serve --bg --https=3923 http://localhost:3923/
```

It binds `127.0.0.1` deliberately. Tailscale is the only thing in front of it.

### 5. transmission-daemon

```bash
sudo apt install transmission-daemon -y
sudo systemctl stop transmission-daemon      # it rewrites settings.json on exit
sudo systemctl edit transmission-daemon      # -> User=robert, Group=robert
```

Edit `~/.config/transmission-daemon/settings.json` **only while the daemon is
stopped** — otherwise your changes are overwritten on shutdown. Start from
`.config/transmission-daemon/settings.example.json`. The parts that matter:

```json
"download-dir": "/home/robert/copyparty/torrents",
"rpc-bind-address": "127.0.0.1",
"rpc-whitelist": "127.0.0.1,::1",
"rpc-whitelist-enabled": true
```

```bash
sudo systemctl start transmission-daemon
transmission-remote -l
tailscale serve --bg --https=9091 http://localhost:9091/
```

### 6. Telegram bot

```bash
cd ~/telegram-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env        # BOT_TOKEN from @BotFather,
                                         # ALLOWED_CHAT_ID from @userinfobot
python bot.py                            # test in the foreground first

sudo cp ~/raspi/system/etc/systemd/system/telegram-bot.service /etc/systemd/system/
sudo cp ~/raspi/system/etc/systemd/system/login-checker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot login-checker
crontab -e                               # see system/crontab.txt
```

`login_checker.sh` reads the journal, so the user needs to be in
`systemd-journal`, and uses `jq` to resolve Tailscale IPs to device names:

```bash
sudo usermod -aG systemd-journal robert
sudo apt install jq bc sqlite3
loginctl enable-linger robert            # for the user-scoped taildrop service
```

### 7. Audio (WM8960 HAT + Spotify Connect)

```bash
sudo apt install -y raspberrypi-kernel-headers dkms git
git clone https://github.com/waveshare/WM8960-Audio-HAT
cd WM8960-Audio-HAT && sudo ./install.sh && sudo reboot

sudo apt install -y pipewire pipewire-audio-client-libraries wireplumber libspa-0.2-bluetooth
wpctl status                             # find the card id
wpctl set-default <id>

curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
```

`sudo usermod -aG bluetooth robert` if you want to pair a speaker over
`bluetoothctl`.

### 8. Dotfiles

The home directory is symlinks into this repo, managed by GNU Stow.

```bash
sudo apt install stow
git clone git@github.com:robertderidder/raspi.git ~/raspi
cd ~/raspi && stow .
```

Everything in the repo root mirrors its path under `$HOME`, so
`.config/copyparty/copyparty.conf` here becomes
`~/.config/copyparty/copyparty.conf`. `.stow-local-ignore` keeps `system/`,
`README.md` and the git plumbing from being linked.

On a conflict, move the existing file aside and re-run — never `--adopt`, which
overwrites the repo's copy with whatever is on the machine.

---

## apt, for someone who thinks in pacman

Same jobs, different verbs. The big conceptual difference: `apt` splits the
package database from the package files, so **`apt update` only refreshes the
index — it installs nothing**. That is the step with no pacman equivalent, since
`pacman -Syu` does both at once.

| Task | pacman / yay | apt |
|---|---|---|
| Refresh the package index | (part of `-Sy`) | `sudo apt update` |
| Upgrade everything | `sudo pacman -Syu` | `sudo apt update && sudo apt upgrade` |
| Upgrade, allowing removals | `sudo pacman -Syu` | `sudo apt full-upgrade` |
| Install | `sudo pacman -S foo` | `sudo apt install foo` |
| Remove, keep config | `sudo pacman -R foo` | `sudo apt remove foo` |
| Remove, config and all | `sudo pacman -Rns foo` | `sudo apt purge foo` |
| Drop orphaned deps | `sudo pacman -Rns $(pacman -Qtdq)` | `sudo apt autoremove` |
| Search | `pacman -Ss foo` | `apt search foo` |
| Show package info | `pacman -Si foo` | `apt show foo` |
| Is it installed? | `pacman -Q foo` | `apt list --installed foo` |
| Files in a package | `pacman -Ql foo` | `dpkg -L foo` |
| Which package owns a file | `pacman -Qo /path` | `dpkg -S /path` |
| Explicitly-installed list | `pacman -Qqe` | `apt-mark showmanual` |
| Clean the download cache | `sudo pacman -Sc` | `sudo apt clean` / `autoclean` |
| Mark as a dependency | `pacman -D --asdeps foo` | `sudo apt-mark auto foo` |
| Hold at current version | `IgnorePkg` in pacman.conf | `sudo apt-mark hold foo` |

Things that catch you out coming from Arch:

- **`apt upgrade` will not remove packages.** If an upgrade needs something
  gone, it is silently held back — you will see "The following packages have
  been kept back". `apt full-upgrade` is what actually resolves it. (`apt
  full-upgrade` is the modern name for `apt-get dist-upgrade`.)
- **`apt` vs `apt-get`.** `apt` is the friendly front-end for interactive use;
  `apt-get` has the stable output format, so scripts use it. `auto_update.sh`
  uses `apt-get` for exactly that reason.
- **Repos are frozen, not rolling.** Debian stable ships one version of a
  package for the life of the release and only backports security fixes. A
  package being three years old is normal, not a broken mirror. This is why
  copyparty comes from pipx and Tailscale from its own repo.
- **Third-party repos are files, not one config.** Each gets a
  `/etc/apt/sources.list.d/*.list` plus a signing key in
  `/usr/share/keyrings/`, referenced by `[signed-by=...]`. Closer to adding a
  `[repo]` block in `pacman.conf` than to installing from the AUR. Currently:
  `raspi`, `tailscale`, `raspotify`, `azlux` (log2ram).
- **There is no AUR.** The equivalent is somebody's install script piped into
  a shell, a `.deb` from a GitHub release, or pipx/venv for Python things.
- **Config files are versioned.** On upgrade, apt prompts when a file you edited
  has changed upstream (`Y/I/N/O/D/Z`). This is `.pacnew`, except it interrupts
  the upgrade instead of leaving a file for later.
- **`/etc/apt/sources.list` is Debian's mirror; `/etc/apt/sources.list.d/raspi.list`
  is the Raspberry Pi Foundation's.** Firmware and kernel come from the latter.

```bash
apt list --upgradable      # what an upgrade would do
apt-mark showmanual        # tracked in system/apt-manual.txt
journalctl -u apt-daily-upgrade
```

---

## Backups and restore

The repo holds configuration; it deliberately holds no data and no secrets.

```bash
git clone git@github.com:robertderidder/raspi.git ~/raspi
cd ~/raspi && stow .
```

`system/` mirrors the root-owned files that Stow cannot manage. They are copied
in by hand — deliberately, since they need `sudo` and a `daemon-reload`:

```
system/etc/systemd/system/copyparty.service
system/etc/systemd/system/login-checker.service
system/etc/systemd/system/telegram-bot.service
system/etc/systemd/system/transmission-daemon.service.d/override.conf
system/etc/unbound/unbound.conf.d/pi-hole.conf
system/crontab.txt              crontab -l
system/apt-manual.txt           apt-mark showmanual
system/apt-sources.txt          which third-party repos are configured
system/services-enabled.txt     enabled units at snapshot time
system/timers.txt               timer schedules at snapshot time
```

Not in the repo, and lost if the SD card dies: `~/copyparty` (all media and
torrents), Pi-hole's query database and custom blocklists, and the Tailscale node
key. All of them are replaceable rather than precious — which is the whole reason
a Zero 2 W with a single SD card is an acceptable host.

---

## Secrets

**Nothing in this list belongs in this repository.** Same policy as the laptop —
see `docs/SECRETS.md` in `dotfiles`.

| Secret | Where it lives | How to replace it |
|---|---|---|
| Telegram bot token + chat ID | `telegram-bot/.env` | @BotFather → `/revoke`, then `/token` |
| copyparty salts and sessions | `.config/copyparty/*-salt.txt`, `sessions.db` | Delete them; copyparty regenerates on start (existing logins are invalidated) |
| transmission RPC credentials | `.config/transmission-daemon/settings.json` | Edit while the daemon is stopped |
| SSH keys | `~/.ssh/id_ed2519`, `id_ed2519_git` | Stored in Bitwarden as *raspi* and *tue* |
| Pi-hole admin password | Pi-hole's own config | `pihole setpassword` |
| Tailscale node key | `/var/lib/tailscale` | Re-authenticate with `tailscale up` |

`.gitignore` blocks all of these by name. It is the only thing standing between
a `git add -A` and a published credential, so extend it before adding a service
rather than after.

---

## Ideas, deliberately not done yet

Every one of these is blocked on the same thing — 425 MB of RAM and one SD card.
They need a Pi 5 with an NVMe HAT, or a real NAS.

- **Kavita** — books. Would probably fit; the least demanding of the four.
- **Jellyfin** — video. Transcoding is out of the question on this hardware.
- **Immich** — photos. Currently on the laptop against an external SSD
  (`immichup` / `immichstatus` in `dotfiles/.bash_functions`).
- **Vaultwarden** — passwords. Wants a backup story better than one SD card
  before it holds anything that matters.

## Links

- Pi-hole — https://pi-hole.net/
- unbound as a Pi-hole upstream — https://docs.pi-hole.net/guides/dns/unbound/
- copyparty — https://github.com/9001/copyparty
- Tailscale serve — https://tailscale.com/kb/1242/tailscale-serve
- log2ram — https://github.com/azlux/log2ram
- Raspotify — https://dtcooper.github.io/raspotify/
- WM8960 audio HAT — https://github.com/waveshare/WM8960-Audio-HAT
