#!/bin/sh
# Sends an encrypted copy of Front Desk's database off this machine, to the `backups` branch of a
# private GitHub repository (docs/HOSTING.md, "Off-site backups"). front-desk-backup.timer runs it
# once a day. It runs as root, because it needs Compose and the deploy key.
#
#   1. The container makes the copy and sends it through a pipe, so root opens nothing in bot/data
#      (a compromised bot could have left symlinks there). make_backup is the nightly backup's own
#      code: an online SQLite backup, integrity-checked before it is sent.
#   2. age encrypts it to a public key. The private key is not on this machine, so the VPS can write
#      backups but can't read them.
#   3. The newest $KEEP copies are pushed as one commit with no parent, and the older ones leave the
#      branch: PRIVACY.md says how long a deleted record can last in a backup, and the repository
#      stays small. A failure at any step leaves the branch as it was.
#
# It posts to WATCHDOG_WEBHOOK_URL (#mod-log) once when backups start failing and once when they
# work again, not every day.
#
# Settings are in /etc/front-desk-backup/settings: BACKUP_REMOTE and AGE_RECIPIENT, and optionally KEEP.
set -eu

REPO=/srv/front-desk/repo
CONF=/etc/front-desk-backup
STATE=/var/lib/front-desk-backup
KEEP=14

why='see the log'
ok=''
work=''

die() {
    why=$1
    echo "front-desk-backup: $1" >&2
    exit 1
}

alert() {
    url=$(sed -n 's/^WATCHDOG_WEBHOOK_URL=//p' "$REPO/server/.env" | tail -n 1 | tr -d '"')
    [ -n "$url" ] || return 0
    # The URL is a secret: through --config, so it stays out of the process list.
    printf 'url = "%s"\n' "$url" | curl -fsS --max-time 20 --config - -o /dev/null \
        -H 'Content-Type: application/json' \
        --data "{\"content\":\"$1\",\"allowed_mentions\":{\"parse\":[]}}" ||
        echo "front-desk-backup: couldn't post to the webhook" >&2
}

on_exit() {
    rc=$?
    [ -z "$work" ] || rm -rf "$work"
    mkdir -p "$STATE"
    if [ -n "$ok" ]; then
        if [ -e "$STATE/failing" ]; then
            rm -f "$STATE/failing"
            alert "Front Desk's off-site backups work again."
        fi
    elif [ "$rc" != 0 ] && [ ! -e "$STATE/failing" ]; then
        touch "$STATE/failing"
        alert "Front Desk's off-site backup FAILED: $why. It tries again tomorrow. Check: journalctl -u front-desk-backup"
    fi
}
trap on_exit EXIT
trap 'exit 143' TERM INT HUP

[ "$(id -u)" = 0 ] || die 'run this as root'
[ -f "$CONF/settings" ] || die "$CONF/settings is missing"
. "$CONF/settings"
[ -n "${BACKUP_REMOTE:-}" ] || die 'BACKUP_REMOTE is not set'
[ -n "${AGE_RECIPIENT:-}" ] || die 'AGE_RECIPIENT is not set'
case "$KEEP" in
    '' | *[!0-9]* | 0) die "KEEP must be a whole number above 0, not '$KEEP'" ;;
esac
command -v age >/dev/null || die 'age is not installed'

work=$(mktemp -d)
cd "$REPO"
stamp=$(date +%Y-%m-%d-%H%M)
name=front_desk-$stamp.db.gz.age

# ---------------------------------------------------------------- copy (inside the container)
docker compose exec -T bot python - >"$work/db.gz" <<'PY' || die 'the container could not make the copy'
import gzip
import shutil
import sys
import tempfile
from pathlib import Path

import config
from cogs.ops import make_backup

with tempfile.TemporaryDirectory() as tmp:
    info = make_backup(config.DATA_DIR / "front_desk.db", Path(tmp), "front_desk.db")
    with open(info["path"], "rb") as src, gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb", mtime=0) as dst:
        shutil.copyfileobj(src, dst)
PY
gzip -t "$work/db.gz" || die 'the copy is not a whole gzip file'
[ "$(gzip -dc "$work/db.gz" | head -c 15)" = 'SQLite format 3' ] || die 'the copy is not a SQLite database'

# ---------------------------------------------------------------- encrypt
age -r "$AGE_RECIPIENT" -o "$work/$name" "$work/db.gz" || die 'age could not encrypt the copy'
rm -f "$work/db.gz"

# ---------------------------------------------------------------- push
export GIT_SSH_COMMAND="ssh -i $CONF/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$CONF/known_hosts"
export GIT_AUTHOR_NAME='Front Desk backup' GIT_AUTHOR_EMAIL='front-desk-backup@localhost'
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

refs=$(git ls-remote "$BACKUP_REMOTE" refs/heads/backups) || die 'could not reach the backup repository'
old=${refs%%[[:space:]]*}
# Trees only: the old copies stay on GitHub and are never downloaded here.
if [ -n "$old" ]; then
    git clone -q --depth 1 --filter=blob:none --no-checkout --branch backups "$BACKUP_REMOTE" "$work/clone" ||
        die 'could not read the backups branch'
    cd "$work/clone"
    git read-tree HEAD
    git ls-tree --name-only HEAD | grep '^front_desk-' | sort | head -n "-$((KEEP - 1))" |
        while IFS= read -r stale; do git update-index --force-remove "$stale"; done
else
    git init -q "$work/clone"
    cd "$work/clone"
    git remote add origin "$BACKUP_REMOTE"
fi
blob=$(git hash-object -w "$work/$name")
git update-index --add --cacheinfo "100644,$blob,$name"
tree=$(git write-tree --missing-ok)
commit=$(git commit-tree "$tree" -m "Front Desk database, $stamp")
git push -q --force-with-lease="backups:$old" origin "$commit:refs/heads/backups" ||
    die 'could not push to the backup repository'

refs=$(git ls-remote origin refs/heads/backups)
[ "${refs%%[[:space:]]*}" = "$commit" ] || die 'the backups branch is not at the commit just pushed'
ok=1
echo "front-desk-backup: pushed $name ($(wc -c <"$work/$name") bytes)"
