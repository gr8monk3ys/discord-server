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

### Security

- `/catchup` with the local model budgets the chat log in tokens, so a log of emoji or non-Latin text can no longer overflow the context window and push the system prompt out of it.

### Changed

- Requirements are complete and pinned (Pillow and aiohttp were missing); the Docker image installs from them.

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
