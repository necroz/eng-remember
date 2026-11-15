#!/bin/sh
# Nightly pg_dump with rotation, plus the crond bootstrap for the backup service.
#
# Runs in postgres:17-alpine, which already ships pg_dump and BusyBox crond, so
# the service adds no packages and nothing on the Python side.
#
#   backup.sh daemon   install the crontab and run crond in the foreground
#   backup.sh dump     take one dump now and prune the old ones
set -eu

# crond starts jobs with a bare environment, so the daemon mode persists the
# container's settings in this file first and the cron job sources it back.
if [ -f /etc/backup.env ]; then
    . /etc/backup.env
fi

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

PGHOST="${PGHOST:-db}"
PGPORT="${PGPORT:-5432}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
CRONTAB_SOURCE=/etc/backup.crontab
CRONTAB_TARGET=/etc/crontabs/root
ENV_FILE=/etc/backup.env

export PGPASSWORD="$POSTGRES_PASSWORD"

prune() {
    # Names carry the date, so the newest N by name are the newest N by time.
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'eng-remember-*.dump' \
        | sort -r \
        | tail -n "+$((BACKUP_KEEP_DAYS + 1))" \
        | while read -r old; do
            rm -f -- "$old"
            echo "backup: removed $old"
        done
}

dump() {
    case "$BACKUP_KEEP_DAYS" in
        '' | *[!0-9]* | 0)
            echo "backup: BACKUP_KEEP_DAYS must be a positive integer, got '$BACKUP_KEEP_DAYS'" >&2
            exit 1
            ;;
    esac

    mkdir -p "$BACKUP_DIR"
    # One dump per day: a second run the same day replaces it, and the mv is
    # atomic, so a reader never sees a half-written file under the final name.
    target="$BACKUP_DIR/eng-remember-$(date -u +%F).dump"
    partial="$target.part"
    # Custom format (-Fc) is compressed and is what pg_restore expects.
    pg_dump --host="$PGHOST" --port="$PGPORT" --username="$POSTGRES_USER" \
        --dbname="$POSTGRES_DB" --format=custom --file="$partial"
    mv "$partial" "$target"
    echo "backup: wrote $target ($(wc -c <"$target") bytes)"
    prune
}

write_env() {
    : >"$ENV_FILE"
    for name in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB PGHOST PGPORT \
        BACKUP_DIR BACKUP_KEEP_DAYS; do
        eval "value=\${$name:-}"
        [ -n "$value" ] || continue
        # Single-quote for the shell, escaping embedded quotes, so a password
        # with spaces or symbols survives the round-trip.
        quoted=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
        printf "%s='%s'\n" "$name" "$quoted" >>"$ENV_FILE"
    done
    chmod 600 "$ENV_FILE"
}

daemon() {
    if [ ! -f "$CRONTAB_SOURCE" ]; then
        echo "backup: $CRONTAB_SOURCE is missing" >&2
        exit 1
    fi
    write_env
    mkdir -p "$(dirname "$CRONTAB_TARGET")"
    cp "$CRONTAB_SOURCE" "$CRONTAB_TARGET"
    chmod 600 "$CRONTAB_TARGET"
    echo "backup: schedule (UTC):"
    grep -v '^#' "$CRONTAB_SOURCE" | grep -v '^$' || true
    # -f keeps crond in the foreground, so the container's restart policy and
    # log collection still apply to it.
    exec crond -f -l 2
}

case "${1:-daemon}" in
    daemon) daemon ;;
    dump) dump ;;
    *)
        echo "usage: $0 [daemon|dump]" >&2
        exit 2
        ;;
esac