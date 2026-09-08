#!/bin/bash
# Prints an HTML-formatted Pi-hole + system report to stdout.
# Called by commands/pihole.py — no Telegram sending logic in here anymore.
set -euo pipefail

DB=/etc/pihole/pihole-FTL.db
TIME_WINDOW="-1 day"

TOTAL=$(sqlite3 "$DB" "SELECT count(*) FROM queries WHERE timestamp >= strftime('%s','now','$TIME_WINDOW');")
BLOCKED=$(sqlite3 "$DB" "SELECT count(*) FROM queries WHERE status IN (1,4,5) AND timestamp >= strftime('%s','now','$TIME_WINDOW');")

if [ "$TOTAL" -eq 0 ]; then
  PERCENT=0
else
  PERCENT=$(echo "scale=2; (100 * $BLOCKED) / $TOTAL" | bc)
fi

TOP_BLOCKED=$(sqlite3 "$DB" "SELECT domain, count(domain) FROM queries WHERE status IN (1,4,5) AND timestamp >= strftime('%s','now','$TIME_WINDOW') GROUP BY domain ORDER BY count(domain) DESC LIMIT 3;" \
  | awk -F'|' '{printf "🚫 %-18s (%s)\n", $1, $2}')

TEMP=$(vcgencmd measure_temp | grep -oP '\d+\.\d+')
DISK=$(df -h / | awk 'NR==2 {print $5}')
RAM=$(free | grep Mem | awk '{printf "%.1f%%", $3/$2 * 100}')
LOAD1=$(cut -d ' ' -f1 /proc/loadavg)
CORES=$(nproc)
CPU_PERCENT=$(echo "scale=1; ($LOAD1 / $CORES) * 100" | bc)

# Simple traffic-light so it's obvious at a glance
if (( $(echo "$CPU_PERCENT > 80" | bc -l) )); then
  CPU_ICON="🔴"
elif (( $(echo "$CPU_PERCENT > 50" | bc -l) )); then
  CPU_ICON="🟡"
else
  CPU_ICON="🟢"
fi

cat << EOF
<b>🛡️ PI-HOLE & SYSTEM REPORT</b>
---------------------------------------
📊 <b>Pi-hole Stats (24h):</b>
• Total Queries: <code>$TOTAL</code>
• Ads Blocked:   <code>$BLOCKED</code>
• Block Rate:    <code>$PERCENT%</code>

🧱 <b>Top Blocked:</b>
<pre>$TOP_BLOCKED</pre>

🌡️ <b>System Health:</b>
• CPU Temp:  <code>$TEMP °C</code>
• CPU Load:  <code>$LOAD1 / $CORES cores (${CPU_PERCENT}%)</code> $CPU_ICON
• SD Usage:  <code>$DISK</code>
• RAM Usage: <code>$RAM</code>
EOF
