#!/bin/sh
# Posts to WATCHDOG_WEBHOOK_URL (#mod-log) when a SpotifyForge job starts failing and once when it
# works again, not on every run. Each job's ExecStopPost runs it, with systemd's SERVICE_RESULT,
# EXIT_STATUS and INVOCATION_ID, so a timeout or a kill counts as well as a bad exit:
#
#   front-desk-spotify-alert.sh <unit> <label> <failures in a row before posting>
#
# A skipped hour (exit 75, the other job holds the lock) is a success to systemd, so it's quiet.
# reflow and describe exit 0 when only some playlists fail, so a clean exit still reads the run's log
# for their "N could not be updated" and "N failed" lines, and posts them each time.
set -u

unit=$1
label=$2
after=$3
REPO=/srv/front-desk/repo
STATE=/var/lib/front-desk-spotify-alert
failures=$STATE/$unit.failures
alerted=$STATE/$unit.alerted

alert() {
    url=$(sed -n 's/^WATCHDOG_WEBHOOK_URL=//p' "$REPO/server/.env" | tail -n 1 | tr -d '"')
    [ -n "$url" ] || return 0
    # The URL is a secret: through --config, so it stays out of the process list.
    printf 'url = "%s"\n' "$url" | curl -fsS --max-time 20 --config - -o /dev/null \
        -H 'Content-Type: application/json' \
        --data "{\"content\":\"$1\",\"allowed_mentions\":{\"parse\":[]}}" ||
        { echo "front-desk-spotify-alert: couldn't post to the webhook" >&2; return 1; }
}

# This run's output, joined onto one line and safe to put in the JSON above.
run_lines() {
    journalctl "_SYSTEMD_INVOCATION_ID=${INVOCATION_ID:-none}" -o cat --no-pager |
        sed -n "$1" | sed 's/ *│ *$//' | tr -d '"\\' | tr '\n' ';' | sed 's/;$//; s/;/; /g'
}

mkdir -p "$STATE"
check="Check: journalctl -u $unit -n 60"

if [ "${SERVICE_RESULT:-}" = success ]; then
    if [ -e "$alerted" ]; then
        alert "SpotifyForge's $label works again." && rm -f "$alerted"
    fi
    rm -f "$failures"
    partial=$(run_lines 's/^│ \([0-9][0-9]* \(could not be updated\|failed\)\)/\1/p')
    [ -z "$partial" ] || alert "SpotifyForge's $label finished, but some playlists didn't update: $partial. $check"
    exit 0
fi

count=$(($(cat "$failures" 2>/dev/null || echo 0) + 1))
echo "$count" >"$failures"
if [ "$count" -ge "$after" ] && [ ! -e "$alerted" ]; then
    why="${SERVICE_RESULT:-unknown}, status ${EXIT_STATUS:-unknown}"
    steps=$(run_lines "s/^step '\(.*\)' exited \([0-9]*\)$/\1 exited \2/p")
    [ -z "$steps" ] || why="$why: $steps"
    alert "SpotifyForge's $label FAILED, $count in a row ($why). $check" && touch "$alerted"
fi
exit 0
