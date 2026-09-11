#!/bin/bash
# Daily Pi-hole Teleporter export, unpacked into this repo and committed.
#
#   pihole_backup.sh              export, scrub, commit, push
#   pihole_backup.sh --dry-run    show what would change, commit nothing
#   pihole_backup.sh --no-push    commit locally only
#   pihole_backup.sh --no-notify  no Telegram message (used by /backup, which
#                                 reports the result itself)
#
# Run daily by pihole-backup.timer, and on demand from the bot's /backup.
#
# WHY THE ARCHIVE IS UNPACKED RATHER THAN STORED AS A .zip
#
#   A zip is opaque and recompresses to different bytes every run, so git cannot
#   delta it: a daily commit would add the whole archive again, forever, and you
#   could never see *what* changed. Unpacked, a new blocklist is a one-line diff
#   and the backup is reviewable -- which is how you find out it has been broken
#   for a month before you need it, rather than after.
#
# WHY SECRETS ARE SCRUBBED
#
#   pihole.toml carries webserver.api.pwhash (the admin password hash) and the
#   TOTP secret. This repo is private, but it has leaked a credential once, and
#   git history is permanent -- see docs/SECRETS.md in the parent repo. Those
#   values are emptied here before anything is staged, and SCRUBBED.md records
#   exactly what was emptied. The cost is one extra step on restore:
#   `sudo pihole setpassword`. The password is the one thing in the export you
#   can reconstruct from memory, so it is the cheapest thing to leave out.
#
# The secret scan before committing is a backstop for the scrubber missing a key
# name nobody anticipated. If it ever fires, the commit is abandoned rather than
# guessed at.
set -uo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
BOT_DIR="$(dirname "$SCRIPT_DIR")"

# Resolve the real repo root. ~/telegram-bot is a stow symlink into the repo, so
# a plain `cd ..` lands in $HOME instead. readlink -f above gives the physical
# path; git then reports the checkout it actually belongs to.
REPO="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO" ]; then
    echo "FATAL: $SCRIPT_DIR is not inside a git checkout" >&2
    exit 1
fi

DEST="$REPO/pihole-backup"
LOGDIR="$BOT_DIR/scripts/logs"
LOGFILE="$LOGDIR/pihole_backup.log"
LOCKFILE="/tmp/pihole_backup.lock"

# Anything bigger than this is reported and left out rather than committed. A
# multi-megabyte binary (gravity.db, if the archive turns out to carry a full
# one) would bloat the repo daily for data that `pihole -g` re-downloads anyway.
MAX_FILE_KB="${PIHOLE_BACKUP_MAX_FILE_KB:-5120}"

DRY=0
PUSH=1
NOTIFY=1
for arg in "$@"; do
    case "$arg" in
        --dry-run)   DRY=1 ;;
        --no-push)   PUSH=0 ;;
        --no-notify) NOTIFY=0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$LOGDIR"
log() {
    printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOGFILE"
}

ENV_FILE="$BOT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

notify() {
    [ "$NOTIFY" -eq 1 ] || return 0
    [ -n "${BOT_TOKEN:-}" ] && [ -n "${ALLOWED_CHAT_ID:-}" ] || return 0
    curl -s -m 20 -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
         -d "chat_id=${ALLOWED_CHAT_ID}" \
         --data-urlencode "text=$1" > /dev/null || true
}

STAGING=""
cleanup() { [ -n "$STAGING" ] && rm -rf "$STAGING"; }
trap cleanup EXIT

fail() {
    log "FAILED: $*"
    notify "⚠️ Pi-hole backup failed: $*"
    exit 1
}

# Only one of these at a time -- the timer and an on-demand /backup can collide.
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    log "another backup is already running; skipping"
    exit 0
fi

# --- refuse to share a dirty index -----------------------------------------
# An unattended job that commits whatever happened to be half-staged is a good
# way to lose work in progress. Same rule as bin/autocommit.sh in the parent.
if ! git -C "$REPO" diff --cached --quiet; then
    git -C "$REPO" diff --cached --name-only | sed 's/^/    /' | tee -a "$LOGFILE"
    fail "the repo has staged changes; commit or reset them first"
fi

# --- export -----------------------------------------------------------------
command -v pihole-FTL >/dev/null 2>&1 || fail "pihole-FTL not found (is Pi-hole v6 installed?)"

STAGING="$(mktemp -d -t pihole-backup-XXXXXX)"
log "exporting teleporter archive"

# sudo: the export reads /etc/pihole, which is root-owned. The archive lands in
# the cwd, so run it from the staging directory rather than wherever we started.
if ! (cd "$STAGING" && sudo pihole-FTL --teleporter >>"$LOGFILE" 2>&1); then
    fail "pihole-FTL --teleporter returned an error (see $LOGFILE)"
fi
sudo chown -R "$(id -u):$(id -g)" "$STAGING" 2>/dev/null || true

ARCHIVE="$(find "$STAGING" -maxdepth 1 -name '*.zip' -type f | head -1)"
[ -n "$ARCHIVE" ] || fail "the export produced no .zip in $STAGING"
log "got $(basename "$ARCHIVE") ($(du -h "$ARCHIVE" | cut -f1))"

# --- unpack -----------------------------------------------------------------
# python3 rather than `unzip`: it is guaranteed present (Pi-hole and the bot both
# need it) and this avoids making the backup depend on an apt package that may
# not be installed on a freshly rebuilt Pi.
UNPACKED="$STAGING/unpacked"
mkdir -p "$UNPACKED"
python3 -m zipfile -e "$ARCHIVE" "$UNPACKED" \
    || fail "could not unpack $(basename "$ARCHIVE")"

file_count="$(find "$UNPACKED" -type f | wc -l)"
[ "$file_count" -gt 0 ] || fail "the archive unpacked to nothing"
log "unpacked $file_count file(s)"

# --- scrub ------------------------------------------------------------------
# Key names, not values: anything whose key looks credential-shaped has its
# value emptied. Empty is deliberate -- in pihole.toml an empty pwhash means
# "no password set", so an imported copy is in a known state rather than
# carrying a placeholder that looks like a hash.
SENSITIVE_KEYS='pwhash|app_pwhash|totp_secret|password|passwd|secret|api_?key|apikey|token'
SCRUB_REPORT="$UNPACKED/SCRUBBED.md"
scrubbed_lines=""

while IFS= read -r -d '' f; do
    # Text files only; a binary would be corrupted by sed and cannot be reviewed
    # in a diff anyway.
    if ! grep -Iq . "$f" 2>/dev/null; then continue; fi

    # -o with a pattern that stops at the `=` extracts the KEY and cannot
    # capture the value, whatever the line looks like. Matching the whole line
    # and trimming afterwards would put the secret one failed regex away from
    # being written into SCRUBBED.md, which is committed.
    keys="$(grep -ioE "^[[:space:]]*(${SENSITIVE_KEYS})[[:space:]]*=" "$f" 2>/dev/null \
            | sed -E 's/^[[:space:]]*//; s/[[:space:]]*=$//' | sort -u || true)"
    [ -n "$keys" ] || continue

    rel="${f#"$UNPACKED"/}"
    while IFS= read -r key; do
        [ -n "$key" ] && scrubbed_lines="${scrubbed_lines}- \`${rel}\` → \`${key}\`"$'\n'
    done <<< "$keys"
    # The delimiter must NOT be `|`: SENSITIVE_KEYS is an alternation full of
    # them, and they would close the s command early -- leaving the file
    # untouched while this loop still logged a successful scrub.
    sed -i -E "s#^([[:space:]]*(${SENSITIVE_KEYS})[[:space:]]*=[[:space:]]*).*#\1\"\"#I" "$f"
    log "scrubbed credentials from $rel"
done < <(find "$UNPACKED" -type f -print0)

{
    echo "# Emptied before committing"
    echo
    echo "Generated by pihole_backup.sh. Re-set with \`pihole setpassword\` after a restore."
    echo
    if [ -n "$scrubbed_lines" ]; then
        printf '%s' "$scrubbed_lines"
    else
        echo "Nothing matched the credential patterns this run. Either no password is"
        echo "set, or Pi-hole renamed a key and SENSITIVE_KEYS needs updating."
    fi
} > "$SCRUB_REPORT"

# --- size guard -------------------------------------------------------------
skipped=""
while IFS= read -r -d '' f; do
    kb=$(( $(stat -c %s "$f") / 1024 ))
    if [ "$kb" -gt "$MAX_FILE_KB" ]; then
        rel="${f#"$UNPACKED"/}"
        skipped="${skipped}${rel} (${kb} KB), "
        rm -f "$f"
        log "SKIPPED $rel -- ${kb} KB is over the ${MAX_FILE_KB} KB limit"
    fi
done < <(find "$UNPACKED" -type f -print0)

# --- move into the repo -----------------------------------------------------
# The destination is wiped first so that a file Pi-hole stopped exporting
# actually disappears from the repo instead of lingering as a stale backup.
mkdir -p "$DEST"
find "$DEST" -mindepth 1 -maxdepth 1 ! -name 'README.md' -exec rm -rf {} +
cp -a "$UNPACKED"/. "$DEST"/

# --- stage ------------------------------------------------------------------
# `-A` is safe here only because it is scoped to our own directory by the
# pathspec: it can add, modify and delete inside pihole-backup/ and can never
# reach anything else in the repo.
git -C "$REPO" add -A -- "$DEST"

if git -C "$REPO" diff --cached --quiet; then
    log "no change since the last backup"
    [ -n "$skipped" ] && notify "⚠️ Pi-hole backup: no changes, but skipped oversized ${skipped%, }"
    exit 0
fi

changed="$(git -C "$REPO" diff --cached --name-only | wc -l)"

# --- secret scan ------------------------------------------------------------
# The same expression bin/autocommit.sh uses in the parent repo. The scrubber
# above is keyed on names it knows; this catches a value that looks like a
# secret whatever its key is called.
SECRET_RE='(BEGIN [A-Z ]*PRIVATE KEY|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|(?i:password|passwd|secret|client_secret|api[_-]?key|pwhash)["'"'"']?\s*[=:]?\s*["'"'"']?(?=[^"'"'"'\s]*[0-9])[^"'"'"'\s]{8,})'

leak="$(git -C "$REPO" diff --cached -U0 -- "$DEST" | grep -E '^\+' | grep -vE '^\+\+\+' \
        | grep -aP "$SECRET_RE" | head -5 || true)"
if [ -n "$leak" ]; then
    printf '%s\n' "$leak" | sed 's/^/    /' >> "$LOGFILE"
    git -C "$REPO" reset -q -- "$DEST"
    fail "staged backup looks like it still contains a secret -- see $LOGFILE. The scrubber may need a new key name."
fi

if [ "$DRY" -eq 1 ]; then
    log "dry run, would commit $changed file(s):"
    git -C "$REPO" diff --cached --name-status -- "$DEST" | sed 's/^/    /' | tee -a "$LOGFILE"
    git -C "$REPO" reset -q -- "$DEST"
    exit 0
fi

# --- commit -----------------------------------------------------------------
name="$(git -C "$REPO" config user.name || true)"
mail="$(git -C "$REPO" config user.email || true)"
git -C "$REPO" \
    -c user.name="${name:-raspi}" \
    -c user.email="${mail:-robert@raspi}" \
    commit -q -m "pihole backup $(date '+%Y-%m-%d')" \
              -m "Teleporter export, credentials scrubbed. Automated by pihole-backup.timer." \
    || fail "git commit failed"

log "committed $(git -C "$REPO" rev-parse --short HEAD) ($changed file(s))"

summary="✅ Pi-hole backup: $changed file(s) changed, committed."
[ -n "$skipped" ] && summary="$summary
⚠️ Left out (too large): ${skipped%, }"

# --- push -------------------------------------------------------------------
if [ "$PUSH" -eq 0 ]; then
    log "--no-push, stopping"
    notify "$summary
(not pushed: --no-push)"
    exit 0
fi

if git -C "$REPO" push -q origin HEAD >>"$LOGFILE" 2>&1; then
    log "pushed to origin"
    notify "$summary Pushed."
else
    # A failed push is not a reason to lose the commit -- it is safe locally and
    # the next run carries it along.
    log "PUSH FAILED -- the commit is safe locally and will go out next run"
    notify "$summary
⚠️ Push failed — the commit is safe locally, will retry tomorrow."
    exit 1
fi
