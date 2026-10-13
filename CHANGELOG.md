# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Security

- `/catchup` with the local model budgets the chat log in tokens, so a log of emoji or non-Latin text can no longer overflow the context window and push the system prompt out of it (budgeted at one token per byte, a true upper bound, with the costliest channel name assumed).

### Changed

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
