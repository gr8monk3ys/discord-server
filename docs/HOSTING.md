# Hosting Front Desk

Front Desk runs on the owner's home PC. When that PC is off, the bot is off. This guide covers
three ways to fix that, from "free, 20 minutes" to "the bot no longer depends on the house".

The bot only makes outgoing connections to Discord. It needs no domain, no open ports and no
public IP address, on any of the options below.

## Which option?

| | A. Harden the PC | B. Small VPS | C. Pi or mini PC at home |
|---|---|---|---|
| Cost | free | about $4 to $6 a month | $60 to $150 once, then pennies of power |
| Survives a PC shutdown or Windows update | restarts by itself afterwards | yes | yes |
| Survives a home power cut | no (back when power returns) | yes | no (back when power returns) |
| Survives a home internet outage | no | yes | no |
| Setup effort | 20 minutes | about an hour | about an hour, plus buying hardware |
| Ongoing work | none | `git pull` and one command to update | same as B |

- Do **A today** whatever else you choose. It is free and removes the common causes of a long outage.
- Choose **B** if the goal is "the bot is up even when the house is not". This is the recommended end state.
- Choose **C** if you would rather own the box than rent one. It fixes "the PC was off", not power or internet.

B and C use the same `Dockerfile` and `docker-compose.yml` from the repo root. The steps are the
same after "get a Linux box".

**The one rule for B and C: never run two bots.** Two machines with the same token both answer
every click, and coins and posts double up. The lock file only protects one machine from itself.
Stop and disable the Windows tasks *before* the new host starts (cut-over checklist below).

---

## A. Keep the PC, make it resilient

The "Front Desk bot" task already starts at boot without anyone signing in, so **Windows
auto-login is not needed**. What is needed is a PC that turns itself back on and never sleeps.

1. **BIOS: power on after a power cut.** Reboot, press Del or F2 for the BIOS setup, and find
   "Restore on AC Power Loss" (also called "AC Back", "After Power Loss" or "AC Recovery";
   usually under Advanced, Power or APM). Set it to **Power On**. Save and exit.
2. **Never sleep or hibernate.** In an *elevated* PowerShell (right-click, Run as administrator):

   ```powershell
   powercfg /change standby-timeout-ac 0      # never sleep on mains power
   powercfg /change hibernate-timeout-ac 0    # never hibernate on mains power
   powercfg /hibernate off                    # no hibernation at all (also turns off Fast Startup)
   powercfg /change disk-timeout-ac 0         # optional: never spin the disks down
   ```

   The monitor may still turn off; that does not affect the bot. Check with `powercfg /query` or
   Settings → System → Power.
3. **Windows Update active hours.** Settings → Windows Update → Advanced options → Active hours:
   set them by hand to the hours the server is busiest (for example 10:00 to 03:00; the window can
   be up to 18 hours). Updates then restart the PC in the quiet hours, and the bot comes back by
   itself a minute or two after the restart.
4. **Check both tasks.** In PowerShell:

   ```powershell
   Get-ScheduledTask 'Front Desk bot', 'Front Desk watchdog' | Select-Object TaskName, State
   python server\watchdog.py --check
   ```

   Both tasks should be `Ready` or `Running`. The watchdog restarts a hung or crashed bot within
   about 10 minutes and alerts 📋・mod-log. It runs on the same PC, so it cannot help while the PC
   itself is off: that is what steps 1 to 3 are for.
5. **Test it once.** Restart the PC and do not sign in. Within a few minutes Front Desk should be
   online and 📋・mod-log should show the "back online" note. Then switch the PC off at the wall for
   ten seconds and back on: it should boot by itself.

Optional: a small UPS (battery backup) rides out short power cuts.

---

## B. A small always-on VPS

### What to rent

Any provider's smallest Linux server is enough (Hetzner, DigitalOcean, Vultr, Akamai/Linode and
similar all have one around $4 to $6 a month).

| | Enough |
|---|---|
| CPU | 1 shared vCPU (x86 or ARM) |
| Memory | 1 GB. The bot is capped at 512 MB and uses far less; the rest is for Linux and for building the image |
| Disk | 10 GB or more (the image is about 190 MB; the database is a few MB) |
| System | Ubuntu 24.04 LTS or Debian 12 |
| Region | anything; the bot keeps Pacific time itself |
| Firewall | allow SSH (port 22) in, nothing else. The bot needs no incoming ports |

Sign in with an SSH key, not a password, if the provider offers the choice.

### Set up the server (the bot does not start yet)

On the VPS, over SSH:

```bash
curl -fsSL https://get.docker.com | sh          # Docker's official installer; includes "docker compose"
git clone <this repository's URL> front-desk
cd front-desk
docker compose build                             # about a minute
```

If you are not signed in as root, either put `sudo` in front of every `docker` command or run
`sudo usermod -aG docker $USER` once and sign in again.

From the PC, in PowerShell in the repo folder, copy the secrets file (it is never in git and never
in the image):

```powershell
scp server\.env USER@SERVER-IP:front-desk/server/.env
```

Back on the VPS: `chmod 600 server/.env`.

### Cut-over checklist

Pick a quiet hour. The bot is offline for about five minutes.

1. **PC: stop the bot and keep it stopped.** Elevated PowerShell:

   ```powershell
   Disable-ScheduledTask 'Front Desk watchdog'
   Stop-ScheduledTask 'Front Desk bot'
   Disable-ScheduledTask 'Front Desk bot'
   Get-ScheduledTask 'Front Desk bot', 'Front Desk watchdog' | Select-Object TaskName, State
   ```

   Both must say `Disabled`, and Front Desk must show as offline in Discord before you go on.
2. **PC: pack the data** (after the stop, so nothing is missed). In the repo folder:

   ```powershell
   server\.venv\Scripts\python server\migrate_host.py pack            # shows what it would do
   server\.venv\Scripts\python server\migrate_host.py pack --apply    # writes bot\data\migrate\front-desk-migrate-....tar.gz
   scp bot\data\migrate\front-desk-migrate-*.tar.gz USER@SERVER-IP:front-desk/migrate.tar.gz
   ```

   The archive holds a checked copy of the database and a manifest. It never contains `.env`,
   logs or the lock file. It does contain member data: do not put it in git, chat or cloud drives.
3. **VPS: unpack it.** In `~/front-desk`:

   ```bash
   docker compose run --rm -v "$PWD/migrate.tar.gz:/tmp/migrate.tar.gz:ro" bot \
     python /app/server/migrate_host.py unpack /tmp/migrate.tar.gz            # checks only
   docker compose run --rm -v "$PWD/migrate.tar.gz:/tmp/migrate.tar.gz:ro" bot \
     python /app/server/migrate_host.py unpack /tmp/migrate.tar.gz --apply
   ```

   The first command must print `Archive OK` with the same schema version the pack step printed.
4. **VPS: start the bot.**

   ```bash
   docker compose up -d
   docker compose logs -f --tail 50      # wait for "logged in as"; Ctrl+C stops watching, not the bot
   ```
5. **Check.** In Discord: Front Desk is online, 📋・mod-log has the "back online" note, `/status`
   answers and `/balance` shows your coins. On the VPS, `docker compose ps` says `healthy` after
   about three minutes.
6. **Clean up.** `rm migrate.tar.gz` on the VPS and delete `bot\data\migrate\` on the PC. Leave the
   PC's `bot\data\front_desk.db` alone for a week: it is the rollback copy.
7. **Set up off-box backups** (below), then test a reboot: `sudo reboot`, wait two minutes, and
   the bot should be back without you doing anything.

### Rolling back to the PC

- **The new host never worked:** on the VPS `docker compose down`. On the PC (elevated PowerShell)
  `Enable-ScheduledTask 'Front Desk bot'; Start-ScheduledTask 'Front Desk bot'; Enable-ScheduledTask 'Front Desk watchdog'`.
  The PC's database was never touched, so nothing is lost.
- **The new host ran for a while** (members earned coins there): bring the data back first.

  ```bash
  # VPS
  docker compose stop
  docker compose run --rm bot python /app/server/migrate_host.py pack --apply
  docker compose cp bot:/app/bot/data/migrate ./migrate
  ```

  ```powershell
  # PC, in the repo folder
  scp USER@SERVER-IP:front-desk/migrate/front-desk-migrate-*.tar.gz .
  server\.venv\Scripts\python server\migrate_host.py unpack (Get-Item front-desk-migrate-*.tar.gz) --apply --force
  ```

  `--force` renames the PC's old database to `front_desk.db.before-migrate-...` instead of
  deleting it. Then enable and start the tasks as above. Leave the VPS stopped
  (`docker compose down`) so it cannot come back after a reboot.

### Day to day on Linux

| Situation | Do this (in `~/front-desk`) |
|---|---|
| Is it up? | `docker compose ps` (`healthy` = heartbeat under 3 minutes old) |
| Read the log | `docker compose logs --tail 100` |
| Restart | `docker compose restart` |
| Update to the latest code | `git pull && docker compose up -d --build` |
| Stop on purpose | `docker compose down` (data is kept in the `front-desk-data` volume) |
| Run a script inside | `docker compose exec bot python /app/server/migrate_host.py pack` |
| Restore a nightly backup | `docker compose stop`, then `docker compose run --rm bot sh -c 'cp data/front_desk.db data/front_desk.db.bad && cp data/backups/front_desk-YYYY-MM-DD.db data/front_desk.db && rm -f data/front_desk.db-wal data/front_desk.db-shm'`, then `docker compose start` |

Never run `docker compose down -v` or `docker volume rm`: `-v` deletes the database.

### What replaces the Windows task and the watchdog

| On the PC | On Linux |
|---|---|
| Task "Front Desk bot" (start at boot, restart on crash) | `restart: unless-stopped` in `docker-compose.yml`; Docker itself starts at boot |
| `bot.lock` (one copy per machine) | unchanged, inside the data volume |
| Task "Front Desk watchdog": restart when the heartbeat is 10 minutes old, at most 3 times an hour | `bot/run_container.sh` does the same inside the container: it stops a bot whose heartbeat is 10 minutes old and Docker starts a fresh one, at most 3 times an hour |
| `watchdog.py --check` | `docker compose ps`: the health check reads `bot/data/heartbeat` and reports `unhealthy` when it is older than 3 minutes |
| Watchdog alert in 📋・mod-log while the bot is down | **not replaced.** The bot still posts its own "back online" note after every restart. `WATCHDOG_WEBHOOK_URL` and `WATCHDOG_PING_USER_ID` in `.env` are simply unused |
| `watchdog.py --pause` before stopping on purpose | not needed: `docker compose down` or `stop` stays stopped |
| Backups in `D:\Backups\front-desk` | the same nightly 04:00 backup, 14 kept, lands in `backups/` inside the data volume (the bot falls back there because `D:\` does not exist) |

### Off-box backups

The nightly backups sit on the same disk as the database. If the VPS is deleted, both are gone.
Copy them somewhere else once a day. Run `crontab -e` on the VPS and add one line (cron uses the
server's clock, normally UTC; 13:00 UTC is after the 04:00 Pacific backup all year):

```cron
0 13 * * * cd $HOME/front-desk && docker compose cp bot:/app/bot/data/backups . && rclone copy backups REMOTE:front-desk-backups >> $HOME/front-desk-backup.log 2>&1
```

Replace the `rclone copy ...` part with whatever you have: `rclone` to a cloud drive you already
pay for, or `scp -r backups USER@OTHER-MACHINE:front-desk-backups`. The files are full copies of
the database, so the destination must be private. Once a month, open the destination and check
that yesterday's file is there.

---

## C. A Raspberry Pi or mini PC at home

Same container, your own hardware.

- **Raspberry Pi:** a Pi 4 or 5 with 2 GB or more, running the **64-bit** Raspberry Pi OS Lite.
  Boot from an SSD or a good "endurance" SD card. A Pi powers on by itself when power returns.
- **Mini PC:** any used x86 box with 4 GB of memory, with Debian 12 or Ubuntu Server 24.04. Set
  the same BIOS option as in A.1 ("Restore on AC Power Loss: Power On").
- Use a network cable rather than Wi-Fi if you can.

Then follow section B from "Set up the server" onwards, with the Pi in place of the VPS
(`scp ... USER@PI-ADDRESS:...`). The cut-over checklist, rollback, daily commands and backup cron
line are identical. For off-box backups, the PC's `D:\Backups` is a fine destination: on the PC,
a daily scheduled `scp -r USER@PI-ADDRESS:front-desk/backups D:\Backups\front-desk-pi` after the
Pi's cron line has run.

The first `docker compose build` takes a few minutes on a Pi.

---

## What stays on the PC after moving (B or C)

Nothing. The bot, its database, its backups and its restart logic all live on the new host.

- Keep both Windows tasks **disabled** for a week as the rollback path, then remove them
  (`Unregister-ScheduledTask 'Front Desk bot'`, same for the watchdog). Do not re-run
  `server\install_watchdog.ps1`: it would switch the watchdog back on.
- The old `bot\data\front_desk.db` and `D:\Backups\front-desk` go stale from the moment of the
  cut-over. Delete them once you are sure you will not roll back.
- The repo itself is still useful on the PC for development and tests. The setup scripts
  (`server\*.py --apply`) talk to Discord directly and are safe to run from any machine that has
  `server\.env`. Running `bot\main.py` or `bot\run_bot.bat` there is **not** safe while the other
  host is up: that would be a second bot.
