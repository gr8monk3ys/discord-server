# Architecture

A map of the system for the next engineer or AI session. `README.md` is the feature list and
runbook; this file says how the pieces fit together. Everything here was read from the code
on 2026-10-11 (schema v15, 37 modules). When this file and the code disagree, the code wins:
fix the file.

## The four parts

| Part | Path | What it is |
|---|---|---|
| Server as code | `server/` | One-shot scripts that build and extend the Discord server from `layout.py`, a read-only snapshot, the watchdog and the host-migration tool |
| Front Desk | `bot/` | The server's own discord.py bot: one process, one guild, one SQLite file |
| Landing page | `site/` | Static page and the `/join` redirect; no build step |
| Client theme and assets | `theme/`, `assets/` | Vencord/BetterDiscord CSS; generated icon, emoji, sounds and listing art plus the scripts that make them |

`server/layout.py` is the shared source of truth. `bot/config.py` puts `server/` on `sys.path`
and imports `layout` and `names.slug`, so the bot's games list (`config.GAMES`) and the setup
scripts can never disagree. Channel and role names the bot needs are constants in
`bot/config.py`, written exactly as they appear in `layout.py` (emoji included); objects are
looked up by name at run time, never by a stored ID.

## Bot runtime (`bot/main.py`)

1. `config.load_settings()` reads `server/.env` (`DISCORD_TOKEN`, `GUILD_ID`, optional `TZ`,
   default `America/Los_Angeles`). Optional feature settings are read from the environment by
   their own cogs: the `/catchup` summary provider and key (catchup; see `server/.env.example`)
   and `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` (creators). Without them those features
   say they are not set up and the rest of the bot is unaffected.
2. `single_instance_lock()` takes an exclusive lock on `bot/data/bot.lock` for the life of the
   process, so a second copy on the same machine exits at once.
3. `FrontDesk.setup_hook()` connects the database, applies pending migrations, loads every
   extension in `MODULES` in order, then copies the command tree to the one guild and syncs it
   (guild commands, so changes show immediately).
4. Intents: the default set plus whichever privileged intents the `MODULES` entries name
   (`members`, `presences`, `message_content`). If Discord refuses them the bot restarts once
   without privileged intents; cogs are written to load and degrade rather than crash.
5. Bot-wide `allowed_mentions` is `everyone=False, roles=False, users=True`; cogs narrow it
   further per message (see cross-cutting rules).
6. Logging: `bot/data/bot.log` (rotating, 1 MB x 3) and the console when there is one.
   Slash-command errors go through `errors.reply_for` / `reply_error` (a short ephemeral reply;
   details only in the log).

## How a request flows

```
Discord interaction / gateway event / tasks.loop tick
        |
   cogs/<name>.py        Discord I/O only: read the interaction, fetch rows, send replies
        |
   logic/<name>.py       pure rules: no discord, no database, no clock of its own
        |                (the cog passes `now`, rows and plain values in)
   db.py / economy.py    one aiosqlite connection; coins only through economy.py
```

Worked example, `/daily`: `cogs/economy.py` checks `db.tracking_allowed(user)`, opens
`db.transaction()`, reads the wallet row, asks `logic/coins.py` what the claim is worth and
what the new streak is, calls `economy.apply_tx(tx, user, amount, "daily", now, ref=...)`
(ledger row + wallet update), updates the streak columns in the same transaction, then
replies. A second click finds the ref already in the ledger and gets `Status.DUPLICATE`.

Rules that make this hold together:

- `logic/` modules do no Discord I/O and never touch the database: they import the standard
  library, `config` and each other (plus Pillow for the card images; `events`, `levels` and
  `recap` import `discord` only for `utils.escape_markdown`). They are what the
  tests exercise most; `tests/test_<name>.py` covers the rules, `tests/test_<name>_cog.py`
  drives the cog with fakes.
- `Database` (`bot/db.py`) holds one connection in WAL mode with `foreign_keys = ON` and one
  `asyncio.Lock`. `transaction()` is `BEGIN IMMEDIATE` under that lock, so two button clicks
  cannot interleave; single statements (`execute`, `fetchone`, `fetchall`) take the same lock.
- Buttons and selects that must survive a restart are `discord.ui.DynamicItem`s whose
  `custom_id` carries a row id or a list index; the row is re-read on every click.
- Small cross-restart state (message ids of panels, "first run" markers, last-run stamps)
  lives in the `meta` key/value table, namespaced per cog (`selfroles:panel`, `ops:perms`,
  `tourney:<id>:auto`, ...). It has no schema of its own, so grep the cog for its keys.
- A few cogs reuse another cog's helpers directly: `cogs.lfg.ping_only`, `cogs.stats`
  (voice presence, used by economy and tempvoice), `cogs.clips` (clip detection, used by
  economy), `cogs.growth.BUMP_DUE_KEY` (votehub), `cogs.tempvoice` (matchmaking registers its
  match channels there). Many cogs read other cogs' tables; only the owner writes them.

## Module map

Load order is the order in `MODULES`. "Writes" lists tables the cog writes besides `meta`
and `jobs` (most cogs with scheduled jobs write both). Times are local (Pacific unless `TZ`
is set). Loops without a wall-clock time are plain `tasks.loop` intervals.

| Module | Owns | Writes | Scheduled work | Channels and roles |
|---|---|---|---|---|
| `lfg` | `/lfg` squad posts, Join/Leave/Close | `lfg_posts`, `lfg_members` | expiry sweep 5 min (posts close after 3 h) | 🎮・lfg forum; game roles, LFG |
| `stats` | voice/message/game-time recording, `/stats`, `/leaderboard`, `/privacy`, weekly MVP | `voice_sessions`, `game_sessions`, `message_counts`, `privacy_optout` (and deletes a member's rows on opt-out) | MVP Sun 18:00; 1 min presence heartbeat | 💬・general |
| `growth` | invite tracking, Recruiter role, Disboard bump reminders, `/invites`, `/recruiters`, `/bumpers`, `/bumpping` | `invite_uses`, `joins`, `bumps` | bump reminder 1 min; recruiter sweep 24 h | 🤖・bot-commands; Bumper, Recruiter |
| `community` | welcome after Onboarding, `/report`, mod log of joins/leaves/bans/AutoMod | `welcomed`, `reports` | none | 💬・general, 📋・mod-log, 🛡️・mod |
| `starboard` | hall of fame at 3 stars | `starboard` | none | ⭐・hall-of-fame |
| `clips` | clip collection, weekly poll, winner role | `clips`, `clip_polls` | poll Sun 18:05 (open 24 h) | 📸・clips, 🛡️・mod; Clip of the Week |
| `tempvoice` | join-to-create voice, `/squad` | `temp_voice` | none (deletes 30 s after empty) | 05 · voice category, ➕ New Squad |
| `events` | `/gamenight`, 15-minute reminders, free games | `gamenights`, `free_games` | free games Thu 18:00; reminders 1 min | 🕹️・gaming, game channels, voice channels; Game Night |
| `economy` | earning (voice, chat, clips, squads, MVP, clip of the week), `/daily`, `/balance`, `/give`, `/coinflip`, `/richest` | `wallets` streak columns; coins via `economy.py` | voice coins 5 min | none of its own |
| `games` | `/slots`, `/blackjack`, `/trivia`, `/predict` | `predictions`, `prediction_bets`, `blackjack_open` | none (open blackjack bets refunded at start) | none of its own |
| `shop` | `/shop`, `/buy`, `/season`, `/raffle`, Spotlight | `perks`, `seasons` | raffle draw Sun 20:00; month-end season results; perk expiry 5 min | 💬・general, 📣・announcements; Hype, Season Champ, colour roles |
| `engagement` | QOTD, this-or-that poll, counting, auto game night, `/birthday` | `qotd_used`, `poll_used`, `counting`, `counting_run`, `birthdays`, `gamenights` | birthdays 09:00, QOTD 12:00, poll 18:00 daily; auto game night Fri 12:00 | 💬・general, 🔢・counting, 🕹️・gaming; Birthday, Counting Champ |
| `moderation` | `/warn`, `/timeout`, `/untimeout`, `/cases`, `/purge`, auto-escalation, anti-spam, anti-raid | `cases` | raid restore check 30 s | 📋・mod-log, 🛡️・mod |
| `ops` | backups, error alerts, back-online note, drift check, permission self-check, `/status` | none | backup 04:00 daily; drift Sun 03:00; permission check 60 s after ready and 09:00 daily | 📋・mod-log |
| `utility` | `/remind`, `/reminders`, `/afk`, suggestion votes and tags, stat channels, tickets | `reminders`, `afk`, `tickets` | reminders 30 s; stat channels 10 min | 🆘・help, 💡・suggestions forum, 01 · front desk category |
| `cards` | welcome card image | none | retry failed cards every 10 min for 24 h | 👋・welcome |
| `tournaments` | `/tournament`, brackets, result confirmation, auto tournaments | `tournaments`, `tournament_entries`, `tournament_matches` | first Monday of the month 12:00 opens one, auto-start Sat 19:00; 1 min tick (reminders, nudges, 48 h staff flag) | 🏆・tournaments, 📋・mod-log; Tournament Champ |
| `achievements` | badges, `/profile`, `/badges` | `achievements` | hourly sweep | 💬・general |
| `creators` | `/creator` linking and verification, upload and go-live posts | `creators`, `creator_pending`, `creator_seen` | YouTube 15 min; Twitch 3 min; pending cleanup hourly | 📺・creators, 📋・mod-log |
| `levels` | XP from chat and voice, level roles, `/rank`, `/levels`, `/xp` | `xp` | voice XP sweep 10 min | 🤖・bot-commands, 📋・mod-log; Regular, Veteran, Elite, Legend, Mythic |
| `recap` | weekly recap, owner digest, member milestones, invite contest | none | recap Mon 10:00; digest Mon 10:05; contest on the 1st 12:00 | 📣・announcements, 📋・mod-log |
| `selfroles` | role panel and `/roles` | none | none (panel edited in place at start) | 🎭・roles; platform, region, play-time, ping and game roles |
| `partners` | `/partner apply/remove`, review cards | `partners` | dead-invite sweep Mon 12:00 | 🤝・partners, 📋・mod-log |
| `quests` | starter quest, `/quest`, nudge DM | `quest_steps`, `achievements` (Settled In) | hourly sweep | 💬・general |
| `heartbeat` | `bot/data/heartbeat` file, event-loop lag warning | none (a file, not the DB) | beat 60 s; lag probe 5 s | none |
| `challenges` | weekly challenges, `/challenges`, Claim button | `challenge_claims` | board Mon 09:00; auto-claim sweep hourly | 🎲・games |
| `wordgame` | Daily Word, `/word` | `word_games` | none (word changes at local midnight by date) | 🎲・games |
| `matchmaking` | `/queue`, match voice channels | `mm_queue`, `mm_matches`, `temp_voice` | expiry sweep 1 min (entries last 60 min) | 🎲・games (else 🤖・bot-commands), 05 · voice category |
| `econstats` | staff `/economy` (read-only) | none | none | none |
| `helpdesk` | `/help`, `/about`, rotating presence | none | presence every 5 min | 🎭・roles (link only) |
| `news` | official game news feeds, staff `/news test` | `news_seen` | poll every 30 min | every game channel |
| `staffapps` | `/apply staff/status`, `/apps list`, review cards | `staff_apps` | none | 📋・mod-log; Moderator (only if below the bot) |
| `seasonal` | seasonal events: drops, daily quest, `/event`, finale | `event_drops`, `event_claims`, `achievements` (event badge) | 1 min tick (seeded drop times 10:00 to 23:30, start and finale); quest sweep hourly | 💬・general, 🎲・games, 📣・announcements; Event Champ |
| `gamelink` | `/link`, `/unlink`, `/accounts`, Steam section on `/profile` | `game_accounts`, `game_account_pending` | upkeep 30 min (each account re-read at most daily) | 📋・mod-log |
| `votehub` | `/vote`, weekly Supporter thanks | none (reads `bumps`) | thanks Sun 17:00 | 🎲・games, 🤖・bot-commands |
| `catchup` | `/catchup` AI summary, staff `/catchup-usage` | `catchup_usage` | none | any public text channel (never the staff category) |
| `vibes` | chat revival, join anniversaries, booster thanks and stipend, member of the month | `qotd_used` | 1 min tick: anniversaries 11:00 daily; stipend on the 1st 12:00; member of the month on the 1st 12:30 | 💬・general, 📣・announcements |

The exact channel permissions and roles each cog needs are data, not prose: `CHANNEL_NEEDS`,
`GUILD_NEEDS` and `ROLE_NEEDS` in `bot/logic/ops.py`. The permission self-check and `/status`
read those tables, and `PERMISSIONS` in `bot/main.py` is what `--invite` asks for.

## Cross-cutting rules

**Coins only through `bot/economy.py`.** `apply` / `apply_tx` write one `ledger` row and
update `wallets` in the same transaction; `transfer` / `transfer_tx` do both legs or neither.
A balance cannot go below zero (explicit check plus a `CHECK` constraint). Any payout that
could be retried (a scheduled job, a button, a sweep) passes a `ref` such as
`challenge:WEEK:USER:KEY`; `ledger.ref` is `UNIQUE`, and a repeated ref returns
`Status.DUPLICATE` without paying. Use the `*_tx` variants inside an open `db.transaction()`
when the coin move must commit with other rows (a claim row, a perk row). `earned_tx` backs
the daily caps. Amounts, caps and refs are decided in `logic/` (`coins.py`, `shop.py`, ...);
`docs/ECONOMY.md` lists every source and sink.

**Account age for anything rewardable.** `logic/quests.py` defines `MIN_ACCOUNT_DAYS = 30`,
`established(user_id, at)` (age read from the snowflake id, so it works for members who left)
and `established_sql(...)` for use inside queries. Two uses: the earner must be established to
be paid (quest, challenges, tournaments, seasonal, Daily Word, `/give`, vote thanks, MVP,
`/catchup`), and where other people's actions earn someone progress (stars, poll votes, squad
joins, voice company) only established accounts count, so alts cannot hand out rewards.
Staff applications use their own 90-day rule in `logic/staffapps.py`.

**Privacy opt-out.** `db.tracking_allowed(user_id)` is the one gate (`privacy_optout` table).
Every recorder of activity (stats, coins, XP, quest steps, clips) checks it first; readers
that rank or show members filter opted-out members out. `/privacy tracking:off` also deletes
the member's voice, message and game history and XP. Things a member does on purpose (a
partner or staff application, Daily Word, a linked account) are not tracking and are not
gated. If a change stores something new about members, update `PRIVACY.md`.

**Member text the bot reposts.** Discord AutoMod never acts on bots, so
`logic/textfilter.py` (`check`, `check_all`, `message`) stands in for it: other servers'
invite links, optionally any link, and a normalised whole-word blocklist. It is applied to
`/lfg` and `/gamenight` notes, shoutouts and Spotlight, tournament names and partner
applications. Refusals never echo the text back.

**Pings and markdown.** Messages that contain member-supplied text are sent with
`ping_only(roles=..., users=...)` (`cogs/lfg.py`; allows exactly the listed mentions) or
`discord.AllowedMentions.none()`. Markdown in names and free text is escaped before the bot
posts it, and every outbound link is rebuilt from validated parts (invite code, Steam id,
channel id) rather than copied. Purchased colour roles are name-prefixed so they cannot look
like staff roles.

**Scheduled jobs (`logic/schedule.py`).** A job is a `Weekly(name, weekday, hour, minute)`;
`plan()` returns the one `Period` to run now and any older ones to mark done. Semantics:
only the latest due period can ever run; the very first sighting of a job only marks it done
(turning a module on never replays history); missed periods since first sight are marked
done, not run; a finished period's key (`name:YYYY-Www`) goes into `jobs` after the work
succeeded, so a failure retries on the next tick and a success never repeats. `first_seen:<job>`
lives in `meta`. `Daily` (`logic/ops.py`, `logic/engagement.py`) and `Monthly`
(`logic/recap.py`, `logic/tournaments.py`) follow the same rules with day and month keys. Cogs
poll with a 1 to 5 minute `tasks.loop` and ask the planner; nothing sleeps until a wall-clock
time.

**Permission self-check (`cogs/ops.py`).** About a minute after ready and daily at 09:00 the
bot compares what the loaded cogs need (`logic/ops.py` tables) with what it actually has:
channels exist, channel permissions, server permissions, and handed-out roles exist and sit
below its own top role. One mod-log embed when the set of problems changes, one when it
clears; the current count shows in `/status`. A test keeps the tables and `MODULES` in step,
so a new cog that posts somewhere must be added there.

**Liveness.** `cogs/heartbeat.py` rewrites `bot/data/heartbeat` atomically every 60 s while
the gateway connection is really up (it tracks disconnect/resume itself). `server/watchdog.py`
runs outside the bot every 5 minutes: a heartbeat older than 10 minutes restarts the bot task
(at most 3 times an hour) and alerts through a webhook; `--pause N` means "down on purpose".
Inside the bot, `ops` counts ERROR log records per logger (more than 5 in 10 minutes posts
one alert, at most hourly per logger, with tokens redacted) and posts a "back online" note
after 30+ minutes down.

## Data model

SQLite at `bot/data/front_desk.db` (gitignored). `MIGRATIONS` in `bot/db.py` is append-only;
`PRAGMA user_version` is the schema version. Times are unix seconds; days are local ISO dates.

| Feature | Tables | Migration |
|---|---|---|
| Core | `meta` (key/value), `jobs` (finished job keys), `privacy_optout` | 1 |
| Squad-up | `lfg_posts`, `lfg_members`; unique index "one open post per host per game" | 1, 2 |
| Stats | `voice_sessions`, `game_sessions`, `message_counts` | 3 |
| Growth | `invite_uses`, `joins`, `bumps` | 4 |
| Community | `welcomed`, `reports` | 4 |
| Hall of fame | `starboard` | 5 |
| Clips | `clips`, `clip_polls` | 5 |
| Temp voice | `temp_voice` | 5 |
| Game nights, free games | `gamenights`, `free_games` | 5 |
| Economy | `wallets`, `ledger` | 6 |
| Games | `predictions`, `prediction_bets`, `blackjack_open` | 6 |
| Shop, seasons | `perks`, `seasons` | 7 |
| Engagement | `qotd_used`, `poll_used`, `counting`, `counting_run`, `birthdays` | 8 |
| Moderation | `cases` | 8 |
| Utility | `reminders`, `afk`, `tickets` | 8 |
| Tournaments | `tournaments`, `tournament_entries`, `tournament_matches` | 9 |
| Achievements | `achievements` | 9 |
| Creators | `creators`, `creator_seen`; `creators.verified`, `creator_pending` | 9; 10 |
| Levels | `xp` | 10 |
| Partners | `partners` | 10 |
| Starter quest | `quest_steps` | 10 |
| Weekly challenges | `challenge_claims` | 11 |
| Daily Word | `word_games` | 11 |
| Matchmaking | `mm_queue`, `mm_matches` | 12 |
| (indexes only) | hot-query indexes on `ledger`, `message_counts`, session tables | 13 |
| Game news | `news_seen` | 14 |
| Staff applications | `staff_apps` | 14 |
| Seasonal events | `event_drops`, `event_claims` | 15 |
| Game accounts | `game_accounts`, `game_account_pending` | 15 |
| Catch-up | `catchup_usage` | 15 |

Derived, not stored: badges, challenge progress, quest steps, event points, recap numbers and
season standings are recomputed from the owning tables every time, so a restart or a missed
event cannot lose progress. Raffle tickets are ledger rows. No message text is stored; the
exceptions are a quoted reported message and the hall-of-fame copy (both in Discord, not the DB).

Other files in `bot/data/`: `bot.log*`, `bot.lock`, `heartbeat`, `backups/` (fallback backup
target), `migrate/` (host-move archives), and the two tracked banks `qotd.txt` and `polls.txt`.

## Server as code (`server/`)

| Script | Role | Changes Discord? |
|---|---|---|
| `layout.py` | Source of truth: `GAMES`, roles, categories and channels, Onboarding, AutoMod, `PUBLIC` text, `TRIM` | no (data) |
| `names.py` | `slug()`: name matching that ignores emoji, case and separators | no |
| `setup_server.py` | First build: create or restyle roles and channels, **reorder roles**, post rules and welcome | with `--apply` |
| `polish_server.py` | Second pass: Community, Onboarding, welcome screen, AutoMod, media lock, trim empty channels | with `--apply` |
| `public_mode.py` | Later changes, additive only: missing roles and channels, private categories, description, invite, Onboarding prompts, AutoMod gaps | with `--apply` |
| `expressions.py` | Upload emoji and soundboard sounds from `assets/expressions/` (skips existing names) | with `--apply` |
| `snapshot_server.py` + `snapshot_lib.py` | Read-only dump of live config to `server/snapshot/*.json` (names not IDs, sorted); `--compare` lists drift from `layout.py` | never |
| `watchdog.py`, `install_watchdog.ps1` | Downtime watchdog and its Task Scheduler install | no (webhook alert only) |
| `migrate_host.py` | `pack` / `unpack` the database for a host move (database and manifest only, never `.env`) | no |

Every script dry-runs by default. None deletes roles or channels that are missing from
`layout.py`; they are listed for a human. Because Front Desk now sits below Keeper and
Moderator, `setup_server.py` (which calls `edit_role_positions`) must not be run against the
live server again: use `public_mode.py`.

Two drift checks exist and compare different things:

- `snapshot_server.py --compare`: live server versus `layout.py` (run by hand).
- `ops` weekly drift job (Sun 03:00): live roles and channels, built with the same
  `snapshot_lib` helpers, versus the committed `server/snapshot/*.json`. It posts a summary to
  the mod log and writes nothing to the repo. After an intended server change, re-run
  `snapshot_server.py` and commit the snapshot so the next check is quiet.

## Landing page (`site/`)

Plain HTML, CSS and JS with a strict CSP in `vercel.json` (no inline script or style). The
invite code lives only in `site/config.js`; `app.js` shows live counts from Discord's public
invite endpoint and `join/` redirects to the invite. The games block in `index.html` is
generated from `layout.GAMES` by `python -m logic.sitegen` (run in `bot/`), and
`bot/tests/test_sitegen.py` fails when the page drifts from `layout.py` or breaks the CSP.
`logic/textfilter.py` reads the server's own invite code from `site/config.js` to allow it.

## Deployment

| Piece | Today | Notes |
|---|---|---|
| Bot | Windows Task Scheduler task "Front Desk bot" runs `pythonw main.py` in `bot/` from the shared `server/.venv`, at boot and logon | Stop the task (and pause the watchdog) before running `main.py` by hand; the lock file refuses a second copy |
| Watchdog | Task "Front Desk watchdog", `server/watchdog.py` every 5 minutes | Installed by `server/install_watchdog.ps1` |
| Backups | `ops` daily 04:00, SQLite online backup, integrity-checked, newest 14 kept | `config.BACKUP_DIR`, falling back to `bot/data/backups` when that drive is missing |
| Container kit | `Dockerfile`, `docker-compose.yml`, `bot/run_container.sh` | Not the live deployment. Read-only root, non-root user, data volume, heartbeat healthcheck and an in-container monitor with the watchdog's thresholds. Steps and cut-over checklist: `docs/HOSTING.md` |
| Site | Vercel project rooted at `site/`, production branch `main` | No build or install command |
| Site mirror | GitHub Pages via `.github/workflows/pages.yml` on pushes that touch `site/` | Rewrites `404.html` paths for the project sub-path |

Never run the bot on two machines with the same token: the lock is per machine. The
`front-desk-bot` branch must be kept, because the bot's public privacy link points at
`PRIVACY.md` on it; fast-forward it after changing `PRIVACY.md` on `main`.

## Where to add things

| You want to | Do this |
|---|---|
| Add a feature | `bot/logic/<name>.py` + `tests/test_<name>.py`, then `bot/cogs/<name>.py` + `tests/test_<name>_cog.py`, then an entry in `MODULES` with the privileged intents it needs |
| Store something new | Append a **new** list to `MIGRATIONS`; never edit an existing one. Update `PRIVACY.md` if it is about members, and the data-model table above |
| Pay or charge coins | `economy.apply_tx` / `transfer_tx` with a `reason` and, for anything retryable, a `ref`. Put amounts and caps in `logic/`. Gate rewards on `quests.established`. Add the source or sink to `docs/ECONOMY.md` |
| Run something on a schedule | Define a `Weekly` / `Daily` / `Monthly` job in `logic/`, poll it from a `tasks.loop`, record the period key in `jobs` only after the work succeeded |
| Post in a channel or hand out a role | Add the name to `bot/config.py` (and to `server/layout.py` if it does not exist yet, created with `public_mode.py`), then add a `Need` / `ROLE_NEEDS` entry in `bot/logic/ops.py` so the self-check covers it |
| Repost member text | Run it through `logic/textfilter`, escape markdown, send with `ping_only(...)` or `AllowedMentions.none()` |
| Record member activity | Check `db.tracking_allowed` first; make opt-out delete it (`cogs/stats.py` `/privacy`) |
| Add a game | Edit `GAMES` in `server/layout.py`, run `public_mode.py`, regenerate the site block with `logic.sitegen`, add a feed to `bot/assets/news_sources.json` if one exists |
| Change the server's structure | Edit `layout.py`, dry-run then `--apply` `public_mode.py`, re-run `snapshot_server.py` and commit the snapshot |
| Add a persistent button | A `DynamicItem` with ids in `custom_id`, registered in the cog's setup; re-read state from the DB on click |

Related docs: `README.md` (features, runbook), `PRIVACY.md`, `docs/ECONOMY.md`,
`docs/HOSTING.md`, `docs/LISTING.md`, `site/README.md`, and the original design and build
history in `docs/superpowers/specs/2026-10-01-front-desk-bot-design.md`.
