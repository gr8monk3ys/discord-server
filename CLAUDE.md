# CLAUDE.md

A Discord server managed as code: a client theme (`theme/`), one-shot setup scripts that create
roles, channels, Onboarding and AutoMod from `server/layout.py` (`server/`), and Front Desk, the
server's own discord.py bot (`bot/`). `README.md` is the feature list and runbook; the design and
build history is `docs/superpowers/specs/2026-10-01-front-desk-bot-design.md`.

**This repo is public.** Never commit `.env`, tokens, IDs of the live server, member names or
anything from `bot/data/` (the SQLite DB, logs and lock file live there and are gitignored).

## Where things live

- `server/layout.py` source of truth for games, roles, channels and their emoji names; the bot
  imports it (via `bot/config.py`), so the setup scripts and the bot always agree
- `server/setup_server.py` (first build), `polish_server.py` (Community, Onboarding, AutoMod),
  `public_mode.py` (later changes; adds what is missing and never deletes, but for games in
  `layout.RETIRED_GAMES` it drops their options from the "What do you play?" question, in
  `game_options` when that step adds a new game and otherwise in `retire_games`, and
  `retire_games` moves their channels into the staff-only `99 · archive` category),
  `focus_mode.py` (Onboarding only: cuts the default channels to the core in
  `ONBOARDING_DEFAULT_CHANNELS` and adds one opt-in question; `--plan`, `--revert`),
  `expressions.py` (emoji and soundboard upload),
  `snapshot_server.py` + `snapshot_lib.py` (read-only config dump to `server/snapshot/*.json`),
  `publish_site_data.py` + `site_schema.py` (push the landing page's live data),
  `register_linked_roles.py` (Linked Roles metadata, `docs/LINKED_ROLES.md`)
- `site/` the landing page; `site/api/` its Vercel functions (ban appeal form, Linked Roles),
  which read their secrets from Vercel environment variables, never from `server/.env`
- `bot/main.py` entry point; `MODULES` lists every cog with the privileged intents it needs
- `bot/logic/<name>.py` pure rules (no Discord I/O), `bot/cogs/<name>.py` Discord I/O
- `bot/db.py` SQLite via aiosqlite with append-only `MIGRATIONS`; `bot/economy.py` the only code
  that moves coins (ledger row + wallet update in one transaction)
- `bot/config.py` channel/role names and settings; `bot/style.py` embed colours;
  `bot/cogkit.py` helpers the cogs share (`is_staff`: import it, don't write another)
- `assets/` generated server icon, emoji and sounds plus the scripts that generate them
- `theme/field-notebook.theme.css` Vencord/BetterDiscord theme

## Commands

The bot and scripts share one virtualenv at `server/.venv` (gitignored); it runs on Python 3.11.

- Install: `python -m venv server/.venv`, then `server/.venv/Scripts/pip install -r requirements-dev.txt`
  (the runtime pins in `bot/requirements.txt` plus pytest and ruff; `server/requirements.txt` is a
  subset of the runtime pins). A new third-party import needs a line in `bot/requirements.txt`.
- Bot tests: `cd bot && ../server/.venv/Scripts/python -m pytest -q -p no:warnings` (~4,100 tests, a few minutes)
- Server tests: `cd server && .venv/Scripts/python -m pytest -q tests` (255 tests)
- Site function tests: `cd site/api && node --test` (34 tests, no network, no `npm install`;
  run from that folder, as CI does, so every Node version finds the test files)
- Lint: `server/.venv/Scripts/ruff check .` from the repo root (`ruff.toml`: bug-catching rules only,
  no formatter). `pyrightconfig.json` is for editors; pyright is not clean and is not part of CI.
- Every server script dry-runs by default and changes Discord only with `--apply`.
- `python main.py --invite` (in `bot/`) prints the invite link with the permissions the bot needs.

CI (`.github/workflows/ci.yml`) runs ruff, both Python test suites and the site function tests on every pull request and push to
`main`; still run them locally before pushing.

## Conventions

- New tables go in a **new** `MIGRATIONS` entry; never edit a shipped migration.
- Coins move only through `bot/economy.py` (`*_tx` variants inside `db.transaction()`); payouts
  carry a `ref` so they can't be paid twice.
- Member-authored text is sent with `ping_only(...)` or `AllowedMentions.none()`, and markdown in
  bot-authored text is escaped (masked links, impersonation).
- Purchased colour roles are prefixed so they can't look like staff roles.
- Scheduled jobs use Pacific time (`TZ` overrides), catch up once after downtime and never run twice.
- Snapshots use names, not IDs, and sorted output so `git diff server/snapshot` is meaningful.
- A new module = `logic/` + tests, `cogs/`, then an entry in `MODULES` (README "Adding a module").

## Gotchas

- Role order: Front Desk sits below Keeper/Moderator and cannot move roles to or above its own
  position. Never reorder roles near the bot (`setup_server.py` calls `edit_role_positions`); use
  `public_mode.py`, which never reorders or deletes (it only adds, except that it drops the
  Onboarding options of retired games and archives their channels; read its dry run first). Roles the bot hands out must sit below it.
- The live bot runs from a scheduled task and holds `bot/data/bot.lock`; stop the task before
  running `main.py` by hand (README runbook).
- The "Front Desk watchdog" task restarts the bot if `bot/data/heartbeat` goes stale; run
  `python server/watchdog.py --pause 60` before stopping the bot on purpose.
- Keep the `front-desk-bot` branch: the bot's public privacy-policy link points at `PRIVACY.md`
  on that branch. After changing `PRIVACY.md` on `main`, fast-forward that branch to `main`.
- The `site-data` branch is not a code branch: it holds one force-pushed, parentless commit
  with the single file `stats.json`, written only by `server/publish_site_data.py`. Never
  commit to it, merge it, rebase onto it or delete it; the landing page reads it.
- Soundboard uploads must be MP3 (discord.py only accepts MP3); `make_sounds.py` needs ffmpeg.
