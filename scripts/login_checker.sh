#!/bin/bash
# Watches sshd logins and pushes a Telegram alert for each attempt
# (failed or successful). Intended to run as a long-lived systemd
# service — it follows the journal indefinitely.

set -uo pipefail  # no -e: one bad grep on a weird log line shouldn't kill the watcher

ENV_FILE="/home/robert/telegram-bot/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
else
  echo "WARNING: $ENV_FILE not found, notifications will be skipped" >&2
fi

NODE=$(hostname)
LOGDIR="/home/robert/scripts/logs"
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
  # Only bother looking up Tailscale's CGNAT range (100.64.0.0/10) —
  # skip the lookup entirely for anything else to avoid a slow/failing call.
  if [[ "$ip" =~ ^100\. ]]; then
    local info
    info=$(tailscale whois --json "$ip" 2>/dev/null)
    if [ -n "$info" ] && command -v jq >/dev/null 2>&1; then
      local name login
      name=$(echo "$info" | jq -r '.Node.Name // empty' | sed 's/\.$//')
      login=$(echo "$info" | jq -r '.UserProfile.LoginName // empty')
      if [ -n "$name" ]; then
        echo "$name${login:+ ($login)}"
        return
      fi
    fi
  fi
  echo "unknown / non-Tailscale"
}

journalctl -u ssh -n 0 -f | while read -r LINE; do
  TIME=$(date +%T)

  if echo "$LINE" | grep -q "Failed password"; then
    IP=$(echo "$LINE" | grep -oE "([0-9]{1,3}\.){3}[0-9]{1,3}" | head -1)
    USER=$(echo "$LINE" | grep -oP "for (invalid user )?\K\S+")
    DEVICE=$(get_tailscale_device "$IP")
    send_alert "⚠️ Failed login attempt on $NODE
User: $USER
IP: $IP
Device: $DEVICE
Time: $TIME"
  fi

  if echo "$LINE" | grep -qE "Accepted (password|publickey)"; then
    METHOD=$(echo "$LINE" | grep -oP "Accepted \K\S+")
    IP=$(echo "$LINE" | grep -oE "([0-9]{1,3}\.){3}[0-9]{1,3}" | head -1)
    USER=$(echo "$LINE" | grep -oP "for (invalid user )?\K\S+")
    DEVICE=$(get_tailscale_device "$IP")
    send_alert "✅ Successful login on $NODE
User: $USER
Method: $METHOD
IP: $IP
Device: $DEVICE
Time: $TIME"
  fi
done
