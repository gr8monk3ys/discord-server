#!/bin/sh
# Sets Front Desk up on a Linux VPS that already runs Docker (docs/HOSTING.md, "The live VPS").
# Run as root; safe to run again. It NEVER starts the bot and never enables the timers: starting
# them is the last step of the cut-over, because two bots must never share one token.
#
#   sh install.sh [branch]      branch to check out on the first run (default: main)
#
# Everything lives under /srv/front-desk and belongs to the system user `frontdesk`, whose uid is
# the same as the container's bot user, so the bot's files stay readable by the host's timers
# without putting anyone in the docker group.
set -eu

ROOT=/srv/front-desk
REPO=$ROOT/repo
BOT_USER=frontdesk
BOT_UID=10001   # the uid in the Dockerfile
ORIGIN=https://github.com/gr8monk3ys/discord-server.git
BRANCH=${1:-main}

as_bot() { runuser -u "$BOT_USER" -- "$@"; }

if ! getent passwd "$BOT_USER" >/dev/null; then
    useradd --system --uid "$BOT_UID" --home-dir "$ROOT" --no-create-home \
        --user-group --shell /usr/sbin/nologin "$BOT_USER"
fi
install -d -o "$BOT_USER" -g "$BOT_USER" -m 750 "$ROOT"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 700 "$ROOT/.ssh"

cd "$ROOT"
if [ ! -d "$REPO/.git" ]; then
    as_bot git clone --quiet --branch "$BRANCH" "$ORIGIN" "$REPO"
fi

# The database lives in the checkout's gitignored bot/data, so that watchdog.py and
# publish_site_data.py find it the same way they do on any other host. docker-compose.yml reads
# this setting from the repo root's .env (gitignored; not the secrets file, that is server/.env).
printf 'FRONT_DESK_DATA=./bot/data\n' | as_bot tee "$REPO/.env" >/dev/null
as_bot mkdir -p "$REPO/bot/data"

# publish_site_data.py pushes over SSH with this key. Its public half goes on GitHub by hand, as
# a deploy key with write access on this repository only (the checkout itself pulls over https).
if [ ! -f "$ROOT/.ssh/id_ed25519" ]; then
    as_bot ssh-keygen -q -t ed25519 -N '' -C "front-desk-site-data" -f "$ROOT/.ssh/id_ed25519"
fi
# GitHub's host keys, from its API over TLS rather than trusting whatever answers on port 22.
curl -fsSL https://api.github.com/meta \
    | python3 -c 'import json,sys; print("\n".join("github.com " + k for k in json.load(sys.stdin)["ssh_keys"]))' \
    | as_bot tee "$ROOT/.ssh/known_hosts" >/dev/null

install -m 644 "$REPO"/deploy/vps/front-desk-*.service "$REPO"/deploy/vps/front-desk-*.timer /etc/systemd/system/
systemctl daemon-reload

cd "$REPO"
docker compose build

[ -f server/.env ] || echo "Next: copy server/.env here (mode 600, owner $BOT_USER). It is never in git or the image."
echo "Deploy key to add on GitHub: $(cat "$ROOT/.ssh/id_ed25519.pub")"
echo "Not started. Finish with the cut-over checklist in docs/HOSTING.md."
