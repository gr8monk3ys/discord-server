# Architecture

A map of the system for the next engineer or AI session. `README.md` is the feature list and
runbook; this file says how the pieces fit together. Everything here was read from the code
on 2026-10-12 (schema v23, 47 modules). When this file and the code disagree, the code wins:
fix the file.

## The four parts

| Part | Path | What it is |
|---|---|---|
| Server as code | `server/` | One-shot scripts that build and extend the Discord server from `layout.py`, a read-only snapshot, the watchdog and the host-migration tool |
| Front Desk | `bot/` | The server's own discord.py bot: one process, one guild, one SQLite file |
| Landing page | `site/` | Static pages (home, `/join`, `/appeal`, `/linked-role`) and three Vercel functions under `site/api/`; no build step |
| Client theme and assets | `theme/`, `assets/` | Vencord/BetterDiscord CSS; generated icon, emoji, sounds and listing art plus the scripts that make them |

`server/layout.py` is the shared source of truth. `bot/config.py` puts `server/` on `sys.path`
and imports `layout` and `names.slug`, so the bot's games list (`config.GAMES`) and the setup
scripts can never disagree. `config.RETIRED_GAMES` mirrors `layout.RETIRED_GAMES`: a retired
game is never offered (no choice, role panel entry, news feed or queue), and
`config.known_game_by_key` exists only so rows written while it was listed (an old squad post,
a queue row) still show its emoji and name. Linked Steam accounts keep a retired game's hours
without showing them. Channel and role names the bot needs are constants in
`bot/config.py`, written exactly as they appear in `layout.py` (emoji included); objects are
looked up by name at run time, never by a stored ID.

## Bot runtime (`bot/main.py`)

1. `config.load_settings()` reads `server/.env` (`DISCORD_TOKEN`, `GUILD_ID`, optional `TZ`,
   default `America/Los_Angeles`). Optional feature settings are read from the environment by
   their own cogs: the `/catchup` summary provider and key (catchup; see `server/.env.example`)
   and `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` (creators). Without them those features
   say they are not set up and the rest of the bot is unaffected. The newer ones are read
   through helpers in `bot/config.py` or by the cog: `CREW_PING_MINUTES`, `CREW_QUIET_HOURS`
   (crew), `SUPPORT_URL` (supporter), `TOWNHALL_TIME` (events), `YOUTUBE_CLIENT_ID`,
   `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`, `YOUTUBE_PRIVACY` (clipcast),
   `REALMS_ADDRESS`, `REALMS_RCON_HOST`, `REALMS_RCON_PORT`, `REALMS_RCON_PASSWORD`,
   `REALMS_LOG_PATH` (realms), `SITE_DATA` (sitedata), `TONIGHT` (tonight; `off` stops the
   voice line, anything else or unset leaves it on), `SMP_WATCH`, `SMP_WATCH_ADDRESS`, `SMP_WATCH_MIN`
   (smpwatch; `off` stops the Monkey SMP ping), `FIELD_NOTES_GITHUB_USER`,
   `FIELD_NOTES_SPOTIFY_HISTORY`, `FIELD_NOTES_LETTERBOXD_USER`, `FIELD_NOTES_PLAYED` (fieldnotes;
   none set runs no tick), `QUIET` (`bot/quietgate.py`; `off` posts
   every question and poll whether or not anyone answers, anything else or unset leaves the
   quiet rule on), and `APPEAL_SECRET`,
   `APPEAL_COOLING_DAYS`, `APPEAL_REAPPEAL_DAYS`, `SITE_URL` (appeals, and `/ban`'s DM).
   All are optional; `docs/WAVE9_SETUP.md` says what each turns on.
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
   jobs.py               (tasks.loop ticks) which period of a scheduled job is due, and marking it done
```

Worked example, `/daily`: `cogs/economy.py` checks `db.tracking_allowed(user)`, opens
`db.transaction()`, reads the wallet row, asks `logic/coins.py` what the claim is worth and
what the new streak is, calls `economy.apply_tx(tx, user, amount, "daily", now, ref=...)`
(ledger row + wallet update), updates the streak columns in the same transaction, then
replies. A second click finds the ref already in the ledger and gets `Status.DUPLICATE`.

Rules that make this hold together:

- `logic/` modules do no Discord I/O and never touch the database: they import the standard
  library, `config` and each other (plus Pillow for the card images; `events`, `levels`,
  `recap`, `tonight` and `fieldnotes` import `discord` only for `utils.escape_markdown`). They are what the
  tests exercise most; `tests/test_<name>.py` covers the rules, `tests/test_<name>_cog.py`
  drives the cog with fakes.
- `Database` (`bot/db.py`) holds one connection in WAL mode with `foreign_keys = ON` and one
  `asyncio.Lock`. `transaction()` is `BEGIN IMMEDIATE` under that lock, so two button clicks
  cannot interleave; single statements (`execute`, `fetchone`, `fetchall`) take the same lock.
- Buttons and selects that must survive a restart are `discord.ui.DynamicItem`s whose
  `custom_id` carries a row id or a list index; the row is re-read on every click.
- Small cross-restart state (message ids of panels, "first run" markers, last-run stamps)
  lives in the `meta` key/value table, namespaced per cog (`selfroles:panel`, `ops:perms`,
  `tourney:<id>:auto`, ...) or per shared module (`quiet:<kind>`, written by
  `bot/quietgate.py`). It has no schema of its own, so grep the cog for its keys.
- "Is this member staff?" is one function, `cogkit.is_staff` (`bot/cogkit.py`: owner,
  administrators, Keeper and Moderator matched by slug), imported by name into each cog that
  asks. Different on purpose: `cogs/helpdesk.py` goes by permission (Moderate Members or
  Administrator) to decide which commands `/help` lists, and `Games.is_staff`,
  `Moderation.is_staff` and `Ops.is_staff` are methods of their cogs with the same rule.
- A few cogs reuse another cog's helpers directly: `cogs.lfg.ping_only`, `cogs.stats`
  (voice presence, used by economy and tempvoice), `cogs.clips` (clip detection, used by
  economy), `cogs.growth.BUMP_DUE_KEY` (votehub), `cogs.tournaments.ACTIVE` (sitedata: the statuses of
  a tournament that is on), `cogs.tempvoice` (matchmaking registers its
  match channels there), `cogs.stats.Stats.in_staff` and `playing` plus
  `cogs.vibes.LAST_REVIVAL_KEY` (tonight). Many cogs read other cogs' tables; only the owner writes them.

## Module map

Load order is the order in `MODULES`. "Writes" lists tables the cog writes besides `meta`
and `jobs` (most cogs with scheduled jobs write both, through `bot/jobs.py`: see Scheduled
jobs below). Times are local (Pacific unless `TZ` is set). Loops without a wall-clock time are plain `tasks.loop` intervals.

| Module | Owns | Writes | Scheduled work | Channels and roles |
|---|---|---|---|---|
| `lfg` | `/lfg` squad posts, Join/Leave/Close | `lfg_posts`, `lfg_members` | expiry sweep 5 min (posts close after 3 h) | 🎮・lfg forum; game roles, LFG |
| `stats` | voice/message/game-time recording, `/stats`, `/leaderboard`, `/privacy`, weekly MVP | `voice_sessions`, `game_sessions`, `message_counts`, `privacy_optout` (and, in `opt_out()`, deletes a member's rows on opt-out, in other cogs' tables too: see Privacy opt-out below) | MVP Sun 18:00; 1 min presence heartbeat | 💬・general |
| `growth` | invite tracking, Recruiter role, Disboard bump reminders, `/invites`, `/recruiters`, `/bumpers`, `/bumpping` | `invite_uses`, `joins`, `bumps` | bump reminder 1 min; recruiter sweep 24 h | 🤖・bot-commands; Bumper, Recruiter |
| `community` | welcome after Onboarding, `/report`, mod log of joins/leaves/bans/AutoMod | `welcomed`, `reports` | none | 💬・general, 📋・mod-log, 🛡️・mod |
| `starboard` | hall of fame at 3 stars | `starboard` | none | ⭐・hall-of-fame |
| `clips` | clip collection, weekly poll, winner role | `clips`, `clip_polls` | poll Sun 18:05 (open 24 h) | 📸・clips, 🛡️・mod; Clip of the Week |
| `tempvoice` | join-to-create voice, `/squad` | `temp_voice` | none (deletes 30 s after empty) | 06 · voice category, ➕ New Squad |
| `events` | `/gamenight`, `/watchparty`, the monthly Town Hall, 15-minute reminders, free games | `gamenights`, `hosted_events`, `free_games` | free games Thu 18:00; reminders 1 min; Town Hall job Saturdays at `TOWNHALL_TIME` (default 12:00), which schedules the first Saturday of a month a week ahead | 🕹️・gaming, game channels, voice channels, 🎙・stage (else 🔊 Lobby), 📣・announcements; Game Night |
| `economy` | earning (voice, chat, clips, squads, MVP, clip of the week), `/daily`, `/balance`, `/give`, `/coinflip`, `/richest` | `wallets` streak columns; coins via `economy.py` | voice coins 5 min | none of its own |
| `games` | `/slots`, `/blackjack`, `/trivia`, `/predict` | `predictions`, `prediction_bets`, `blackjack_open` | none (open blackjack bets refunded at start) | none of its own |
| `shop` | `/shop`, `/buy`, `/season`, `/raffle`, Spotlight | `perks`, `seasons` | raffle draw Sun 20:00; month-end season results; perk expiry 5 min | 💬・general, 📣・announcements; Hype, Season Champ, colour roles |
| `engagement` | QOTD, this-or-that poll, counting, auto game night, `/birthday` | `qotd_used`, `poll_used`, `counting`, `counting_run`, `birthdays`, `gamenights` | birthdays 09:00, QOTD 12:00, poll 18:00 daily (QOTD and poll ask `quietgate` first: see Quiet rooms); auto game night Fri 12:00 | 💬・general, 🔢・counting, 🕹️・gaming; Birthday, Counting Champ |
| `moderation` | `/warn`, `/timeout`, `/untimeout`, `/ban`, `/cases`, `/purge`, auto-escalation, anti-spam, anti-raid, the ban DM with the appeal code | `cases`, `appeal_bans` | raid restore check 30 s | 📋・mod-log, 🛡️・mod |
| `ops` | backups, error alerts, back-online note, drift check, permission self-check, `/status` | none | backup 04:00 daily; drift Sun 03:00; permission check 60 s after ready and 09:00 daily | 📋・mod-log |
| `utility` | `/remind`, `/reminders`, `/afk`, suggestion votes and tags, stat channels, tickets | `reminders`, `afk`, `tickets` | reminders 30 s; stat channels 10 min | 🆘・help, 💡・suggestions forum, 01 · front desk category |
| `cards` | welcome card image | none | retry failed cards every 10 min for 24 h | 👋・welcome |
| `tournaments` | `/tournament`, brackets, result confirmation, auto tournaments | `tournaments`, `tournament_entries`, `tournament_matches` | first Monday of the month 12:00 opens one, auto-start Sat 19:00; 1 min tick (reminders, nudges, 48 h staff flag) | 🏆・tournaments, 📋・mod-log; Tournament Champ |
| `achievements` | badges, `/profile` (asks `supporter` for the flair), `/badges` | `achievements` | hourly sweep | 💬・general |
| `creators` | `/creator` linking and verification, upload and go-live posts | `creators`, `creator_pending`, `creator_seen` | YouTube 15 min; Twitch 3 min; pending cleanup hourly | 📺・creators, 📋・mod-log |
| `levels` | XP from chat and voice, level roles, `/rank`, `/levels`, `/xp` | `xp` | voice XP sweep 10 min | 🤖・bot-commands, 📋・mod-log; Regular, Veteran, Elite, Legend, Mythic |
| `recap` | weekly recap, owner digest (with the "Games list" field from `logic/gamelist.py` and the "Newcomers" field from `logic/funnel.py`), member milestones, invite contest | none | recap Mon 10:00; digest Mon 10:05; contest on the 1st 12:00 | 📣・announcements, 📋・mod-log |
| `selfroles` | role panel and `/roles` | none | none (panel edited in place at start) | 🎭・roles; platform, region, play-time, ping and game roles |
| `partners` | `/partner apply/remove`, review cards | `partners` | dead-invite sweep Mon 12:00 | 🤝・partners, 📋・mod-log |
| `quests` | starter quest, `/quest`, nudge DM | `quest_steps`, `achievements` (Settled In) | hourly sweep | 💬・general |
| `heartbeat` | `bot/data/heartbeat` file, event-loop lag warning | none (a file, not the DB) | beat 60 s; lag probe 5 s | none |
| `challenges` | weekly challenges, `/challenges`, Claim button | `challenge_claims` | board Mon 09:00; auto-claim sweep hourly | 🎲・games |
| `wordgame` | Daily Word, `/word` | `word_games` | none (word changes at local midnight by date) | 🎲・games |
| `matchmaking` | `/queue`, match voice channels | `mm_queue`, `mm_matches`, `temp_voice` | expiry sweep 1 min (entries last 60 min) | 🎲・games (else 🤖・bot-commands), 06 · voice category |
| `econstats` | staff `/economy` (read-only) | none | none | none |
| `helpdesk` | `/help`, `/about`, rotating presence | none | presence every 5 min | 🎭・roles (link only) |
| `news` | official game news feeds, staff `/news test` | `news_seen` | poll every 30 min | every game channel |
| `staffapps` | `/apply staff/status`, `/apps list`, review cards | `staff_apps` | none | 📋・mod-log; Moderator (only if below the bot) |
| `seasonal` | seasonal events: drops, daily quest, `/event`, finale | `event_drops`, `event_claims`, `achievements` (event badge) | 1 min tick (seeded drop times 10:00 to 23:30, start and finale); quest sweep hourly | 💬・general, 🎲・games, 📣・announcements; Event Champ |
| `gamelink` | `/link`, `/unlink`, `/accounts`, Steam section on `/profile` | `game_accounts`, `game_account_pending` | upkeep 30 min (each account re-read at most daily) | 📋・mod-log |
| `votehub` | `/vote`, weekly bump thanks (the job, ledger reason and refs still say `support`) | none (reads `bumps`) | thanks Sun 17:00 | 🎲・games, 🤖・bot-commands |
| `catchup` | `/catchup` AI summary, staff `/catchup-usage` | `catchup_usage` | none | any public text channel (never the staff category) |
| `vibes` | chat revival (skipped within an hour of the `tonight` line; it also asks `quietgate`, which never refuses it), join anniversaries, booster thanks and stipend, member of the month | `qotd_used` | 1 min tick: anniversaries 11:00 daily; stipend on the 1st 12:00; member of the month on the 1st 12:30 | 💬・general, 📣・announcements |
| `houses` | three houses, `/house`, staff `/house-admin move`, weekly House Cup, quarterly trophy | `house_members`, `house_results` | placement and role sweep 10 min (one role change a second; a one-time introduction post when the first members are sorted); 1 min tick: House Cup Mon 10:10, season close on the 1st 10:20 (acts when a quarter ended) | 📣・announcements; House Fern, House Kestrel, House Otter |
| `crew` | Welcome Crew: `/crew` with Join/Leave buttons, the crew ping, greeting reward, staff `/crew-stats` | `crew_arrivals`, `crew_greetings`, `achievements` (Welcoming Committee) | 1 min tick (crew ping, first-time joins only; hourly upkeep: opt-out cleanup, role taken back after a case) | 💬・general, 📋・mod-log; Welcome Crew |
| `supporter` | Supporter tier: thank-you, `/profile` flair, stipend, `/support`, staff `/supporter grant/revoke` | `supporters` | reconcile 5 min; stipend on the 1st 12:15 | 💬・general, 📋・mod-log; Supporter (only read, unless staff grant it by hand) |
| `forums` | first reaction on guide and setup posts, delayed one-time post reward | `forum_posts` | settle sweep 10 min (a post is judged once it is a day old) | 📚・guides and 🖥・setups forums |
| `clipcast` | weekly top clips to YouTube with per-clip consent, `/clipcast consent/remove` (coins are paid only after 7 days up), staff `/clipcast status/skip` | `clipcast`, `clipcast_prefs`, `achievements` (On the Channel) | 5 min tick: opt-out and crash sweep, 7-day payouts, ask Sun 18:30, post Tue 18:30 | 📸・clips, 📋・mod-log, DMs |
| `realms` | Minecraft bridge: status panel, `/smp link/unlink/status/top`, mods `/smp remove/adopt`, two-way chat, playtime coins | `realms_links`, `realms_playtime` | 15 s tick (server poll every 2 min, backing off to 15 min while it is down; departed-member sweep 30 min); RCON sends only `list` and `tellraw`, never `whitelist`; chat flush 3 s | ⛏️・monkey-smp, 📋・mod-log |
| `wrapped` | `/wrapped` cards (Dec 1 to Jan 15), server card, staff `/wrapped-preview` | `achievements` (`wrapped_YEAR`); `meta` only otherwise | 5 min tick: announcement Dec 1 10:00, reminder Dec 20 10:00, level snapshot once in January | 📣・announcements; wherever a member shares a card |
| `sitedata` | aggregates file for the landing page, staff `/sitedata refresh` | none (writes `bot/data/site/stats.json`) | 1 min tick, rebuilds every 30 min | none |
| `appeals` | ban appeals from the website: review cards with Accept/Deny, discussion threads, staff `/appeals list/code` | `appeals` | 5 min tick: re-reads the channel for webhook posts missed while down; expiry daily 09:00 (30 days untouched; answers blanked 180 days after a decision) | 🔒・appeals, 📋・mod-log |
| `tonight` | one line in general while 2+ non-bot members are in a public voice channel (everyone can view and join; not AFK, not the staff category): counts, the shared game, a channel mention; edited in place, past tense when the rooms empty, never deleted. No commands, no coins | none (`meta` only: `tonight:state`, one JSON value with no member data, and `tonight:fired_at`) | 1 min tick: line after a room has held 3 min, edits at most every 2 min, same message reused for 45 min after a session ends, at most 3 new messages a local day; `TONIGHT=off` runs no tick and closes a standing "right now" line once | 💬・general |
| `welcomers` | one line in general pinging the opt-in Welcomer role when a member who joined in the last 14 days first joins a public voice channel (`Tonight.public_room`) and stays 30 s; "first" is `voice_sessions` history (read only). Skipped for bots, tracking off, a Welcomer or staff already in the channel, nobody else holding the role, quiet hours (`crew.quiet_hours` of `CREW_QUIET_HOURS`). No commands, no coins | none (`meta` only: `welcomers:last_ping`, and `welcomers:pinged:<id>` per newcomer, pruned after 30 days) | 10 s tick (joins held 30 s); at most one ping per 15 min; prune hourly | 💬・general; Welcomer |
| `smpwatch` | one line mentioning SMP Ping in the realms channel when 3+ are on Monkey SMP: a Server List Ping (the `realms` codec) of the public address, found through its SRV record by a minimal UDP DNS client in `logic/smpwatch.py`; reads the count only, no RCON, no names. No commands, no coins | none (`meta` only: `smpwatch:state`, when the last ping went out and whether the next may go) | 5 min tick; SRV answer cached 1 h; after a ping, none until the count drops below the minimum and 4 h pass; none 22:00 to 09:00 local; `SMP_WATCH=off` runs no tick | ⛏️・monkey-smp; SMP Ping |
| `fieldnotes` | the owner's weekly field notes: Built (GitHub search plus the owner's repo listing, public repos only, no token), Heard (SpotifyForge's `listening_history.jsonl`, read only; no Spotify token), Watched (Letterboxd RSS), Played (the guild owner's `game_sessions` rows, unless opted out); a reply thread; the closing question asks `quietgate` (kind `fieldnotes`); staff `/fieldnotes preview` (changes nothing) | none (`jobs` rows `fieldnotes:YYYY-Www`; `meta` `quiet:fieldnotes`) | 5 min tick: Sun 18:00 weekly; an empty week is marked done with no post; no `FIELD_NOTES_*` setting runs no tick | 📓・field-notes |

Four of the `logic/` modules have no cog of their own: `linkedroles.py` validates
`site/api/linked-role/metadata.json` and plans the registration diff for
`server/register_linked_roles.py`, `wrappedcard.py` draws the Wrapped cards, and `gamelist.py`
compares 28 days of `game_sessions` with `config.GAMES` and `config.RETIRED_GAMES` for the
owner digest (`Recap.game_list_line`): an unlisted game needs 3 distinct players and 5 hours
combined to be named, presence names go through the text filter and are markdown-escaped,
bots and opted-out members are excluded, and there is no field at all when nobody played.
`funnel.py` is the digest's "Newcomers" field (`Recap.newcomers_line`). It stores nothing: the
cog hands it rows the other modules already keep (`joins`, `message_counts`,
`voice_sessions`, the `pick_roles` step in `quest_steps`, and who holds a platform, region or
game role right now). For the digest's week and the 28 local days that end with it, it counts
how many joined, left since, picked roles, said something and joined voice; for the week
before's joiners, how many are still here and how many came back in week 2; `reading` picks
one sentence about the week. Counts only, never a member or an id. Bots never count.
Opted-out members count as joined and left and are left out of every later step, numerator
and denominator, so the steps read "of the N with stats on". There is no field when nobody
joined in 28 days, and a failure leaves the field out without breaking the digest.
(`logic/quiet.py` has no cog either; its driver is `bot/quietgate.py`: see Quiet rooms.)
`logic/sitedata.py` shares its schema with the publisher through `server/site_schema.py`.
Cross-cog reads added with these modules, all by `bot.get_cog(...)` and all optional (a missing
or failing cog just leaves its line out): `recap` asks `Forums.fresh_line` for the weekly recap
and `Crew.digest_line` for the owner digest, `ops` asks `Realms.health_text` for `/status`,
`achievements` asks `Supporter.profile_flair`, and `sitedata` and `wrapped` read house standings
and membership through `logic/houses.py`.

`vibes` and `tonight` keep out of each other's way through two `meta` keys: `tonight` writes
`tonight:fired_at` when a session's line appears and `vibes` skips its chat revival for an
hour after it; `vibes` writes `vibes:last_revival` and `tonight` holds a new session's line
until an hour after that. Each only reads the other's key (`logic/tonight.py` `REVIVAL_GAP`).

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
and `established_sql(...)` for use inside queries. These are the only place the rule is written:
a module with its own named gate (`tournaments.can_join`, `wordgame.payable`, `shop.can_enter`,
`quests.eligible_for_reward`) calls `established` for the age part and takes the user id, not a
`created_at`. Two uses: the earner must be established to
be paid (quest, challenges, tournaments, seasonal, Daily Word, `/give`, vote thanks, MVP,
`/catchup`), and where other people's actions earn someone progress (stars, poll votes, squad
joins, voice company) only established accounts count, so alts cannot hand out rewards.
Staff applications use their own 90-day rule in `logic/staffapps.py`.

**Privacy opt-out.** `db.tracking_allowed(user_id)` is the one gate (`privacy_optout` table).
Every recorder of activity (stats, coins, XP, quest steps, clips) checks it first; readers
that rank or show members filter opted-out members out. `/privacy tracking:off` also deletes
the member's voice, message and game history and XP. `opt_out()` in `cogs/stats.py` does
all of it in one transaction, for the newer tables too: it deletes `realms_playtime`,
`forum_posts` and `crew_greetings` rows (either side), removes the member from the level
snapshots in `meta`, deletes a Wrapped share time whose 6-hour cooldown is over, and then asks
`clipcast` to withdraw consent for anything not posted (a posted video needs `/clipcast
remove`). The rule is "opting out removes the detail, never a limit": every cap, cooldown and
once-only reward is either counted from the ledger, which is never deleted (forum weekly cap,
crew daily cap, playtime slots), or its bare row is kept (`crew_arrivals`, which tells a rejoin
from a first join; a Wrapped share time still inside its cooldown). The cogs' own sweeps
(`crew` hourly, `realms` at each poll, `clipcast` and `wrapped` every 5 minutes) remain as a
second pass. A new table about members needs a line in `opt_out()`. Things a member does on purpose (a
partner or staff application, Daily Word, a linked account, a house, a Minecraft link, a ban
appeal) are not tracking and are not gated. If a change stores something new about members, update `PRIVACY.md`.

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

**Scheduled jobs (`logic/schedule.py`, `bot/jobs.py`).** A job is a `Weekly(name, weekday, hour, minute)`;
`plan()` returns the one `Period` to run now and any older ones to mark done. Semantics:
only the latest due period can ever run; the very first sighting of a job only marks it done
(turning a module on never replays history); missed periods since first sight are marked
done, not run; a finished period's key (`name:YYYY-Www`) goes into `jobs` after the work
succeeded, so a failure retries on the next tick and a success never repeats. `first_seen:<job>`
lives in `meta`. `Daily` (`logic/ops.py`, `logic/engagement.py`), `Monthly`
(`logic/recap.py`, `logic/tournaments.py`) and `Annual` (`logic/wrapped.py`) follow the same
rules with day, month and year keys; each has its own planner with the same signature. Cogs
poll with a 1 to 10 minute `tasks.loop` and ask the planner; nothing sleeps until a wall-clock
time.

The planners are pure; the database half is `bot/jobs.py`, a plain module with no Discord
imports and no clock of its own. `due(db, job, tz, now, planner=schedule.plan)` reads
`first_seen:<job>` and the job's finished keys (`key LIKE '<job>:%'`), asks the planner, writes
first_seen and the skipped periods in one transaction and returns the period to run, unmarked.
`done(db, period, now)` marks it after the work succeeded. `run_due(db, job, tz, clock, action,
planner)` is both: it marks the period only when `action(period)` returns truthy, and an
exception goes to the caller with nothing marked. Fifteen cogs run their jobs through it:
`appeals`, `challenges`, `clipcast`, `clips`, `engagement`, `events`, `houses`, `partners`,
`recap`, `stats`, `supporter`, `tournaments`, `vibes`, `votehub` and `wrapped`. What differs
stays in the cog: `events` catches its action's errors and gives up a day after the scheduled
time, and `appeals` calls `due` and `done` itself (`run_expiry`), from a tick that runs the
channel sweep and the daily expiry as two steps that fail on their own. Because the keys are
read with `LIKE`, a job name must not differ from another only by `_`, `%` or letter case.

A period is marked done only when its work could happen. In `stats` (weekly MVP), `clips`
(clip poll) and `clipcast` (the ask, the publish, and the staff hand-over when YouTube is not
connected) the action returns False when the server, its channel or, for the hand-over, the
mod log is missing, before anything is sent, paid, uploaded or handed over. `appeals` leaves
the day unmarked while the server is unavailable. The period is then tried again on every
tick until the next period is due, when the planner marks the old one done without running
it; `stats`, `clips` and `clipcast` log the wait once per period, not on every tick. Handled
with nothing to post
still returns True: a quiet week with no MVP, fewer than two clips, no approved clips.
`tournaments` does the same when the tournaments channel is missing: the monthly auto
tournament waits for it, until it is too late to open sign-ups for that month. What keeps a
retry from doing anything twice is unchanged: ledger refs, the `clip_polls` row and the
`clipcast` rows.

Three drivers are still their own, on purpose: `ops` (backup, drift, permission check: it reads
the keys with `substr`, writes first_seen in a statement of its own and stamps skipped periods
with a second clock read), the `shop` raffle (its periods come from ticket rows in the ledger
and there is no first_seen) and the `seasonal` drops (a slot's key is claimed before the post
goes out, so a crash loses a drop rather than repeating one). Their `jobs` keys never collide
with the ones above.

**Quiet rooms (`logic/quiet.py`, `bot/quietgate.py`).** A recurring prompt that asks members
for a response is a "kind": the question of the day (`QOTD`), the daily poll (`POLL`) and the
chat revival (`REVIVAL`) and the question that ends the weekly field notes (`FIELD_NOTES`). The rule is pure and lives in `logic/quiet.py`: post unless the last
3 prompts of the kind were all ignored; a sleeping kind wakes when a member writes in the
channel after its last prompt, and if nobody does, one probe goes out 7 days after that
prompt (a probe nobody answers leaves the kind asleep for another 7 days).
`bot/quietgate.py` is the driver, a plain module like `jobs.py`. A cog calls
`quietgate.allowed(bot, kind, channel, now)` before it picks anything and
`quietgate.posted(db, kind, message, at)` after the send. Who calls them:

- `cogs/engagement.py`, for the question of the day and the poll. It passes the period's
  scheduled time to both calls. A refusal returns True to `run_due`, so the day is marked done
  and nothing is taken from the question or poll bank.
- `cogs/vibes.py`, for the chat revival. It passes `last_member_at`, so nothing is read from
  Discord. Its own rule already needs a member message since the last revival, so the gate
  never refuses it.
- `cogs/fieldnotes.py`, for its closing question. It passes the period's scheduled time; a
  refusal drops only the question line, and the notes still go out.
- `cogs/ops.py` reads `quietgate.status_lines` for the "Paused prompts" field of `/status`.

Without `last_member_at`, `allowed` fetches the newest prompt once (poll votes, a reaction
the bot did not add, the message count of the answers thread, and the voter list when a poll
shows no votes) and, only when that shows nothing, reads at most 50 channel messages after it
(a member's reply is an answer; any member message wakes a sleeping kind). This is why
`engagement` needs Read Message History in general (`CHANNEL_NEEDS`). State is one JSON value
per kind in `meta` under `quiet:<kind>` (`quiet:qotd`, `quiet:thisorthat`, `quiet:revival`, `quiet:fieldnotes`):
the last 3 prompts as time, message id and answered, and whether the kind is asleep. No member
ids. The keys are live data, so never rename a kind's key. The gate fails open: whatever
cannot be read (a deleted prompt, a missing permission, an outage) never counts against a
prompt, an exception in `allowed` posts the prompt, and `posted` never raises. Staff get one mod-log
line when a kind pauses and one when it resumes, none for a probe. `QUIET=off` in
`server/.env` turns the rule off: `allowed` returns True without reading anything, `posted`
records nothing and `/status` shows no field. Informational posts (free games, recaps,
results, welcomes, the voice line) are not kinds and do not go through the gate.

**Permission self-check (`cogs/ops.py`).** About a minute after ready and daily at 09:00 the
bot compares what the loaded cogs need (`logic/ops.py` tables) with what it actually has:
channels exist, channel permissions, server permissions, and handed-out roles exist and sit
below its own top role. One mod-log embed when the set of problems changes, one when it
clears; the current count shows in `/status`. A test fails when a module in `MODULES` has no
`CHANNEL_NEEDS` row and no reason in `NO_CHANNEL`, or when a cog whose source calls
`add_roles` / `remove_roles` is in neither `ROLE_HOLDERS` nor `NO_ROLE_HOLDER`. It checks that
each cog is listed, not that every channel a cog posts in has a row: that part is still by hand.

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
| Houses | `house_members` (one row per member ever placed; `left_at` set = left), `house_results` (one frozen row per closed week and per season) | 16 |
| Welcome Crew | `crew_arrivals` (latest arrival per member), `crew_greetings` (one per newcomer ever) | 17 |
| Supporter tier | `supporters` | 17 |
| Forums | `forum_posts` (state: pending, paid, unpaid, skipped, deleted) | 18 |
| Watch parties, Town Hall | `hosted_events` (kept out of `gamenights` so game-night counts leave them out) | 18 |
| Clipcast | `clipcast` (one row per shortlisted clip; `status` is the state machine in `logic/clipcast.py`), `clipcast_prefs` | 19 |
| Monkey SMP | `realms_links` (state: new links are always ok; add and remove are left from when linking ran the whitelist and are settled when the cog loads; unique index "one name per member"), `realms_playtime` (seconds per member per local day); `realms_links.managed` and `attempts` are unused since links stopped touching the whitelist (kept, since migrations are never edited) | 20; 22 |
| Ban appeals | `appeals` (unique index "one open appeal per user"), `appeal_bans` (when a ban happened; written by `moderation`) | 21 |

Wrapped, sitedata, Linked Roles, focus, tonight, smpwatch, welcomers and fieldnotes add no tables. Wrapped keeps its share cooldown and the
January level snapshot in `meta` (`wrapped:shared:YEAR:USER`, `wrapped:levels:YEAR`). Quiet
rooms and the digest's Newcomers field add none either: quiet rooms keeps one `meta` value per
prompt kind (`quiet:<kind>`), and the Newcomers field stores nothing.

Derived, not stored: badges, challenge progress, quest steps, event points, recap numbers,
season standings, the running week's house points and everything on a Wrapped card are
recomputed from the owning tables every time, so a restart or a missed
event cannot lose progress. Raffle tickets are ledger rows. No message text is stored; the
exceptions are a quoted reported message and the hall-of-fame copy (both in Discord, not the DB),
the title of a guide or setup post, and the answers on a ban appeal (blanked after 180 days).

Other files in `bot/data/`: `bot.log*`, `bot.lock`, `heartbeat`, `backups/` (fallback backup
target), `migrate/` (host-move archives), `site/stats.json` (the landing page's live data,
written by `sitedata`), and the two tracked banks `qotd.txt` and `polls.txt`.

## Server as code (`server/`)

| Script | Role | Changes Discord? |
|---|---|---|
| `layout.py` | Source of truth: `GAMES` and `RETIRED_GAMES`, roles, categories and channels, Onboarding (the core default channels, `FOCUS_GROUPS` and the other `FOCUS_*` lists), AutoMod, `PUBLIC` text, `TRIM` | no (data) |
| `names.py` | `slug()`: name matching that ignores emoji, case and separators | no |
| `setup_server.py` | First build: create or restyle roles and channels, **reorder roles**, post rules and welcome | with `--apply` |
| `polish_server.py` | Second pass: Community, Onboarding, welcome screen, AutoMod, media lock, trim empty channels | with `--apply` |
| `public_mode.py` | Later changes: missing roles and channels, private categories, description, invite, Onboarding prompts, AutoMod gaps, and the games (role, channel, forum tag and "What do you play?" option for each game in `GAMES`; `restore_games` moves a re-listed game's channel back). Additive except for retired games: a game in `RETIRED_GAMES` loses its Onboarding option (in `game_options`, in the same edit that adds a newly listed game, or else in `retire_games`) and its channel moves into the staff-only `GAME_ARCHIVE` category (`99 · archive`; `retire_games` only). Never deletes: messages, roles and forum tags stay | with `--apply` |
| `focus_mode.py` | The short newcomer channel list, in one Onboarding edit: default channels become `ONBOARDING_DEFAULT_CHANNELS` (the core of 10) and one question, `FOCUS_PROMPT`, offers the `FOCUS_GROUPS`. `--plan` prints the plan from `layout.py` without connecting and fails when a public channel is in no list or in two; `--revert` restores `ONBOARDING_EVERYTHING` as the defaults and leaves the question. Never touches permissions, channels, roles or the other questions | with `--apply` |
| `expressions.py` | Upload emoji and soundboard sounds from `assets/expressions/` (skips existing names) | with `--apply` |
| `snapshot_server.py` + `snapshot_lib.py` | Read-only dump of live config to `server/snapshot/*.json` (names not IDs, sorted); `--compare` lists drift from `layout.py` | never |
| `watchdog.py`, `install_watchdog.ps1` | Downtime watchdog and its Task Scheduler install | no (webhook alert only) |
| `migrate_host.py` | `pack` / `unpack` the database for a host move (database and manifest only, never `.env`) | no |
| `site_schema.py` | Schema and leak guard for `stats.json`: a whitelist of keys, types and game names that refuses anything ID-like, mention-like or link-like. Imported by the bot and by the publisher | no (pure) |
| `publish_site_data.py` | Checks `bot/data/site/stats.json` again and pushes it as one parentless commit to the `site-data` branch (force-push with a lease, from a throwaway repository in the temp directory; any other branch is refused) | no (it changes GitHub, with `--apply`) |
| `register_linked_roles.py` | Writes the Linked Roles metadata records and the verification URL to the Discord application (`docs/LINKED_ROLES.md`) | the application, not the server; with `--apply` |

Every script dry-runs by default. None deletes roles or channels that are missing from
`layout.py`; they are listed for a human. Retiring a game is the one thing that takes something
away from members, and `public_mode.py` does it in two steps: `game_options` drops the retired
games' options from "What do you play?" in the same Onboarding edit that adds a newly listed
game (Discord caps the options of one question), and `retire_games` removes any option that is
still there and then hides the game's channel in the archive. The channel is only hidden after
its option is gone, and `restore_games` runs before a re-listed game becomes an option again. Because Front Desk now sits below Keeper and
Moderator, `setup_server.py` (which calls `edit_role_positions`) must not be run against the
live server again: use `public_mode.py`.

Two drift checks exist and compare different things:

- `snapshot_server.py --compare`: live server versus `layout.py` (run by hand).
- `ops` weekly drift job (Sun 03:00): live roles and channels, built with the same
  `snapshot_lib` helpers, versus the committed `server/snapshot/*.json`. It posts a summary to
  the mod log and writes nothing to the repo. After an intended server change, re-run
  `snapshot_server.py` and commit the snapshot so the next check is quiet.

## Landing page (`site/`)

Plain HTML, CSS and JS with a strict CSP in `vercel.json` (no inline script or style;
`connect-src` is this origin, `https://discord.com` and `https://raw.githubusercontent.com`). The
invite code lives only in `site/config.js`; `app.js` shows live counts from Discord's public
invite endpoint and `join/` redirects to the invite. The games block in `index.html` is
generated from `layout.GAMES` by `python -m logic.sitegen` (run in `bot/`), and
`bot/tests/test_sitegen.py` fails when the page drifts from `layout.py` or breaks the CSP.
`logic/textfilter.py` reads the server's own invite code from `site/config.js` to allow it.

Live data: `cogs/sitedata.py` writes `bot/data/site/stats.json`, `server/publish_site_data.py`
pushes it to the `site-data` branch, and `app.js` fetches `STATS_URL` (`config.js`) from
raw.githubusercontent.com, checks every field again and renders with `textContent`. The live
section stays hidden when the fetch fails or the file is older than 48 hours. `site-data` is a
data branch holding one force-pushed commit; nothing but the publisher may write to it.

Functions: Vercel builds each `.js` file under `site/api/` as a Node function. They have no
dependencies and there is no `package.json`; files and folders starting with `_` are not
endpoints. Each answers 503 and does nothing until its variables are set.

| Path | File | What it does | Vercel env vars |
|---|---|---|---|
| `POST /api/appeal` | `api/appeal.js` | Backend of the appeal form (`appeal/index.html`, `appeal/appeal.js`). Checks the appeal code (an HMAC of the user ID, the same as `logic/appeals.py`) and forwards a small JSON payload to a webhook in the staff appeals channel, where `cogs/appeals.py` verifies it again. A wrong code gets the same 200 as a right one | `APPEAL_SECRET`, `APPEAL_WEBHOOK_URL`, optional `APPEAL_ORIGIN` |
| `GET /api/linked-role/start` | `api/linked-role/start.js` | Sets the signed state cookie and redirects to Discord's consent screen | `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `LINKED_ROLE_STATE_SECRET`, `LINKED_ROLE_REDIRECT_URI` |
| `GET /api/linked-role/callback` | `api/linked-role/callback.js` | Exchanges the code, reads the member's connections once, writes the role-connection metadata to Discord and redirects to `/linked-role` with a result flag. Shared code is in `api/linked-role/_lib.js`, the records in `metadata.json` | the same four |

Tests: `node --test` run in `site/api/` (CI runs them the same way). The GitHub Pages mirror is static and has no
functions, so `/appeal` says so there and Linked Roles must use the Vercel domain.

## Deployment

| Piece | Today | Notes |
|---|---|---|
| Bot | Windows Task Scheduler task "Front Desk bot" runs `pythonw main.py` in `bot/` from the shared `server/.venv`, at boot and logon | Stop the task (and pause the watchdog) before running `main.py` by hand; the lock file refuses a second copy |
| Watchdog | Task "Front Desk watchdog", `server/watchdog.py` every 5 minutes | Installed by `server/install_watchdog.ps1` |
| Backups | `ops` daily 04:00, SQLite online backup, integrity-checked, newest 14 kept | `config.BACKUP_DIR`, falling back to `bot/data/backups` when that drive is missing |
| Container kit | `Dockerfile`, `docker-compose.yml`, `bot/run_container.sh` | Not the live deployment. Read-only root, non-root user, data volume, heartbeat healthcheck and an in-container monitor with the watchdog's thresholds. Steps and cut-over checklist: `docs/HOSTING.md` |
| Site | Vercel project rooted at `site/`, production branch `main` | No build or install command. The functions under `site/api/` read their settings from the Vercel project's environment variables |
| Site data | A scheduled task the owner creates runs `server/publish_site_data.py --apply --quiet` every 30 minutes | Needs git push access to `origin` on that machine; exits 0 without output when there is nothing to do |
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
| Run something on a schedule | Define a `Weekly` / `Daily` / `Monthly` job in `logic/` with a name no other job has, poll it from a `tasks.loop` with `jobs.run_due(self.db, JOB, self.tz, now, action, planner)` (`bot/jobs.py`), and have the action return True only when the period is handled. Don't write `first_seen:` or `jobs` rows by hand |
| Post a recurring prompt that asks for a response | Add a `Kind` to `logic/quiet.py` (`KINDS` too), call `quietgate.allowed` before picking anything and `quietgate.posted` after the send, and treat a refusal as handled |
| Post in a channel or hand out a role | Add the name to `bot/config.py` (and to `server/layout.py` if it does not exist yet, created with `public_mode.py`), then add a `Need` / `ROLE_NEEDS` entry in `bot/logic/ops.py` so the self-check covers it |
| Repost member text | Run it through `logic/textfilter`, escape markdown, send with `ping_only(...)` or `AllowedMentions.none()` |
| Record member activity | Check `db.tracking_allowed` first; make opt-out delete it (`cogs/stats.py` `/privacy`) |
| Add or retire a game | Move the game's line between `GAMES` and `RETIRED_GAMES` in `server/layout.py` (or add a new line to `GAMES`), regenerate the site block with `logic.sitegen`, dry-run then `--apply` `public_mode.py`, restart the bot. For a new game add a feed to `bot/assets/news_sources.json` if one exists (a retired game's entry is kept and ignored) and a team size in `logic/matchmaking.py` `DEFAULT_SIZES` if it has a usual one |
| Add a public channel | Besides `CATEGORIES`, put it in exactly one of `ONBOARDING_DEFAULT_CHANNELS`, a `FOCUS_GROUPS` group or `FOCUS_UNLISTED` in `layout.py`; `focus_mode.py --plan` and a server test fail otherwise |
| Change the server's structure | Edit `layout.py`, dry-run then `--apply` `public_mode.py`, re-run `snapshot_server.py` and commit the snapshot |
| Add a persistent button | A `DynamicItem` with ids in `custom_id`, registered in the cog's setup; re-read state from the DB on click |

Related docs: `README.md` (features, runbook), `PRIVACY.md`, `docs/ECONOMY.md`,
`docs/HOSTING.md`, `docs/LISTING.md`, `docs/LINKED_ROLES.md`, `docs/WAVE9_SETUP.md`,
`site/README.md`, and the original design and build
history in `docs/superpowers/specs/2026-10-01-front-desk-bot-design.md`.
