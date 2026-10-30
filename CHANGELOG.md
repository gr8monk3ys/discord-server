# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Monkey SMP retention tools (migration 25 adds `factions` and `tebex_webhooks`):
  - `/faction register` and `/faction charter`: the faction sign-up form from the SMP's SEASONS.md,
    one post per faction in #smp-forum tagged Faction, approved or rejected by staff with buttons.
    `FACTIONS_FORUM`, `FACTIONS_RESERVED` and `FACTIONS_SEASON` are optional.
  - Tebex "pass ending" alerts: a signed-webhook receiver for
    `recurring-payment.cancellation.requested` that posts in #mod. Off unless
    `TEBEX_WEBHOOK_SECRET` is set (`docs/TEBEX_WEBHOOK.md`).
  - `/botm award` (staff): moves the Base of the Month role to this month's builders and announces it
    in #monkey-smp; makes the role when it is missing and never moves roles.

### Changed

- The Minecraft bridge drops the server's old name, The Realms: the module is `monkey`
  (`cogs/monkey.py`, `logic/monkey.py`, cog `MonkeySmp`), its settings are `MONKEY_ADDRESS`,
  `MONKEY_RCON_HOST`, `MONKEY_RCON_PORT`, `MONKEY_RCON_PASSWORD` and `MONKEY_LOG_PATH` (the old
  `REALMS_*` names still work when a new one is unset), and migration 24 renames its tables to
  `monkey_links` and `monkey_playtime` and the status panel's meta key to `monkey:panel`, so links,
  playtime and the pinned panel carry over. The playtime ledger reason and refs keep `realms`, so
  past payouts still count and nothing is paid twice. The `/smp` commands are unchanged.
- Monkey SMP has its own category, `04 · monkey smp`, right after `03 · games`: #smp-rules,
  #smp-faq, a new read-only #smp-commands (a pinned list of the player commands, in game and
  `/smp`), #monkey-smp, #founding-roster and #smp-forum. The categories after it are renumbered
  (`05 · off topic`, `06 · voice`, `07 · squad`). `public_mode.py` renames categories by their
  "was" names, makes the new category with the games category's permissions, and moves the SMP
  channels in, keeping their permissions. Temp voice and matchmaking find the voice category
  under its old name too, so restart Front Desk before applying. Picking Minecraft in
  Onboarding also opens #smp-commands.
- The landing page is a Next.js app on the lscaturchio.xyz stack (Next.js 16, App Router,
  TypeScript, Tailwind v4, next-themes, bun, Vitest, Playwright), at the owner's request of
  2026-10-29; it was static HTML. Same look, copy, behaviour and URLs. The appeal form and Linked
  Roles functions are route handlers with Vitest tests, the CSP is per-request nonce based, fonts
  are served first-party by next/font, the games and rules are generated JSON, and CI builds and
  tests the site with bun. The GitHub Pages mirror is retired (`pages.yml` removed).
- Monkey SMP: `/smp link` only records the Discord-to-Minecraft name pair, for the chat bridge and
  playtime. Front Desk no longer whitelists or un-whitelists anyone (access is a paid pass staff
  grant by hand or through the store), and its RCON client refuses every command but `list` and
  `tellraw`. The 14-days-in-the-server wait is gone; the 30-day account age stays. `/smp unlink`,
  leaving and a ban just forget the link. `/smp adopt` stays as a staff link. Link rows still
  waiting on the old whitelist sync are settled on load without sending anything. Replies, the
  status panel, the channel topic, the welcome copy and the site say a link is not access and
  point to the pass and a ticket in #help.

### Added

- CI: GitHub Actions runs ruff and both test suites on every pull request and push to `main`.
- `/catchup` can use a local Ollama model, so summaries need no API key and stay on the bot's machine.
- `docs/ARCHITECTURE.md`, `SECURITY.md`, `CONTRIBUTING.md`, issue and pull request templates.
- `ruff.toml`, `pyrightconfig.json`, `requirements-dev.txt`.
- Houses: three houses (Fern, Kestrel, Otter) that members are sorted into, `/house`, a weekly
  House Cup scored from what the other modules already record, and a trophy each calendar
  quarter; staff `/house-admin move`.
- Welcome Crew (`/crew`): an opt-in role with a bar to join, one batched crew ping for new
  arrivals, and a coin reward when the newcomer answers; staff `/crew-stats`.
- Supporter tier: cosmetic perks for holders of the Supporter role (thank-you, `/profile`
  flair, a private lounge, a small monthly stipend), `/support`, staff `/supporter grant/revoke`.
  The role comes from Ko-fi's Discord integration or Server Subscriptions; the bot has no
  payment code.
- Guides and setups forums with a first reaction from the bot and a small one-time reward for
  posts that stay up a day; a Stage channel; `/watchparty`; a monthly Town Hall.
- Clipcast: the week's top clips uploaded to the server's YouTube channel, only with each
  author's consent for that clip (`/clipcast consent`, `/clipcast remove`); staff
  `/clipcast status/skip`. Without YouTube credentials the approved clips are listed for staff.
- The Realms: a bridge to the Minecraft server with a status panel, `/realms link` (whitelist
  over RCON), two-way chat and playtime coins; mods `/realms remove` and `/realms adopt`.
- Wrapped: `/wrapped` year-in-review cards from December 1 to January 15, a server-wide card
  and staff `/wrapped-preview`.
- Live landing page: the bot writes server-wide aggregates (`/sitedata refresh`),
  `server/publish_site_data.py` pushes them to the `site-data` branch, and the page shows
  upcoming events, house standings, top games and weekly totals.
- Ban appeals: `/ban`, an appeal form on the site (`site/appeal/`, `site/api/appeal.js`),
  review cards with Accept and Deny in a staff channel, `/appeals list` and `/appeals code`.
- Linked Roles for verified Steam, Xbox, PlayStation and Epic Games connections
  (`site/api/linked-role/`, `server/register_linked_roles.py`, `docs/LINKED_ROLES.md`).
- Two badges, Welcoming Committee and On the Channel (23 in the catalogue), and a per-year
  Wrapped badge outside it.
- `docs/WAVE9_SETUP.md`: the owner checklist for turning these on.
- `RETIRED_GAMES` in `server/layout.py`: games the server used to list. Retiring a game is
  moving its line there from `GAMES`, and moving it back lists it again. `public_mode.py`
  gained the steps that follow the two lists (game roles and channels, forum tags, the "What
  do you play?" options, archive and restore).
- A "Games list" field in the Monday owner digest (`bot/logic/gamelist.py`): games worth
  adding and worth retiring, from the last 28 days of play. Game names, player counts and
  hours only, never a game fewer than three members played, and it never edits the list.
- `server/focus_mode.py` (`--plan`, dry run by default, `--apply`, `--revert`): cuts the
  Onboarding default channels to a core of nine and puts the other public channels behind one
  optional question, "What else do you want to see?". No permission changes, nothing deleted.
- `tonight` module: one line in general while two or more people are in a public voice
  channel (counts, the shared game and a channel mention, never names), edited in place and
  kept as a record afterwards. `TONIGHT=off` turns it off.
- `smpwatch` module: one line in the Monkey SMP channel when 3 or more players are on,
  mentioning a new opt-in SMP Ping role (on the 🎭・roles panel; `server/public_mode.py --apply`
  creates it). A status ping of the public address every 5 minutes; after a ping, nothing until
  the count has dropped and 4 hours have passed, and nothing from 22:00 to 09:00.
  `SMP_WATCH=off` turns it off.
- `welcomers` module: when someone who joined in the last 14 days joins a public voice channel
  for the first time, one line in general pings a new opt-in Welcomer role (on the 🎭・roles
  panel, Pings; `server/public_mode.py --apply` creates it) and names the channel. Once per
  newcomer, after 30 seconds in the channel, not when a Welcomer or staff member is already
  there, at most one ping per 15 minutes, never in the crew's quiet hours (`CREW_QUIET_HOURS`)
  and never for members with tracking off. "First time" comes from the voice history the stats
  module already keeps.
- A "Newcomers" field in the Monday owner digest (`bot/logic/funnel.py`): for the week and the
  last 28 days, how many joined, left since, picked roles, said something and joined voice; of
  the week before's joiners, how many are still here and how many came back in week 2; and one
  sentence of reading. Counts only. Members with tracking off count as joined and left and in
  nothing else, and the field shows how many recent joiners have tracking off.
- Quiet rooms (`bot/logic/quiet.py`, `bot/quietgate.py`): the question of the day and the
  daily this-or-that poll stop when the last 3 of their kind went unanswered (no vote,
  reaction, thread message or reply). The skipped day is marked done and no question is used
  up. A member message in general lets the next one through, and one goes out anyway after 7
  quiet days. One mod-log line per pause and resume, a "Paused prompts" field in `/status`,
  and `QUIET=off` to post every day again. The chat revival (`vibes`) asks the same gate, but
  its own rule already needs a member message, so the gate never refuses it. State is one
  `meta` value per prompt (`quiet:<kind>`) with no member ids.
- Monkey SMP season channels in `server/layout.py`, after #monkey-smp in `03 · games`: #founding-roster
  (everyone posts, 6 h slow mode, no threads), #smp-rules and #smp-faq (read-only), and the
  #smp-forum forum for factions, recruiting, suggestions, bugs and trading. The "Minecraft" answer
  in "What do you play?" now also opens #monkey-smp, #founding-roster, #smp-forum and #smp-rules
  (`layout.GAME_EXTRA_CHANNELS`; it still gives only @Minecraft, never @SMP Ping), and the
  Minecraft opt-in group lists all five SMP channels.
- Layout: a text channel can set `"no_threads"` (members post, but can't start threads).
  `public_mode.py` creates it with that permission and its slow mode, and only reports (never
  edits) one that was made by hand with other settings. Its new `game_answer_channels` step adds a
  listed game's missing layout channels to that game's existing Onboarding answer, never removing
  a channel or touching its roles.
- `fieldnotes` module: the owner's weekly field notes in a new read-only 📓・field-notes channel
  (`01 · front desk`, a newcomer default): Built (merged pull requests in public GitHub repos),
  Heard (SpotifyForge's listening log), Watched (Letterboxd RSS) and Played (the owner's game
  time), Sundays at 18:00, with a reply thread and one question through quiet rooms
  (`quiet:fieldnotes`). Staff `/fieldnotes preview`. Off until a `FIELD_NOTES_*` setting is set.
- Monkey SMP season posts in `layout.POSTS`: the founding roster's sign-up rules in #founding-roster,
  the condensed Season I rulebook in #smp-rules (rule numbers match the SMP repo's `docs/RULES.md`,
  which is too long for one message, so it links the website's rules) and a short FAQ in #smp-faq.
  `public_mode.py` posts and pins each once and edits it in place when the text changes, as it does
  #rules and #welcome. The posts carry no dates.

### Security

- `/catchup` with the local model budgets the chat log in tokens, so a log of emoji or non-Latin text can no longer overflow the context window and push the system prompt out of it (budgeted at one token per byte, a true upper bound, with the costliest channel name assumed).

### Changed

- Landing page: the rules are generated from the `#rules` embed in `server/layout.py` (`logic.sitegen`,
  with a drift test); a short Monkey SMP section with the Season I date links to its own site; the FAQ
  explains the paid SMP pass, the "Lorenzo's Server" name and what the bot keeps; quieter copy, ink-in
  scroll reveals, a scroll-spy header and a footer Help column (ban appeals, privacy).
- The server is all-ages: the 18+ rule is gone from the rules embed, and the listing copy says so.
- The Minecraft server is now Monkey SMP: `/realms` is `/smp` (`/smp link`, `/smp remove`, `/smp adopt`),
  and the channel is #monkey-smp. The bridge also answers in the old #realms channel until
  `public_mode.py` renames it in place (its new `renamed_channels` step, for any layout channel
  still under a "was" name). Internal names (the cog, `REALMS_*` settings, tables, job keys) are unchanged.
- The emoji pack is redrawn in the Field Notebook style of the server icon: every emoji is a
  night-paper stamp with one mark in pen green, near-white or muted ink, made to read at 22 px
  on both Discord themes (`assets/expressions/contact-sheet.png`). Names are unchanged.
  `expressions.py --replace` (dry run by default) swaps redrawn emoji on the server.
- Requirements are complete and pinned (Pillow and aiohttp were missing); the Docker image installs from them.
- The bot now asks for the Ban Members and Mute Members permissions (`main.py --invite`): Ban
  Members for `/ban` and for lifting a ban when an appeal is accepted, Mute Members for Stage
  events. The owner has to turn both on for the Front Desk role.
- The site's Content-Security-Policy `connect-src` now allows `'self'` (the appeal form posts to
  `/api/appeal`) and `https://raw.githubusercontent.com` (the live data).
- The site sets one short-lived cookie during Linked Roles sign-in (`__Host-fd_linked_role`,
  HttpOnly, 10 minutes, deleted when the member comes back). It had no cookies before.
- The schema is at version 23 (eight new migrations) and the bot loads 47 modules.
- The games list is now Risk of Rain 2, Gears of War, Overwatch, Marvel Rivals, Minecraft,
  Tabletop Simulator, Wardogs and Apex Legends. Fortnite, Valorant, Call of Duty, League of
  Legends, Counter-Strike 2, GTA Online and Roblox are retired: their roles stay (members keep
  them) but are no longer offered, and old squad posts and queue rows that name one still
  show its name.
- `public_mode.py` is no longer purely additive. Its `retire_games` step removes a retired
  game from the "What do you play?" question and moves the game's channel into a staff-only
  `99 · archive` category. It still never deletes anything: messages, roles and forum tags are
  kept, the dry run lists every channel it would hide, and listing the game again moves the
  channel back.
- The chat revival in general (`vibes`) and the new voice line (`tonight`) never both post
  within the same hour.
- The site, `docs/LISTING.md`, the banner images, the server description, the welcome screen
  and the welcome post now describe a small co-op and PvE server that is mostly on in the
  evenings, US Pacific time.
- CI also runs the site function tests (`node --test` in `site/api`).
- `/privacy tracking:off` also deletes a member's Minecraft playtime, forum post records and
  Welcome Crew greetings at once, removes them from the level snapshots and withdraws clipcast
  consent for anything not posted yet. Caps and once-only rewards are counted from the ledger,
  so opting out and back in resets none of them.
- The weekly thanks for Disboard bumpers is now called "Bump thanks" (it was "Supporter
  thanks" before the Supporter tier existed).
- The site footers say "No tracking · No analytics" instead of "No cookies · No trackers",
  since Linked Roles sign-in sets a cookie.
- The permission self-check now also expects Read Message History in general for the
  `engagement` module: quiet rooms re-reads the bot's last prompt and the messages after it.

### Fixed

- Scheduled jobs no longer mark a period done when the server or their channel is missing at
  the scheduled time. The weekly MVP (`stats`), the clip of the week poll (`clips`) and the
  clipcast ask, publish and staff hand-over (`clipcast`) retry on each tick until the next
  period is due. Before, a channel that was missing at that moment lost the week.
- The daily appeal expiry (`appeals`) waits for the server instead of marking the day done,
  and the channel sweep and the expiry now fail independently: a sweep that keeps raising no
  longer stops the expiry from running.
- The monthly auto tournament (`tournaments`) waits for a missing tournaments channel instead
  of skipping the month, until it is too late to open sign-ups.
- The server description comes from the REST API. The gateway's copy of the server never
  carries it, so `/about` showed no description, `public_mode.py` rewrote it on every run
  and `server/snapshot/server.json` recorded it as `null`.
- The permission self-check finds a channel under the old names its cog also uses, so it no
  longer reports the Monkey SMP bridge's channel as missing while it is still called #realms.
  The bridge's old names now come from the channel's `"was"` list in `server/layout.py`.

## [1.0.0] - 2026-10-11

The first tagged state of the repo: everything from the first commit (2026-09-28) through
pull request #21, reconstructed from the git history. Numbers in brackets are pull requests.

### Added

- **Client theme**: the Field Notebook theme for Vencord and BetterDiscord
  (`theme/field-notebook.theme.css`), with night and paper variants.
- **Server as code** (`server/`): `layout.py` as the source of truth for games, roles and
  channels; `setup_server.py` (first build, dry run by default, never deletes);
  `polish_server.py` (Community, Onboarding, welcome screen, AutoMod, media lock behind a
  Verified role, trimming empty channels); `public_mode.py` (additive changes only, safe with
  the bot below the staff roles) [#1]; `snapshot_server.py` for a names-only, sorted config
  snapshot with `--compare` [#1].
- **Emoji and soundboard pack** generated from code (20 emoji, 8 sounds) with
  `expressions.py` to upload them [#1].
- **Front Desk bot** (`bot/`): skeleton with config, SQLite migrations, a single-instance
  lock and windowless start for the scheduled task [#1].
- Squad-up: `/lfg` forum posts with Join, Leave and Close buttons that survive restarts [#1].
- Stats: voice, message-count and game-time recording, `/stats`, `/leaderboard`, weekly MVP
  and the `/privacy` opt-out [#1].
- Growth and community: invite tracking, the Recruiter role, Disboard bump reminders,
  welcome messages after Onboarding, `/report` and a mod log [#1].
- Hall of fame, clip of the week, join-to-create voice channels, `/gamenight` with
  reminders, and weekly free-games posts [#1].
- Economy: wallets and an idempotent ledger (`bot/economy.py`), `/daily`, `/balance`,
  `/give`, `/coinflip`, `/richest`; `/slots`, `/blackjack`, `/trivia`, `/predict`; the coin
  shop, timed perks and monthly seasons [#1].
- Autopilot modules: engagement (question of the day, daily poll, counting, auto game
  night, birthdays), moderation (`/warn`, `/timeout`, `/cases`, `/purge`, auto-escalation,
  anti-spam, anti-raid), operations (daily backup, error alerts, weekly drift check,
  `/status`) and utility (`/remind`, `/afk`, suggestion voting, stat channels, tickets) [#1].
- Privacy policy (`PRIVACY.md`) [#1]; README feature list and runbook [#2]; `CLAUDE.md` [#3].
- Landing page (`site/`), welcome cards, tournaments with brackets and prizes, achievements
  with `/profile` and `/badges`, and the creator spotlight for YouTube and Twitch [#4].
- Levels with rank cards and level roles, weekly recap and owner digest, member milestones,
  monthly invite contest, the self-assign role panel, the partner program, the starter
  quest, creator ownership verification, and the heartbeat plus downtime watchdog [#5].
- Automatic monthly tournaments, weekly challenges, Daily Word, and community touches
  (chat revival, join anniversaries, booster thanks and stipend, member of the month) [#7].
- Matchmaking queue (`/queue`), coin sinks (weekly raffle, Gift Hype, Spotlight), staff
  `/economy` and the economy audit in `docs/ECONOMY.md` [#9].
- `/help` built from the live command list, `/about`, a rotating status, and AutoMod gap
  filling in `public_mode.py` [#10].
- Game news from official Steam and RSS feeds, staff applications (`/apply staff`), and a
  text filter for member text the bot reposts [#12].
- Permission self-check (after start and daily, with a line in `/status`) and a retry loop
  for welcome cards that fail to post [#14].
- Seasonal events (Halloween, Winter Fest) with drops, a daily quest and `/event`; Steam
  account linking (`/link`); the `/vote` hub and the short `/join` link; AI `/catchup`
  summaries; and the hosting kit (`Dockerfile`, `docker-compose.yml`, `docs/HOSTING.md`,
  `server/migrate_host.py`) [#20].
- GitHub Pages mirror of the landing page (`.github/workflows/pages.yml`) and listing-site
  copy and banners (`docs/LISTING.md`, `assets/listing/`).

### Changed

- The repo was renamed to `discord-server` and the roadmap extended beyond the first four
  bot modules [#1].
- Front Desk Levels replaced the hosted Lurkr bot for XP and level roles [#5].
- The starter-quest reward now needs a Discord account at least 30 days old [#6]; the same
  account-age rule was then applied to other rewards and to counted company [#8, #11].
- The landing page was rebuilt on the lscaturchio.xyz design system [#10] and then matched
  to that site's header, layout and typography [#17].
- "18+" was removed from the tagline and marketing copy, and the "Why 18+?" FAQ was
  dropped [#18, #19].
- The server snapshot was refreshed after public mode, the phase 3 channels and later
  server changes [#1, #16].
- Container-related files keep LF line endings on every platform (`.gitattributes`) [#21].

### Fixed

- Squad-up races and failed closes found in testing; `/gamenight` cooldown claimed before
  awaiting and released on refusal [#1].
- Voice and squad coins are capped per day and deafened idlers no longer earn; the squad
  coin cap is checked and paid in one transaction [#1].
- Eighteen issues from the first code and security review [#1].
- Onboarding falls back cleanly when the server is at Discord's question cap [#6].
- Load, ping and member-facing text issues found in review [#11].
- Front Desk can post in read-only channels that were created before it existed [#13].
- Game-night and tournament events no longer fail with 403: the separate Create Events
  permission is requested and checked; free-games posting was repaired [#14].
- Front Desk can open ticket threads in the help channel [#15].

### Security

- `/lfg` pings are restricted to the intended roles and users, so member text cannot ping
  arbitrary roles [#1].
- Purchased colour roles are name-prefixed so they cannot impersonate staff roles [#1].
- AFK reasons are escaped so the bot cannot be made to render masked links [#1].
- Reward-farming and authorization gaps in the wave 3 features were closed (account-age
  checks on rewards and on the accounts that count toward them, staff-only actions) [#8].
- Further alt-farming hardening after review [#11].
- Slurs and other servers' invite links are refused in member text the bot reposts, since
  Discord AutoMod does not act on bots [#12].
- Invite links were kept out of the AutoMod allow lists recorded in the snapshot [#16].
