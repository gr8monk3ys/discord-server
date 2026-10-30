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
each one gets a ping role plus a channel. `RETIRED_GAMES` under it holds games the
server used to list: their roles stay, they are no longer offered, and
`public_mode.py` archives their channels. The script is safe to re-run: it
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
@Verified covers new members). The lock is off for now (`layout.MEDIA_LOCK = False`,
captcha paused since 2026-10-28), so `polish_server.py` gives `@everyone` media instead;
set it back to `True` when Captcha.bot returns. The bot roles are ordered by the `keep` entries
in `layout.ROLES`, and every bot gets `@Bots` so they're grouped in the member list.

**Afterwards**

- Give yourself **@Keeper** and your friends **@Squad**.
- Take Administrator back off the bot's role. Front Desk now stays as the server's
  permanent bot (section 3) with only the permissions it needs.
- For later changes use `public_mode.py`: it never deletes or reorders roles, so it works with
  Front Desk sitting below the staff roles. It adds what is missing, with one exception: a
  game in `RETIRED_GAMES` leaves the "What do you play?" question and its channel moves into
  the staff-only `99 · archive` category (messages and the role are kept; the dry run lists
  every channel it would hide).
- To give newcomers a short channel list, run `focus_mode.py` (`--plan` prints the plan from
  `layout.py` alone, no arguments is a dry run against the server, then `--apply`). The
  Onboarding default channels become the nine in `ONBOARDING_DEFAULT_CHANNELS`, and the other
  public channels become answers to one optional question, "What else do you want to see?".
  No permission changes and nothing is deleted: every channel stays open to @everyone and
  current members keep the list they have. `--revert` makes every channel a default again.
- Reset the bot token in the portal if you shared `.env` anywhere.
- Optional: set `ICON_PATH` in `layout.py` to a square image for the server icon.

### Emoji & sounds

The server has its own emoji pack (20 emoji, such as `:gg:`, `:take_w:`, `:clutch:`, `:touchgrass:`
and `:frontdesk:`) and 8 soundboard sounds. Code generates all of them from scratch: Pillow
draws the emoji and numpy synthesizes the sounds. No samples or outside art are used. The emoji
share the look of the server icon: a night-paper stamp with one mark in one ink (pen green for
wins and hype, near-white for neutral words, muted for losses), drawn to read at 22 to 48 px on
both Discord themes. `assets/expressions/contact-sheet.png` shows the whole set at those sizes.
The lettering is set in Bahnschrift Bold, so `make_emoji.py` needs Windows 10 or 11; the font
itself is not in this repo.

```
.venv\Scripts\pip install pillow numpy                     # ffmpeg must be on PATH for the MP3s
.venv\Scripts\python ..\assets\expressions\make_emoji.py   # -> assets/expressions/emoji/*.png
.venv\Scripts\python ..\assets\expressions\make_sounds.py  # -> assets/expressions/sounds/*.mp3
.venv\Scripts\python expressions.py                        # dry run: what fits in your slots
.venv\Scripts\python expressions.py --apply                # upload
.venv\Scripts\python expressions.py --replace              # dry run: which emoji have new art
.venv\Scripts\python expressions.py --replace --apply      # swap them, one at a time
```

The bot's role needs **Create Expressions**. Names that already exist are skipped, and anything
over the emoji or soundboard slot limit (8 sounds unboosted) is listed rather than uploaded.
Sounds are MP3 because discord.py only accepts MP3 for the soundboard.

`--replace` is for emoji that were redrawn. Discord cannot change an emoji's picture, so each
emoji whose local art differs from the server's is uploaded again under the same name and the
old one is deleted (upload first when a slot is free; `--delete-first` turns that round). The
emoji gets a new ID: typing `:name:` shows the new art, but messages and reactions that already
used the old one do not change. It needs **Manage Expressions** as well, unless the bot uploaded
every emoji it deletes. A touch-up too small for the comparison to notice can be named with
`--force NAME`. Nothing in the bot refers to these emoji, by name or by ID.

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
| Stats | `/stats`, `/leaderboard`, `/privacy`; weekly MVP Sundays 18:00 (accounts 30+ days old; fresh accounts don't count as voice company for it; if 💬・general is missing at that time the week is retried, not skipped) |
| Growth | `/invites`, `/recruiters` (Recruiter role), `/bumpers`, `/bumpping`; Disboard bump reminders |
| Community | welcome after Onboarding, `/report` and "Report message", mod log |
| Hall of fame | 3 ⭐ from other members (accounts 30+ days old) reposts to ⭐・hall-of-fame (public channels only) |
| Clips | clip of the week poll Sundays 18:05 (retried, not skipped, while 📸・clips is missing); winner gets Clip of the Week (only votes from accounts 30+ days old count, and not the clip author's own) |
| Voice | join ➕ New Squad for your own channel; `/squad name`, `/squad limit`, `/squad claim` |
| Events | `/gamenight`; `/watchparty` (a Stage event in 🎙・stage, or a voice event in the Lobby when the bot can't schedule on the stage; accounts 30+ days old; shares the game-night limits; its reminder pings the Game Night role only when staff host it); a Town Hall on the first Saturday of each month at 12:00 (`TOWNHALL_TIME`), created a week ahead and announced once; 15-min reminders; free Epic/Steam games Thursdays 18:00 |
| Economy | `/daily`, `/balance`, `/give` (accounts 30+ days old), `/coinflip`, `/richest`; coins for voice, chat, clips, squads |
| Games | `/slots`, `/blackjack`, `/trivia`, `/predict` |
| Shop and seasons | `/shop`, `/buy` (colour role, Hype, shoutout), `/season`; monthly champions |
| Engagement | question of the day 12:00 and this-or-that poll 18:00 (each pauses while nobody answers it: see Quiet rooms), 🔢・counting, auto game night Fridays, `/birthday` |
| Moderation | `/warn`, `/timeout`, `/untimeout`, `/cases`, `/purge` (shown only to members with Timeout Members, like `/xp` and `/suggestion`); `/ban` (staff who also hold Ban Members; DMs the reason first, plus the appeal code when appeals are set up; one line in 📋・mod-log per ban); auto-escalation, anti-spam, anti-raid |
| Operations | daily DB backup 04:00, error alerts, back-online note, weekly config drift check, `/status`; permission self-check a minute after start and daily at 09:00 (every channel, permission and role the loaded modules need: one mod-log note when something is missing, one when it is fixed, and a line in `/status`) |
| Utility | `/remind`, `/reminders`, `/afk`, suggestion voting, member/online stat channels, tickets in 🆘・help |
| Welcome cards | an image card in 👋・welcome when someone finishes Onboarding (names them without a ping; the #general welcome is the one ping); a card that fails to post is retried every 10 minutes for up to 24 hours while the member is still here |
| Tournaments | `/tournament create/start/cancel/bracket`; sign-up buttons, brackets with byes, both players confirm results, 1000/400 coin prizes, Tournament Champ role |
| Achievements | 23 badges, `/profile`, `/badges`; checked on activity and hourly; Founding Member is granted without a post |
| Creators | `/creator link/verify/unlink/list` (members prove ownership with a code in their channel description), staff `/creator approve/remove`; YouTube uploads and Twitch go-live in 📺・creators |
| Levels | XP from chat (60 s cooldown) and voice; Regular/Veteran/Elite/Legend/Mythic at levels 5/10/20/30/50; `/rank` card, `/levels`, staff `/xp`; level-ups are announced from level 5 (the first reward role) |
| Recap | weekly recap in 📣・announcements Mondays 10:00, owner digest by DM 10:05, member milestones, monthly invite contest (1500/750/300 coins) on the 1st. The digest has a "Games list" field that compares the last 28 days of play with `GAMES`: worth adding (an unlisted game that 3+ members played for 5+ hours combined, a retired one included) and worth retiring (a listed game nobody played); game names, player counts and hours only, and it never changes the list. Its "Newcomers" field says whether the people who join stick: for the week and for the last 28 days, how many joined, left since, picked roles, said something and joined voice; of the week before's joiners, how many are still here and how many came back in week 2; and one sentence of reading. Counts only, never a name. Members with tracking off count as joined and left and in nothing else; the other steps are "of the N with stats on", so the field shows how many recent joiners have tracking off. No field when nobody joined in 28 days |
| Self roles | button panel in 🎭・roles (platform, region, play time, pings, games), `/roles`; Game Night role pinged when a game night starts, SMP Ping when 3+ are on Monkey SMP, Welcomer when someone new joins voice |
| Partners | `/partner apply` → staff review card in 📋・mod-log → 🤝・partners; weekly dead-invite sweep; staff `/partner remove` |
| Starter quest | `/quest`: pick roles, say hi, join a squad, claim `/daily`, join voice; the Settled In badge, plus 500 coins for new members (account 30+ days old, finished within 30 days of joining); one nudge DM after a day |
| Heartbeat | writes `bot/data/heartbeat` every minute while connected to the gateway (stops during a reconnect loop, so the watchdog alerts); logs a warning when the event loop stalls for 1 s+ |
| Auto tournaments | first Monday of the month: a 16-player bracket for the most-played game (if 🏆・tournaments is missing it waits for the channel, until it is too late to open sign-ups), Discord event, Saturday 19:00 auto-start (4+ entrants), 1 h reminder, nudges for unreported matches; matches still undecided after 48 h (including a report the opponent never confirmed) go to staff in mod-log |
| Weekly challenges | 3 rotating goals each week (150/250/400 coins + 300 bonus), board in 🎲・games Mondays 09:00, `/challenges` with Claim button, hourly auto-claim; squads and voice company only count from accounts 30+ days old, a game night counts once it starts (not if cancelled), a tournament entry once it starts |
| Daily Word | `/word guess/today/stats/leaderboard`: one 5-letter word a day (order salted with a private secret kept in the database), spoiler-free results in 🎲・games, coins for wins, streaks |
| Vibes | conversation starter when 💬・general is quiet for 3 h (not within an hour of the Tonight voice line), join anniversaries, booster thanks + 1000 coins and a monthly stipend, member of the month (squads = joining someone else's post) |
| Matchmaking | `/queue join/leave/status`: pick a game, mode and size; when the queue fills, a voice channel is made and the players are pinged in 🎲・games; entries expire after 60 min |
| Economy extras | `/raffle buy/info` (weekly draw Sundays 20:00, 80% of the pot to the winner, 20% burned), Gift Hype, Spotlight (pinned shoutout for 24 h); staff `/economy` for supply and minted vs burned. Audit: `docs/ECONOMY.md` |
| Help | `/help` (categories built from the live command list, staff commands hidden from members, "Start here" button), `/about`; the bot's status rotates every 5 min |
| Game news | official Steam announcements and verified feeds (`bot/assets/news_sources.json`) posted in each game's channel every 30 min, no pings; staff `/news test` |
| Staff applications | `/apply staff` (30+ days in the server, 90+ day account, clean record), `/apply status`; review card in 📋・mod-log with Approve/Deny (Keepers) and Interview; the Moderator role is then given by hand, since it sits above Front Desk |
| Text filter | slurs (with evasion spellings) and other servers' invite links are refused in member text the bot reposts: `/lfg` and `/gamenight` notes, shoutouts and Spotlight, tournament names, partner applications |
| Seasonal events | Halloween (Oct 20 to Nov 1) and Winter Fest (Dec 15 to Jan 2) from `bot/assets/events.json`, every year: 6-10 drops a day in 💬・general while chat is active (Grab button, 25-75 coins + 1 point, gone after 10 min, max 12 a day each), a daily quest (+3 points, 100 coins), `/event` with your points and the top 10; at the end the top 3 win 2000/1000/500 coins, first place gets the Event Champ role and 10+ points earns the event badge; accounts under 30 days old earn nothing |
| Game accounts | `/link steam` + `/link verify` (members prove ownership with a code in their Steam profile summary), `/unlink`, `/accounts`; staff `/link approve/remove`; persona, Steam link and hours in the server's games show on `/profile`, refreshed daily. No API key |
| Vote hub | `/vote` (private): Disboard with your bump count and when the next bump is ready, discord.me, top.gg ("coming soon" until listed) and the share link discord.lscaturchio.xyz/join; Sundays 17:00 the week's top 3 Disboard bumpers are thanked in 🎲・games and paid 300/200/100 coins (accounts 30+ days old) |
| Catch-up | `/catchup [channel] [hours]`: a private AI summary (5-8 bullets plus a "good moment to jump in") of a public channel's last 1-48 hours (default 12). Two providers: a local Ollama model on the bot's own machine (`OLLAMA_MODEL`, default `llama3.2:3b`; nothing leaves the machine, one summary at a time) or the Anthropic API (`ANTHROPIC_API_KEY`); `CATCHUP_PROVIDER` picks one, otherwise the key wins and Ollama is the fallback, and with neither it says summaries aren't set up. Accounts 30+ days old, 3 a day per member and 60 a day server-wide; bots and members with tracking off are left out; staff `/catchup-usage` shows the provider, requests and tokens |
| Houses | three houses (Fern, Kestrel, Otter): established members are sorted into the smallest one every 10 min and get its role (one introduction post in 📣・announcements the first time; the permission self-check lists the house roles until `public_mode.py --apply` has made them); `/house me/standings/join/leave/history` (leaving starts a 30-day wait), staff `/house-admin move`; points come from what the other modules already record, capped at 100 per member a week and scored per member of the house; House Cup Mondays 10:10 in 📣・announcements (100 coins to each member of the winning house with 10+ points, once 2 qualify); a season trophy each calendar quarter |
| Welcome Crew | `/crew` with Join/Leave buttons (accounts 30+ days old, 14 days here, no moderation case in 30 days; a new case takes the role back); one crew ping in 💬・general per 30 min for first-time joins (`CREW_PING_MINUTES`; someone who left and came back is not announced again), never 22:00 to 09:00 (`CREW_QUIET_HOURS`), during a raid or with no crew member online; 40 coins when a newcomer answers you (once per newcomer, 3 paid a day), the Welcoming Committee badge at 5 greetings; staff `/crew-stats` and a line in the owner digest |
| Supporter tier | cosmetic perks for whoever holds the Supporter role (given by Ko-fi's Discord integration, Server Subscriptions or staff `/supporter grant/revoke`; no payment code in the bot): a thank-you in 💬・general, a flair on `/profile`, the 💜・supporters lounge, 250 coins on the 1st at 12:15 (accounts 30+ days old); `/support` shows the perks and the `SUPPORT_URL` link |
| Forums | 📚・guides and 🖥・setups: the bot adds the first reaction to every post; a post still up after a day that clears a minimum bar (guides: 200 characters and 25 different words; setups: a picture, or 150 characters and 20 words) earns 30 coins, 2 paid posts per member a week, accounts 30+ days old; locked or deleted posts earn nothing; a "fresh in the forums" line in the weekly recap |
| Clipcast | the week's top 3 clips (one per author, Discord-hosted video files only) go to the server's YouTube channel, only with the author's yes for that clip: asked by DM Sundays 18:30 (credit by name, anonymous, or no), posted Tuesdays 18:30; `/clipcast consent always/ask/never` (switching to ask reopens clips the standing yes approved and that are not posted yet), `/clipcast remove` (takes a posted clip down); the On the Channel badge when posted, and 100 coins once the clip has stayed up 7 days (a clip taken down before then never pays); without the `YOUTUBE_*` settings the approved clips are listed in 📋・mod-log to post by hand; a week whose ask, post or hand-over can't happen (the server, 📸・clips or the mod log is missing) is retried every 5 min until the next week is due; staff `/clipcast status/skip` |
| Monkey SMP | Minecraft bridge in ⛏️・monkey-smp, each part on only when its settings are in `server/.env`: a status panel edited in place (polled every 2 min), `/smp link/unlink` (records which Minecraft name is yours, for the chat bridge and playtime only: accounts 30+ days old, one name per member; unlinking, leaving or a ban just forgets the link. A link gives no access: Monkey SMP is a paid pass staff grant by hand or through the store at https://monkeysmp.lscaturchio.xyz, and the bot never sends `whitelist`; over RCON it sends only `list` and `tellraw`, plus, only with `REFERRAL_GRANT=rcon`, the three referral scoreboard commands), chat both ways (no pings; in-game chat comes through only from players linked to a member who can post in the channel right now, join and leave lines for everyone), 5 coins per half hour online while at least two players are on, up to 40 a day; `/smp status`, `/smp top`, mods `/smp remove` and `/smp adopt` (link a name to a member without the account-age bar); the bridge's health shows in `/status`. Nudges (off unless `SMP_NUDGES=on`): `/smp nudges on` (linked members, tracking on) gets one DM after 7 days off the server, saying what happened since (other members' playtime as counts, newly approved factions, Base of the Month, upcoming SMP events) and how to get back in; one per lapse, a refused DM stops further tries, last-seen comes from the 2-minute player-list poll and is kept only for members who opted in |
| Wrapped | `/wrapped [year]`, December 1 to January 15: a private year-in-review card with a Share button (only in a channel you can post in yourself; sharing gives the Wrapped badge); "Wrapped is open" with the server-wide card December 1 at 10:00 and a reminder December 20; nothing for members with tracking off; staff `/wrapped-preview` |
| Live site data | every 30 min the bot writes `bot/data/site/stats.json`: server-wide aggregates only (no names or IDs, nothing about fewer than 3 members, rounded numbers; house standings only when every house has 3+ members who scored that week; event titles are only the bot's own templates, never a tournament name or a hand-made title); `server/publish_site_data.py` pushes it to the `site-data` branch and the landing page shows it; staff `/sitedata refresh`; `SITE_DATA=off` stops it |
| Ban appeals | a banned member appeals at discord.lscaturchio.xyz/appeal with the code from their ban DM; a review card appears in 🔒・appeals with Accept (unbans) and Deny buttons for staff with Ban Members, plus a discussion thread; read 7 days after the ban at the earliest, 90 days between a denial and the next appeal, open appeals close after 30 days (checked daily; a day when the server is unavailable is retried, not skipped); staff `/appeals list`, `/appeals code`; off until `APPEAL_SECRET` is set |
| Linked Roles | Discord's own Linked Roles for verified Steam, Xbox, PlayStation and Epic Games connections, served by the site's route handlers (`site/src/app/api/linked-role/`) on Vercel; the bot is not involved and nothing is stored. Setup: `docs/LINKED_ROLES.md` |
| Tonight | one line in 💬・general while 2+ people are in a public voice channel: how many, the game most of them share if there is one, and the channel as a mention; never names, no pings. Posted once a room has held for 3 min, edited in place at most every 2 min, and turned into a past-tense line (how long, how many at most) when the rooms empty; it is kept as a record, never deleted. At most 3 new lines a day; a new session reuses the previous line when nobody has written since; not within an hour of the Vibes conversation starter. `TONIGHT=off` turns it off |
| SMP watch | one line in ⛏️・monkey-smp when 3 or more players are on Monkey SMP, mentioning the opt-in SMP Ping role (🎭・roles, Pings): the count and the join address, never names. It checks every 5 min with a Minecraft status ping of the public address, found through its SRV record (no RCON, no secrets). After a ping, none until the count has dropped below 3 again and 4 hours have passed, and none from 22:00 to 09:00 local. `SMP_WATCH=off` turns it off; `SMP_WATCH_ADDRESS` and `SMP_WATCH_MIN` (2 to 50) change the address and the count |
| Welcomers | one line in 💬・general when someone who joined in the last 14 days joins a public voice channel for the first time (by the voice history stats keeps) and stays 30 s: it pings the opt-in Welcomer role (🎭・roles, Pings), mentions the newcomer without pinging them and names the channel. Once per newcomer; not when a Welcomer or staff member is already in that channel or nobody else holds the role; at most one ping per 15 min; never in the crew's quiet hours (`CREW_QUIET_HOURS`, default 22:00 to 09:00); members with tracking off are skipped |
| Factions | `/faction register` opens a form (name, members' Minecraft Java names, banner, charter) and makes one post in 🚩・smp-forum tagged Faction, with Approve / Reject buttons for staff. The SMP's SEASONS.md rules: up to 4 players, the charter is one paragraph, one registration per leader, a Minecraft name on one faction per season, and the name of last season's champion (`FACTIONS_RESERVED`, comma-separated) or one a letter or two away is refused. `/faction charter` rewrites the charter in place. `FACTIONS_FORUM` names another forum; `FACTIONS_SEASON` (default 1) starts a new season's registrations. Recruitment board (off unless `RECRUIT_FORUM` names a forum): the leader of an approved faction runs `/faction pitch` (pitch, up to 4 playstyle tags, timezone) for one post on the board and `/faction recruit on|off` to open or close it (closed when the roster is full; tagged Recruiting when the forum has that tag). The post's Request to join button DMs the leader who's asking (their linked Minecraft name, once a day per faction, not for players already on a roster), or pings the leader in the post when DMs are closed. Solo players list themselves with `/faction lfg on|off` (a note and timezone, 14 days); leaders of approved factions and staff browse them with `/faction seekers`. Leaving or a ban takes a member off the list |
| Tebex alerts | off unless `TEBEX_WEBHOOK_SECRET` is set: a small HTTP receiver (`TEBEX_WEBHOOK_HOST`, default 127.0.0.1; `TEBEX_WEBHOOK_PORT`, default 8790; path `/tebex/webhook`) for Tebex's `recurring-payment.cancellation.requested`. Each signed webhook posts the player, the package and the end date in 🛡️・mod once, so staff DM them while the pass still runs. Unsigned requests get 401. Setup: `docs/TEBEX_WEBHOOK.md` |
| Referrals | off unless `REFERRALS=on` (needs the Tebex receiver): `/referral` gives a linked member a personal code. When a new buyer's Tebex purchase carries it (a checkout custom variable, or Tebex creator codes with `REFERRAL_CREATOR_CODES=on`), both players get the Kindred Band wardrobe helm and the code's owner one more "friend invited" (`rw.invites`). No money or discount from the bot. A buyer counts once ever, a transaction once, never your own name. The three scoreboard commands are posted in 🛡️・mod for staff (`REFERRAL_GRANT=staff`, default) or sent over the bridge (`REFERRAL_GRANT=rcon`); refunds and chargebacks post the undo commands. `REFERRAL_VARIABLE`, `REFERRAL_PACKAGES` optional. Setup: `docs/TEBEX_WEBHOOK.md`, "Referrals" |
| Base of the Month | `/botm award` (staff): up to 4 builders, an optional line from them and up to 3 screenshots. Moves the Base of the Month role from last month's holders to them (makes the role, at the bottom, if it is missing; never moves roles) and announces it in ⛏️・monkey-smp. The same builders can't win two months running |
| Quiet rooms | the question of the day and the this-or-that poll stop when the last 3 of their kind went unanswered (no vote, reaction, thread message or reply; anything the bot can't read never counts against a prompt). A skipped day is marked done and no question or pair is used up. A member message in 💬・general lets the next one through, and one goes out anyway after 7 quiet days. One line in 📋・mod-log when a prompt pauses and one when it resumes, and a "Paused prompts" field in `/status`. The Vibes conversation starter goes through the same gate, but its own rule already needs a member message, so the gate never refuses it. `QUIET=off` posts every day again |
| Field notes | one post a week in 📓・field-notes, Sundays at 18:00: what the server owner built (pull requests merged in their public GitHub repos), heard (top artists and repeats from SpotifyForge's listening log), watched (Letterboxd diary entries, with stars) and played (their own game time), about 4 lines each, then one question; each post opens a thread for replies. Only the owner's own activity, never a member's; no pings. A section is left out when its source is not set up, fails or had nothing that week, and a week with nothing posts nothing. Staff `/fieldnotes preview` shows the week so far privately. Off until a `FIELD_NOTES_*` setting is in `server/.env` |

Turning the newest features on (permissions, secrets, outside accounts): `docs/WAVE9_SETUP.md`.

Short join link: <https://discord.lscaturchio.xyz/join>. Moving the bot off this PC: `docs/HOSTING.md`
(Docker image, `docker-compose.yml`, `server/migrate_host.py`).

Landing page: <https://discord.lscaturchio.xyz/> (`site/`, a Next.js app served by Vercel from `main`; see
`site/README.md`). Listing-site copy and banners: `docs/LISTING.md`, `assets/listing/`.

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
- **Live site data:** the bot only writes `bot\data\site\stats.json`. A scheduled task you create
  runs `server\.venv\Scripts\pythonw.exe publish_site_data.py --apply --quiet` in `server\` every
  30 minutes and pushes the file to the `site-data` branch (`docs/WAVE9_SETUP.md`).

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
| Tests | `cd bot`, then `..\server\.venv\Scripts\python -m pytest -q` (about 4,200 tests); `cd server`, then `.venv\Scripts\python -m pytest -q tests` (275); `cd site`, then `bun install --frozen-lockfile`, `bun run lint`, `bun run typecheck`, `bun run test` and `bun run build` (the Next.js site; `bun run test:e2e` for Playwright). CI runs all three |
| New roles or channels in `layout.py` | `cd server`, then `.venv\Scripts\python public_mode.py` (dry run) and `--apply`. It adds what is missing and never deletes; the one thing it takes away is a retired game's channel, which it moves to the staff-only archive (next row). The permission self-check lists what is still missing in 📋・mod-log |
| Add or retire a game | In `server\layout.py`, move the game's tuple line between `GAMES` and `RETIRED_GAMES` (a brand-new game is a new line in `GAMES`). Then `cd bot` and `..\server\.venv\Scripts\python -m logic.sitegen` (the site's games block), `cd server` and `.venv\Scripts\python public_mode.py` (dry run: read what it would hide) then `--apply`, and restart the bot. Retiring keeps the role and every member's copy of it and moves the channel, messages included, into `99 · archive`; moving the line back and re-running undoes it |
| Monkey SMP channels (the `04 · monkey smp` category: 📜・smp-rules, ❔・smp-faq, ⌨️・smp-commands, ⛏️・monkey-smp, 📝・founding-roster, 🚩・smp-forum) | `cd server`, then `.venv\Scripts\python public_mode.py` (dry run: under "Categories" it makes `04 · monkey smp` after `03 · games` with that category's permissions and renumbers the ones after it; under "Channels in another category" it moves the SMP channels in from `03 · games`, permissions kept; and "Minecraft also opens ..." under "Game answers' channels") and `--apply`. Restart Front Desk first: it finds the voice category as `06 · voice` or its old name `05 · voice`, where a bot from before this finds only `05 · voice`. Then `focus_mode.py` (dry run: the "Minecraft" group with all six SMP channels) and `focus_mode.py --apply`, which needs the channels to exist first. The same `public_mode.py` run posts and pins the roster rules, the condensed rulebook, the FAQ and the commands (`layout.POSTS`; the dry run lists them under "Posts") and later edits them in place. A #founding-roster made by hand before this is never edited: the dry run says if its slow mode or thread permissions differ |
| Shorten or restore the newcomer channel list | `cd server`, then `.venv\Scripts\python focus_mode.py --plan` (the plan from `layout.py`), `focus_mode.py` (dry run against the server) and `focus_mode.py --apply`; `--revert` (dry run) and `--revert --apply` make every channel a default again. Afterwards run `snapshot_server.py` and commit `server/snapshot/onboarding.json` |
| Turn on a new feature | `docs/WAVE9_SETUP.md`: one checklist, cheapest steps first. Secrets go in `server\.env` (bot; restart it afterwards) or the Vercel project (site functions; redeploy afterwards) |
| Landing page live section is gone | It hides itself when the published data is 48 h old. Run `python server\publish_site_data.py` (dry run) to see why nothing is pushed; `/sitedata refresh` rebuilds the file |
| Someone was banned without an appeal code | `/appeals code` shows it so you can pass it on. `/ban` sends it by DM before the ban; a ban from Discord's own menu usually can't |

Required in the Developer Portal: the **Server Members**, **Message Content** and **Presence**
intents. Required on the Front Desk role: the permissions printed by `main.py --invite`
(includes Manage Roles/Channels/Server/Events, Create Events, Timeout Members, Manage Messages, Pin Messages,
Move Members, Mute Members, Ban Members; Create Events is separate from Manage Events, and without it game
nights and tournament events fail with 403. Pin Messages is separate from Manage Messages in the same way:
without it the Spotlight and the Monkey SMP panel cannot be pinned. Mute Members is for Stage events, Ban Members
for `/ban` and ban appeals).

### Adding a module

1. Add tables as a **new** entry in `MIGRATIONS` (`bot/db.py`); never edit a shipped one.
2. Pure rules go in `bot/logic/<name>.py` with tests; Discord I/O in `bot/cogs/<name>.py`.
3. Move coins only through `bot/economy.py`. Send member text only with `ping_only(...)` or
   `AllowedMentions.none()`, and escape markdown in bot-authored text.
4. Register it in `MODULES` in `bot/main.py` with the privileged intents it needs.
