# raspi

Setup notes and configuration for my Raspberry Pi — the always-on box that runs
Pi-hole, the VPN, and the media services.

Kept as its own repository so it can be cloned directly onto the Pi without dragging
along the laptop's dotfiles. It is also wired into
[`dotfiles`](https://github.com/robertderidder/dotfiles) as a submodule at `raspi/`,
so there is one place to look for everything.

```bash
# on the Pi
git clone git@github.com:robertderidder/raspi.git ~/raspi
```

---

## The machine

| | |
|---|---|
| OS | Raspberry Pi OS (Debian-based, `apt`) |
| User | `robert` |
| Tailscale name | `raspi` — `100.66.228.36`, **advertises an exit node** |
| Reached from the laptop | `ssh raspi` (see `~/.ssh/config` in `dotfiles`) |

The laptop reaches the Pi over Tailscale rather than a LAN address, so it works from
anywhere without port forwarding. `sudo tailscale set --exit-node raspi` on the laptop
routes all traffic through the Pi.

> The `mntpi` alias and the `pi()` shell function in `dotfiles/.bash_functions` still
> refer to a host called `pihole`, which is no longer defined in `~/.ssh/config` —
> only `raspi` is. Either add a `Host pihole` alias back or update those helpers.

---

## What runs on it

| Service | Purpose | Notes |
|---|---|---|
| **Pi-hole** | Network-wide DNS ad blocking | Runs in Docker (`docker exec -it pihole ...`). Admin password: `docker exec -it pihole pihole setpassword`. |
| **PiVPN / WireGuard** | VPN back into the home network | Client profiles for laptop, both phones, and the iPad. Being superseded by Tailscale. |
| **Tailscale** | Mesh VPN + exit node | The primary remote-access path now. |
| **Nginx + Cloudflare Tunnel** | Hosts the personal website | No port forwarding, home IP stays hidden. |
| **Navidrome** | Music streaming | Library synced from `~/raspi-backup/Music`. |
| **copyparty** | File sharing over HTTP | Deployed with `mvraspi copyparty-sfx.py`. |
| **Telegram bot** | Alerts and reports | See *Monitoring scripts* below. |

---

## Monitoring scripts

Live in `~/scripts` on the Pi (pushed with `mvraspi scripts` from the laptop).

### `login_checker.sh`
Tails `journalctl -u ssh` and sends a Telegram message on every failed **and**
successful SSH login, with user, IP and time. Run it as a systemd service so it
survives reboots.

### `statistics.sh`
Daily report to Telegram: Pi-hole query totals and block rate for the last 24 h from
`/etc/pihole/pihole-FTL.db`, the top three blocked domains, plus CPU temperature
(`vcgencmd measure_temp`), disk and RAM usage. Sources `~/.bash_functions` for the
`tg` helper, so that file needs to exist on the Pi too.

### `auto_update.sh`
Weekly: `apt update && upgrade`, `pihole -up`, `pihole -g` to refresh blocklists, a
Telegram notification, then `sudo reboot`.

> **Before committing this one:** `auto_update.sh` currently has a Telegram bot token
> and chat ID hardcoded in the `curl` call. `login_checker.sh` already reads them from
> variables at the top. Pull the token out into `~/.config/tg_token` (the same file the
> laptop uses) and source it, so no token ends up in this repo. See *Secrets* below.

---

## Hosting the website

Nginx serves `/var/www/robert_site`, published through a Cloudflare Tunnel — no router
port forwarding, the home IP stays hidden, and the tunnel survives a dynamic IP change.

Deploy from the laptop with the `website` function in `dotfiles/.bash_functions`:

```bash
website     # rsync -avz --delete ~/Documents/robert_site/ → /var/www/robert_site
```

### Installing cloudflared

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null

echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list

sudo apt update && sudo apt install cloudflared
```

### Creating the tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create raspi-website
cloudflared tunnel route dns raspi-website your-domain.example
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL-ID>
credentials-file: /home/robert/.cloudflared/<TUNNEL-ID>.json

ingress:
  - hostname: your-domain.example
    service: http://localhost:80
  - service: http_status:404
```

### Nginx site

`/etc/nginx/sites-available/your-domain.example`:

```nginx
server {
    listen 80;
    server_name localhost;

    root /var/www/your-domain.example;
    index index.html;

    access_log off;
    error_log /var/log/nginx/error.log crit;
}
```

---

## Navidrome

```bash
sudo apt update && sudo apt install ffmpeg

sudo mkdir -p /opt/navidrome /var/lib/navidrome
sudo useradd -r -s /bin/false navidrome
sudo chown -R navidrome:navidrome /opt/navidrome /var/lib/navidrome

cd /tmp
wget https://github.com/navidrome/navidrome/releases/download/v0.xx.x/navidrome_0.xx.x_linux_arm64.tar.gz
sudo tar -xvzf navidrome_0.xx.x_linux_arm64.tar.gz -C /opt/navidrome/
sudo chown -R navidrome:navidrome /opt/navidrome
```

`/var/lib/navidrome/navidrome.toml`:

```toml
MusicFolder = "/home/robert/Music"
DataFolder = "/var/lib/navidrome"
LogLevel = "info"
TranscodingCacheSize = 0
```

### Filling the library

`Music/download_list.sh` reads every `*.csv` playlist export in `~/Music` (Spotify
export format — track name in column 2, artist in column 4), then pulls each track with
`yt-dlp`, falling back from YouTube to SoundCloud. It uses `--no-overwrites`, so it
resumes where it left off. Anything it cannot find is appended to `failed_tracks.txt`.

Current playlists: `90shappyhardcore`, `christelijk`, `Corpshitjes`.

---

## Backups

`~/raspi-backup` on the laptop holds what would be painful to lose:

```
configs/          WireGuard client profiles (laptop, both phones, iPad)
pivpnbackup/      pivpn -bk archive, e.g. 20260102-151632-pivpnwgbackup.tgz
scripts/          login_checker.sh, statistics.sh, auto_update.sh
Music/            playlist CSVs, download_list.sh, downloaded audio
```

Refresh the PiVPN side with `pivpn -bk` on the Pi and copy the archive down.

---

## Secrets

**Nothing in this list belongs in this repository.** Same policy as the laptop —
see `docs/SECRETS.md` in `dotfiles`.

| Secret | Where it lives | How to replace it |
|---|---|---|
| Telegram bot token + chat ID | `~/.config/tg_token` on the Pi | BotFather. `auto_update.sh` currently hardcodes it — fix before committing. |
| WireGuard client profiles | `~/raspi-backup/configs/*.conf`, `/etc/wireguard` on the Pi | `pivpn -a` to re-issue |
| PiVPN backup archive | `~/raspi-backup/pivpnbackup/*.tgz` | `pivpn -bk` |
| Cloudflare tunnel credentials | `~/.cloudflared/<TUNNEL-ID>.json` | `cloudflared tunnel login` |
| Pi-hole admin password | Pi-hole's own config | `docker exec -it pihole pihole setpassword` |
| SSH host + user keys | `/etc/ssh`, `~/.ssh` | Regenerate; add the laptop's public key |

A `.gitignore` guarding `*.conf`, `*.tgz`, `tg_token` and key files is worth adding
before the first real commit.

---

## Hardware wishlist

- Raspberry Pi 5, 16 GB
- Official power supply, active cooling, proper case
- NVMe HAT so it boots from SSD instead of the SD card
- USB hub for extra SSDs
- Or: a real NAS with RAID, if storage reliability starts to matter

## Services worth adding

- **Kavita** — books (epub, pdf, cbr)
- **Jellyfin** — video
- **Immich** — photos (currently runs on the laptop against an external SSD, see
  `immichup` / `immichstatus` in `dotfiles/.bash_functions`)
- **Vaultwarden** — passwords, with care

## Links

- Pi-hole — https://pi-hole.net/
- PiVPN — https://www.pivpn.io/
- Navidrome — https://www.navidrome.org/docs/installation/linux/
- Raspotify — https://dtcooper.github.io/raspotify/ (Spotify Connect endpoint, pairs
  well with an audio HAT)
