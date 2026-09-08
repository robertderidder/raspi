#!/bin/bash
# Watches SSH logins and pushes a Telegram alert for each one. Intended to
# run as a long-lived systemd service -- it follows the journal indefinitely.
#
# There are two independent ways onto this box, and they log to different
# units:
#
#   sshd        Direct connections to port 22 -- from the LAN, or from a
#               tailnet client not using Tailscale SSH.  -> ssh.service
#
#   tailscaled  With `tailscale set --ssh`, tailscaled intercepts port 22 on
#               the tailnet address and runs the session itself. sshd never
#               sees the connection and logs nothing.      -> tailscaled.service
#
# Watching only `ssh` silently misses every Tailscale SSH login. That is
# exactly what happened here: --ssh was enabled and the alerts stopped dead,
# with no error anywhere, because sshd was simply no longer involved.

set -uo pipefail  # no -e: one bad match on a weird log line shouldn't kill the watcher

ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
else
  echo "WARNING: $ENV_FILE not found, notifications will be skipped" >&2
fi

NODE=$(hostname)
LOGDIR="/home/robert/telegram-bot/scripts/logs"
LOGFILE="$LOGDIR/login_checker.log"
mkdir -p "$LOGDIR"

send_alert() {
  local message="$1"
  echo "$(date '+%F %T') | $message" >> "$LOGFILE"
  if [ -n "${BOT_TOKEN:-}" ] && [ -n "${ALLOWED_CHAT_ID:-}" ]; then
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
         -d "chat_id=${ALLOWED_CHAT_ID}" \
         --data-urlencode "text=$message" \
         > /dev/null
  fi
}

get_tailscale_device() {
  local ip="$1"
  # Only bother looking up Tailscale's CGNAT range (100.64.0.0/10) --
  # skip the lookup entirely for anything else to avoid a slow/failing call.
  if [[ "$ip" =~ ^100\. ]]; then
    local info
    info=$(tailscale whois --json "$ip" 2>/dev/null)
    if [ -n "$info" ] && command -v jq >/dev/null 2>&1; then
      local name
      name=$(echo "$info" | jq -r '.Node.Name // empty' | sed 's/\.$//')
      if [ -n "$name" ]; then
        echo "$name"
        return
      fi
    fi
  fi
  echo "unknown / non-Tailscale"
}

# Tailscale logs the connection (with the source IP) and the authorisation
# decision on two separate lines, tied together only by the session id. Hold
# the first until the second arrives, so an alert is only sent for a session
# that was actually granted -- and still carries the IP.
declare -A SESS_IP

# Anchor every tailscaled pattern on the "ssh-session(sess-...):" prefix.
# tailscaled also echoes the *command text* of each session into the journal,
# so an unanchored match would let anyone trigger a fake alert just by
# running `ssh raspi 'echo access granted to root'`.
SESS='ssh-session\((sess-[^)]+)\)'

# Pre-filter in grep rather than in the loop: tailscaled is chatty, and this
# is a 4-core Zero 2 W.
journalctl -u ssh -u tailscaled -n 0 -f 2>/dev/null \
  | grep --line-buffered -E "Failed password|Accepted (password|publickey)|ssh-session\(sess-[^)]+\): (handling new SSH connection|access granted|access denied|failed)" \
  | while read -r LINE; do
  TIME=$(date +%T)

  # ---- sshd: direct connections to port 22 --------------------------------
  if [[ "$LINE" == *"Failed password"* ]]; then
    IP=$(echo "$LINE" | grep -oE "([0-9]{1,3}\.){3}[0-9]{1,3}" | head -1)
    USER=$(echo "$LINE" | grep -oP "for (invalid user )?\K\S+")
    send_alert "⚠️ Failed login attempt on $NODE
Path: sshd (port 22)
User: $USER
IP: $IP
Device: $(get_tailscale_device "$IP")
Time: $TIME"
    continue
  fi

  if [[ "$LINE" =~ Accepted\ (password|publickey) ]]; then
    METHOD="${BASH_REMATCH[1]}"
    IP=$(echo "$LINE" | grep -oE "([0-9]{1,3}\.){3}[0-9]{1,3}" | head -1)
    USER=$(echo "$LINE" | grep -oP "for (invalid user )?\K\S+")
    send_alert "✅ Successful login on $NODE
Path: sshd (port 22)
User: $USER
Method: $METHOD
IP: $IP
Device: $(get_tailscale_device "$IP")
Time: $TIME"
    continue
  fi

  # ---- Tailscale SSH: handled by tailscaled, invisible to sshd ------------
  if [[ "$LINE" =~ $SESS:\ handling\ new\ SSH\ connection\ from\ [^\ ]+\ \(([^\)]+)\)\ to\ ssh-user ]]; then
    SESS_IP["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
    # Bound the map: a session that never resolves must not leak memory in a
    # process that is meant to run for months.
    if [ "${#SESS_IP[@]}" -gt 64 ]; then unset "SESS_IP[${!SESS_IP[*]%% *}]"; fi
    continue
  fi

  if [[ "$LINE" =~ $SESS:\ access\ granted\ to\ ([^\ ]+)\ as\ ssh-user\ \"([^\"]+)\" ]]; then
    SID="${BASH_REMATCH[1]}"; WHO="${BASH_REMATCH[2]}"; AS_USER="${BASH_REMATCH[3]}"
    IP="${SESS_IP[$SID]:-unknown}"
    unset "SESS_IP[$SID]"
    send_alert "✅ Successful login on $NODE
Path: Tailscale SSH
Identity: $WHO
User: $AS_USER
IP: $IP
Device: $(get_tailscale_device "$IP")
Time: $TIME"
    continue
  fi

  if [[ "$LINE" =~ $SESS:\ (access\ denied|failed) ]]; then
    SID="${BASH_REMATCH[1]}"
    IP="${SESS_IP[$SID]:-unknown}"
    unset "SESS_IP[$SID]"
    send_alert "⚠️ Rejected Tailscale SSH attempt on $NODE
IP: $IP
Device: $(get_tailscale_device "$IP")
Time: $TIME"
    continue
  fi
done
