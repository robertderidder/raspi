#functions
set -a
source /home/robert/raspi/telegram-bot/.env
set +a

#HELP: tg - Send a message via telegram
tg() {
    # Read from stdin if no argument is provided
    local MSG="${1:-$(cat -)}"

    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
        -d "chat_id=$ALLOWED_CHAT_ID" \
        --data-urlencode "text=$MSG" > /dev/null
}
