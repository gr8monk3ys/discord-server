# discord-server

Everything for Lorenzo's Server in one place:

1. **Client theme**: the Field Notebook look, matching lscaturchio.xyz. Night or paper
   ground, forest-green ink, Fraunces headings and IBM Plex Mono wall labels.
2. **Server setup** (`server/`): roles, channels, Onboarding and AutoMod as code.
3. **Front Desk** (`bot/`): the server's own bot, with squad-up, stats and more.

Roadmap and design: `docs/superpowers/specs/2026-10-01-front-desk-bot-design.md`.

## 1. Client theme (`theme/field-notebook.theme.css`)

Works with **Vencord** (recommended) or **BetterDiscord**.

- **Vencord:** Settings → Vencord → Themes → *Open Themes Folder*, drop the file in, then toggle it on.
- **BetterDiscord:** Settings → Themes → *Open Themes Folder*, drop the file in, then toggle it on.

Discord's own **Appearance** setting picks the variant: Dark, Darker or Midnight
gives the night notebook, and Light gives the paper one.

Heads up: both mods break Discord's Terms of Service. Bans for themes alone are
very rare, but it's your call. Discord renames its internal classes every few
months. If a detail stops applying (for example the mono category labels), the
colours and fonts still work because they're driven by CSS variables.

## 2. Server makeover (`server/`)

This is a one-shot bot run. It creates or renames roles, categories and channels,
makes the front-desk channels read-only, sets the join-message and AFK channels,
and posts rules and welcome embeds. It **never deletes** anything. Anything not
in `layout.py` gets listed so you can clean it up by hand.

**One-time setup**

1. Go to <https://discord.com/developers/applications> → **New Application** → **Bot** → **Reset Token**, and copy the token.
2. Copy `.env.example` to `.env` and paste the token and your server ID into it.
3. Invite the bot with Administrator (replace `APP_ID` with the Application ID from the General Information page):
   `https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot&permissions=8`
4. Server Settings → Roles: drag the bot's role to the **very top**.

**Run it**

```bash
cd server
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python setup_server.py          # dry run: shows the plan
.venv\Scripts\python setup_server.py --apply  # do it
```

Edit `layout.py` first. `GAMES` at the top holds your group's actual games, and
each one gets a ping role plus a channel. The script is safe to re-run: it
matches existing channels and roles by plain name, so `#general` becomes
`#💬・general` instead of a duplicate.

**Second pass: Discord's built-in features**

```bash
.venv\Scripts\python polish_server.py          # dry run
.venv\Scripts\python polish_server.py --apply
```

This turns on Community and **Onboarding**, so new members pick their own game
roles on join. It also sets up **AutoMod** (spam, mention raids and slurs, with
alerts to the mod channel), defaults notifications to @mentions only, and
removes the empty channels listed in `TRIM`. Everything is configured at the
bottom of `layout.py`. The bot's role must be at the top for Onboarding to
assign roles.

The server icon comes from `assets/make_icon.py` (`pip install pillow`).

**Hosted bots (always online, set up in their dashboards)**

| Bot | Job | Setup |
|---|---|---|
| [Captcha.bot](https://captcha.bot) | DMs new members a web captcha; passing gives **@Verified** | Verification channel `#💬・general`, role `@Verified` (Add role). Its role must sit above @Verified. |
| ~~[Lurkr](https://lurkr.gg)~~ | Replaced by Front Desk Levels | Turn leveling off in its dashboard, then kick it and delete its role, so members don't get two level numbers and two sets of level-up pings. |
| [GiveawayBot](https://giveawaybot.party) | Giveaways | Nothing to set up: `/gstart <time> <winners> <prize>`. |

MEE6 was tried and dropped: its Levels and Welcome plugins now need Premium.

`polish_server.py` also handles the **media lock**: `@everyone` can't post images,
files or link embeds until they have one of the `MEDIA_ROLES` (Captcha.bot's
@Verified covers new members). The bot roles are ordered by the `keep` entries
in `layout.ROLES`, and every bot gets `@Bots` so they're grouped in the member list.

**Afterwards**

- Give yourself **@Keeper** and your friends **@Squad**.
- Take Administrator back off the bot's role. Front Desk now stays as the server's
  permanent bot (section 3) with only the permissions it needs.
- For later changes use `public_mode.py`: it only adds things and never reorders or deletes,
  so it works with Front Desk sitting below the staff roles.
- Reset the bot token in the portal if you shared `.env` anywhere.
- Optional: set `ICON_PATH` in `layout.py` to a square image for the server icon.

### Emoji & sounds

The server has its own emoji pack (20 emoji, such as `:gg:`, `:take_w:`, `:clutch:`, `:touchgrass:`
and `:frontdesk:`) and 8 soundboard sounds. Code generates all of them from scratch: Pillow
draws the emoji, numpy synthesizes the sounds, and the lettering is a hand-made block font.
No samples, fonts or outside art are used.

```
.venv\Scripts\pip install pillow numpy                     # ffmpeg must be on PATH for the MP3s
.venv\Scripts\python ..\assets\expressions\make_emoji.py   # -> assets/expressions/emoji/*.png
.venv\Scripts\python ..\assets\expressions\make_sounds.py  # -> assets/expressions/sounds/*.mp3
.venv\Scripts\python expressions.py                        # dry run: what fits in your slots
.venv\Scripts\python expressions.py --apply                # upload
```

The bot's role needs **Create Expressions**. **Manage Expressions** is only needed if you later
want it to edit or delete them. Names that already exist are skipped, and anything over the
emoji or soundboard slot limit (8 sounds unboosted) is listed rather than uploaded. Sounds are
MP3 because discord.py only accepts MP3 for the soundboard.

### Snapshot

`snapshot_server.py` is read-only. It writes the live server's config to `server/snapshot/`
(`server`, `roles`, `channels`, `onboarding`, `automod` and `welcome` `.json`). It uses names,
not IDs, and sorts its output, so `git diff server/snapshot` shows exactly what changed since
the last snapshot you committed. It never records members, messages, invites or the token.

```
cd server
.venv/Scripts/python snapshot_server.py            # write the snapshot
.venv/Scripts/python snapshot_server.py --compare  # also list roles/channels that differ from layout.py
.venv/Scripts/python -m pytest tests               # unit tests
```

If the bot lacks a permission for a fetch (AutoMod rules need Manage Server), that file
gets an `"error"` note instead and the rest is still written.

## 3. Front Desk bot (`bot/`)

Front Desk is the server's own bot. It runs on LORENZO-COMPUTE around the clock and needs no
day-to-day attention. Design and history:
`docs/superpowers/specs/2026-10-01-front-desk-bot-design.md`. Privacy: [PRIVACY.md](PRIVACY.md).

### What it does

| Area | Commands and automatic jobs |
|---|---|
| Squad-up | `/lfg` posts in 🎮・lfg with Join/Leave/Close buttons; posts close after 3 h; a host's posts ping the game roles at most once per 30 min (later ones post without pings) |
| Stats | `/stats`, `/leaderboard`, `/privacy`; weekly MVP Sundays 18:00 (accounts 30+ days old; fresh accounts don't count as voice company for it) |
| Growth | `/invites`, `/recruiters` (Recruiter role), `/bumpers`, `/bumpping`; Disboard bump reminders |
| Community | welcome after Onboarding, `/report` and "Report message", mod log |
| Hall of fame | 3 ⭐ from other members (accounts 30+ days old) reposts to ⭐・hall-of-fame (public channels only) |
| Clips | clip of the week poll Sundays 18:05; winner gets Clip of the Week (only votes from accounts 30+ days old count, and not the clip author's own) |
| Voice | join ➕ New Squad for your own channel; `/squad name`, `/squad limit`, `/squad claim` |
| Events | `/gamenight`; 15-min reminders; free Epic/Steam games Thursdays 18:00 |
| Economy | `/daily`, `/balance`, `/give` (accounts 30+ days old), `/coinflip`, `/richest`; coins for voice, chat, clips, squads |
| Games | `/slots`, `/blackjack`, `/trivia`, `/predict` |
| Shop and seasons | `/shop`, `/buy` (colour role, Hype, shoutout), `/season`; monthly champions |
| Engagement | question of the day 12:00, this-or-that poll 18:00, 🔢・counting, auto game night Fridays, `/birthday` |
| Moderation | `/warn`, `/timeout`, `/untimeout`, `/cases`, `/purge` (shown only to members with Timeout Members, like `/xp` and `/suggestion`); auto-escalation, anti-spam, anti-raid |
| Operations | daily DB backup 04:00, error alerts, back-online note, weekly config drift check, `/status` |
| Utility | `/remind`, `/reminders`, `/afk`, suggestion voting, member/online stat channels, tickets in 🆘・help |
| Welcome cards | an image card in 👋・welcome when someone finishes Onboarding (names them without a ping; the #general welcome is the one ping) |
| Tournaments | `/tournament create/start/cancel/bracket`; sign-up buttons, brackets with byes, both players confirm results, 1000/400 coin prizes, Tournament Champ role |
| Achievements | 21 badges, `/profile`, `/badges`; checked on activity and hourly; Founding Member is granted without a post |
| Creators | `/creator link/verify/unlink/list` (members prove ownership with a code in their channel description), staff `/creator approve/remove`; YouTube uploads and Twitch go-live in 📺・creators |
| Levels | XP from chat (60 s cooldown) and voice; Regular/Veteran/Elite/Legend/Mythic at levels 5/10/20/30/50; `/rank` card, `/levels`, staff `/xp`; level-ups are announced from level 5 (the first reward role) |
| Recap | weekly recap in 📣・announcements Mondays 10:00, owner digest by DM 10:05, member milestones, monthly invite contest (1500/750/300 coins) on the 1st |
| Self roles | button panel in 🎭・roles (platform, region, play time, pings, games), `/roles`; Game Night role pinged when a game night starts |
| Partners | `/partner apply` → staff review card in 📋・mod-log → 🤝・partners; weekly dead-invite sweep; staff `/partner remove` |
| Starter quest | `/quest`: pick roles, say hi, join a squad, claim `/daily`, join voice; the Settled In badge, plus 500 coins for new members (account 30+ days old, finished within 30 days of joining); one nudge DM after a day |
| Heartbeat | writes `bot/data/heartbeat` every minute while connected to the gateway (stops during a reconnect loop, so the watchdog alerts); logs a warning when the event loop stalls for 1 s+ |
| Auto tournaments | first Monday of the month: a 16-player bracket for the most-played game, Discord event, Saturday 19:00 auto-start (4+ entrants), 1 h reminder, nudges for unreported matches; matches still undecided after 48 h (including a report the opponent never confirmed) go to staff in mod-log |
| Weekly challenges | 3 rotating goals each week (150/250/400 coins + 300 bonus), board in 🎲・games Mondays 09:00, `/challenges` with Claim button, hourly auto-claim; squads and voice company only count from accounts 30+ days old, a game night counts once it starts (not if cancelled), a tournament entry once it starts |
| Daily Word | `/word guess/today/stats/leaderboard`: one 5-letter word a day (order salted with a private secret kept in the database), spoiler-free results in 🎲・games, coins for wins, streaks |
| Vibes | conversation starter when 💬・general is quiet for 3 h, join anniversaries, booster thanks + 1000 coins and a monthly stipend, member of the month (squads = joining someone else's post) |
| Matchmaking | `/queue join/leave/status`: pick a game, mode and size; when the queue fills, a voice channel is made and the players are pinged in 🎲・games; entries expire after 60 min |
| Economy extras | `/raffle buy/info` (weekly draw Sundays 20:00, 80% of the pot to the winner, 20% burned), Gift Hype, Spotlight (pinned shoutout for 24 h); staff `/economy` for supply and minted vs burned. Audit: `docs/ECONOMY.md` |
| Help | `/help` (categories built from the live command list, staff commands hidden from members, "Start here" button), `/about`; the bot's status rotates every 5 min |
| Game news | official Steam announcements and verified feeds (`bot/assets/news_sources.json`) posted in each game's channel every 30 min, no pings; staff `/news test` |
| Staff applications | `/apply staff` (30+ days in the server, 90+ day account, clean record), `/apply status`; review card in 📋・mod-log with Approve/Deny (Keepers) and Interview; the Moderator role is then given by hand, since it sits above Front Desk |
| Text filter | slurs (with evasion spellings) and other servers' invite links are refused in member text the bot reposts: `/lfg` and `/gamenight` notes, shoutouts and Spotlight, tournament names, partner applications |

Landing page: <https://gr8monk3ys.github.io/discord-server/> (`site/`, deployed by
`.github/workflows/pages.yml`). Listing-site copy and banners: `docs/LISTING.md`, `assets/listing/`.

All times are Pacific. Every scheduled job catches up once after downtime and never runs twice.

### How it runs

- **Task Scheduler task "Front Desk bot"** runs `server\.venv\Scripts\pythonw.exe main.py` in
  `bot\`, at boot (no sign-in needed) and at logon, and restarts it if it crashes. A lock file
  (`bot\data\bot.lock`) stops a second copy.
- **Logs:** `bot\data\bot.log` (rotated). **Database:** `bot\data\front_desk.db`.
  **Backups:** `D:\Backups\front-desk\`, 14 days kept, integrity-checked.
- **Health:** errors and "back online" notes are posted in 📋・mod-log; `/status` (staff only)
  shows uptime, latency, last backup and error counts.
- **Watchdog:** Task Scheduler task "Front Desk watchdog" runs `server\watchdog.py` every 5
  minutes. If the heartbeat is older than 10 minutes it restarts the bot task (at most 3 times an
  hour) and alerts 📋・mod-log through the webhook in `server\.env` (`WATCHDOG_WEBHOOK_URL`,
  `WATCHDOG_PING_USER_ID`). Install or update it with `server\install_watchdog.ps1`.

### Runbook

| Situation | Do this |
|---|---|
| Restart the bot | `Stop-ScheduledTask 'Front Desk bot'; Start-ScheduledTask 'Front Desk bot'` |
| Bot offline | Check `bot\data\bot.log` (last lines), then restart. "already running" means another copy holds `bot.lock` |
| Change the task | Needs an **elevated** PowerShell (the task runs as S4U) |
| Restore the database | Stop the task, copy the newest `D:\Backups\front-desk\front_desk-*.db` over `bot\data\front_desk.db`, start the task |
| A role won't be given out | The role must sit **below Front Desk** in Server Settings → Roles. Only the owner can move roles above the bot |
| Run by hand (debugging) | `python server\watchdog.py --pause 60` first (or it restarts the task), stop the task, then `bot\run_bot.bat`; `--resume` afterwards |
| Watchdog status | `python server\watchdog.py --check` |
| Tests | `cd bot`, then `..\server\.venv\Scripts\python -m pytest -q` (about 2,300 tests) |

Required in the Developer Portal: the **Server Members**, **Message Content** and **Presence**
intents. Required on the Front Desk role: the permissions printed by `main.py --invite`
(includes Manage Roles/Channels/Server/Events, Timeout Members, Manage Messages, Move Members).

### Adding a module

1. Add tables as a **new** entry in `MIGRATIONS` (`bot/db.py`); never edit a shipped one.
2. Pure rules go in `bot/logic/<name>.py` with tests; Discord I/O in `bot/cogs/<name>.py`.
3. Move coins only through `bot/economy.py`. Send member text only with `ping_only(...)` or
   `AllowedMentions.none()`, and escape markdown in bot-authored text.
4. Register it in `MODULES` in `bot/main.py` with the privileged intents it needs.
