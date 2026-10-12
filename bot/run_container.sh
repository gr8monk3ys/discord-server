#!/bin/sh
# Container entry point for Front Desk (see docs/HOSTING.md). The Linux stand-in for
# run_bot.bat plus the Windows watchdog task.
#
#   run_container.sh            run the bot (what the image does by default)
#   run_container.sh health     exit 0 when bot/data/heartbeat is fresh (Docker HEALTHCHECK)
#   run_container.sh CMD ARGS   run something else in the image, e.g.
#                               python /app/server/migrate_host.py unpack /tmp/a.tar.gz
#
# Docker restarts a container that exits, but not one that is merely "unhealthy". So while
# the bot runs, a small background loop watches the heartbeat and stops a bot that has gone
# quiet (hung, or stuck in a reconnect loop); the restart policy then starts a fresh one.
# Same thresholds as server/watchdog.py: stale after 10 minutes, at most 3 restarts an hour.
set -u

BOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_DIR="$BOT_DIR/data"
SEED_DIR="${SEED_DIR:-/app/seed}"
HEARTBEAT="$DATA_DIR/heartbeat"
RESTARTS="$DATA_DIR/container_restarts"

HEALTH_MAX_AGE="${HEALTH_MAX_AGE:-180}"   # healthcheck: heartbeat older than this = unhealthy
STALE_AFTER="${STALE_AFTER:-600}"         # monitor: heartbeat older than this = stop the bot
START_GRACE="${START_GRACE:-600}"         # monitor: give a fresh bot this long to connect
CHECK_EVERY="${CHECK_EVERY:-60}"
MAX_RESTARTS="${MAX_RESTARTS:-3}"         # per hour
STOP_WAIT="${STOP_WAIT:-20}"              # seconds between the polite stop and the hard one

heartbeat_age() {
    # Seconds since the heartbeat file was last replaced; a huge number when it is missing.
    modified=$(stat -c %Y "$HEARTBEAT" 2>/dev/null) || { echo 999999999; return; }
    echo $(( $(date +%s) - modified ))
}

recent_restarts() {
    # How many monitor restarts happened in the last hour (and forget older ones).
    cutoff=$(( $(date +%s) - 3600 ))
    count=0
    kept=""
    if [ -f "$RESTARTS" ]; then
        while read -r stamp; do
            case "$stamp" in ''|*[!0-9]*) continue ;; esac
            if [ "$stamp" -gt "$cutoff" ]; then
                count=$(( count + 1 ))
                kept="$kept$stamp
"
            fi
        done < "$RESTARTS"
    fi
    printf '%s' "$kept" > "$RESTARTS" 2>/dev/null || true
    echo "$count"
}

monitor() {
    bot_pid=$1
    sleep "$START_GRACE"
    while kill -0 "$bot_pid" 2>/dev/null; do
        age=$(heartbeat_age)
        if [ "$age" -gt "$STALE_AFTER" ]; then
            if [ "$(recent_restarts)" -ge "$MAX_RESTARTS" ]; then
                echo "run_container: heartbeat is ${age}s old, but already restarted $MAX_RESTARTS times this hour; waiting" >&2
            else
                echo "run_container: heartbeat is ${age}s old; stopping the bot so Docker restarts it" >&2
                date +%s >> "$RESTARTS" 2>/dev/null || true
                kill -INT "$bot_pid" 2>/dev/null
                sleep "$STOP_WAIT"
                kill -KILL "$bot_pid" 2>/dev/null
                return
            fi
        fi
        sleep "$CHECK_EVERY"
    done
}

case "${1:-bot}" in
    health)
        age=$(heartbeat_age)
        if [ "$age" -le "$HEALTH_MAX_AGE" ]; then
            exit 0
        fi
        echo "heartbeat is ${age}s old (limit ${HEALTH_MAX_AGE}s)"
        exit 1
        ;;
    bot)
        mkdir -p "$DATA_DIR" || { echo "run_container: $DATA_DIR is not writable" >&2; exit 1; }
        # The data volume hides the question and poll banks that ship in the repo: refresh them.
        for seed in "$SEED_DIR"/*.txt; do
            [ -f "$seed" ] && cp -f "$seed" "$DATA_DIR/" 2>/dev/null
        done
        cd "$BOT_DIR" || exit 1
        # `exec` keeps this PID for Python, so the monitor (started first) knows whom to stop
        # and `docker stop` reaches the bot directly.
        monitor $$ &
        exec python main.py
        ;;
    *)
        exec "$@"
        ;;
esac
