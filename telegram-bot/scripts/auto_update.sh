#!/bin/bash
# Runs system + Pi-hole updates, notifies via the Telegram bot, then reboots.
# Triggered by: the existing Monday 3am cron job, AND on-demand from the
# bot's /update command (which confirms with a button before calling this).
#
# Secrets (BOT_TOKEN, ALLOWED_CHAT_ID) are read from the bot's .env so
# there's a single place they're managed, instead of being duplicated here.

set -uo pipefail  # no -e: we want the log + notification steps to run
                   # even if a package step has a non-fatal hiccup

ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
else
  echo "WARNING: $ENV_FILE not found, notification will be skipped" >&2
fi

LOGDIR="/home/robert/telegram-bot/scripts/logs"
LOGFILE="$LOGDIR/auto_update.log"
mkdir -p "$LOGDIR"

{
  echo "=== Update started at $(date) ==="

  # 1. Update Raspberry Pi OS (System Packages)
  sudo apt-get update && sudo apt-get upgrade -y
  sudo apt-get autoremove -y
  sudo apt-get autoclean

  # 2. Update Pi-hole Core (Web Interface, FTL, Core)
  sudo pihole -up

  # 3. Update Pi-hole Gravity (Blocklists)
  # Pi-hole also installs its own cron for this (usually Sundays), but
  # running it here too keeps it in lockstep with the OS update.
  sudo pihole -g

  echo "Update completed at $(date)"
  echo "---------------------------------------------"
} >> "$LOGFILE" 2>&1

if [ -n "${BOT_TOKEN:-}" ] && [ -n "${ALLOWED_CHAT_ID:-}" ]; then
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
       -d "chat_id=${ALLOWED_CHAT_ID}" \
       -d "text=✅ Weekly update completed successfully. Rebooting now." \
       > /dev/null
fi

sudo reboot
