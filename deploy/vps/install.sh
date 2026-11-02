#!/bin/sh
# Sets Front Desk up on a Linux VPS that already runs Docker (docs/HOSTING.md, "The live VPS").
# Run as root; safe to run again. It NEVER starts the bot and never enables the timers: starting
# them is the last step of the cut-over, because two bots must never share one token.
#
#   sh install.sh [branch]      branch to check out on the first run (default: main)
#
# Root runs Compose and installs the units from this checkout, so the checkout, its compose file,
# units and .env files belong to root. The system user `frontdesk` (the same uid as the container's
# bot user) owns only bot/data, which the container also writes to, and its own ~/.ssh. Root must
# not work on files in bot/data directly: a compromised bot could have left symlinks there.
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
install -d -o root -g "$BOT_USER" -m 750 "$ROOT"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 700 "$ROOT/.ssh"

if [ ! -d "$REPO/.git" ]; then
    git clone --quiet --branch "$BRANCH" "$ORIGIN" "$REPO"
    # Only on the first run, when bot/data holds nothing but the two tracked question banks.
    chown -Rh "$BOT_USER:$BOT_USER" "$REPO/bot/data"
fi

# Everything below trusts the checkout, so refuse one that anyone but root could have changed.
not_root=$(find "$REPO" -path "$REPO/bot/data" -prune -o ! -user root -print -quit)
if [ -n "$not_root" ]; then
    echo "Refusing to continue: $not_root is not owned by root, so this checkout can't be trusted." >&2
    exit 1
fi

# The database lives in the checkout's gitignored bot/data, so that watchdog.py and
# publish_site_data.py find it the same way they do on any other host. docker-compose.yml reads
# this setting from the repo root's .env (gitignored; not the secrets file, that is server/.env).
printf 'FRONT_DESK_DATA=./bot/data\n' > "$REPO/.env"
chmod 644 "$REPO/.env"

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

[ -f server/.env ] || echo "Next: copy server/.env here (mode 600, owner root). It is never in git or the image."
echo "Deploy key to add on GitHub: $(cat "$ROOT/.ssh/id_ed25519.pub")"
echo "Not started. Finish with the cut-over checklist in docs/HOSTING.md."
