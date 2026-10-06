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
  `public_mode.py` (additive changes only), `expressions.py` (emoji and soundboard upload),
  `snapshot_server.py` + `snapshot_lib.py` (read-only config dump to `server/snapshot/*.json`)
- `bot/main.py` entry point; `MODULES` lists every cog with the privileged intents it needs
- `bot/logic/<name>.py` pure rules (no Discord I/O), `bot/cogs/<name>.py` Discord I/O
- `bot/db.py` SQLite via aiosqlite with append-only `MIGRATIONS`; `bot/economy.py` the only code
  that moves coins (ledger row + wallet update in one transaction)
- `bot/config.py` channel/role names and settings; `bot/style.py` embed colours
- `assets/` generated server icon, emoji and sounds plus the scripts that generate them
- `theme/field-notebook.theme.css` Vencord/BetterDiscord theme

## Commands

The bot and scripts share one virtualenv at `server/.venv` (gitignored); it runs on Python 3.11.

- Install: `python -m venv server/.venv`, then `pip install -r bot/requirements.txt` (a superset
  of `server/requirements.txt`, adds pytest). `server/tests` also need `pillow`.
- Bot tests: `cd bot && ../server/.venv/Scripts/python -m pytest -q` (~1,200 tests, about a minute)
- Server tests: `cd server && .venv/Scripts/python -m pytest -q tests`
- Every server script dry-runs by default and changes Discord only with `--apply`.
- `python main.py --invite` (in `bot/`) prints the invite link with the permissions the bot needs.

There is no CI; run both test suites before pushing.

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
  `public_mode.py`, which only adds. Roles the bot hands out must sit below it.
- The live bot runs from a scheduled task and holds `bot/data/bot.lock`; stop the task before
  running `main.py` by hand (README runbook).
- Keep the `front-desk-bot` branch: the bot's public privacy-policy link points at `PRIVACY.md`
  on that branch. After changing `PRIVACY.md` on `main`, fast-forward that branch to `main`.
- Soundboard uploads must be MP3 (discord.py only accepts MP3); `make_sounds.py` needs ffmpeg.
