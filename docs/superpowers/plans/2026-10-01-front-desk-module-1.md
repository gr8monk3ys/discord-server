# Plan: Front Desk foundation + Module 1 (Squad-up)

Spec: `docs/superpowers/specs/2026-10-01-front-desk-bot-design.md`
Branch: `front-desk-bot`
Python: 3.11 (the existing `server/.venv`), discord.py 2.7.1

Each task ends with its tests passing and a commit.

## Task 1: Shared name matching
- Move `slug()` from `server/setup_server.py` to a new `server/names.py`.
- `setup_server.py` and `polish_server.py` import it from `names`.
- Check: both scripts still import (`python -c "import setup_server, polish_server"` from `server/`).

## Task 2: Bot skeleton
- `bot/requirements.txt`: `discord.py>=2.4`, `python-dotenv`, `aiosqlite`, `tzdata`, `pytest`.
- `bot/config.py`:
  - Loads `server/.env` and `server/layout.py`.
  - `Game` dataclass, plus `build_games()`.
  - Pure `match_by_name(items, name)` for resolving channels, roles and tags.
- `bot/db.py`:
  - aiosqlite with WAL and numbered `MIGRATIONS`. Migration 1 creates `meta`, `jobs`,
    `privacy_optout`, `lfg_posts` and `lfg_members`.
  - Helpers `tracking_allowed()` and `transaction()` (`BEGIN IMMEDIATE`).
- `bot/style.py`: `embed()` with FOREST and mono footer labels.
- `bot/main.py`:
  - `FrontDesk(commands.Bot)`, which asks for privileged intents only when an enabled
    module needs them (module 1 needs none).
  - `setup_hook` migrates the DB, loads the cogs, registers the dynamic items and syncs to
    the guild.
  - A global error handler, and logging to `data/bot.log`.
  - `--invite` prints the OAuth URL, taking the app id from the token's first segment.
- `bot/run_bot.bat`, and `.gitignore` entries for `bot/data/`.
- Tests: `tests/test_config.py` (name matching, including Counter-Strike) and
  `tests/test_db.py` (migrations run on an empty database and are idempotent).

## Task 3: LFG logic (test first)
`bot/logic/lfg.py` (pure, no Discord imports):
- **Roster:** `Roster(host_id, size, members)`.
  - `join()` returns joined, already_in or full, and says whether the squad became full.
  - `leave()` returns left or not_in. The host can't leave and uses Close instead.
  - `resize()` refuses a size below the current member count.
- **Who can close:** `can_close(user, host, is_keeper)`.
- **Expiry:** `is_expired(created_at, now)`, at 3 hours.
- **Display text:** `title(game, mode)` (100-character limit), `count_label`, and
  `voice_hint(size)`, which suggests Squad for 5 or fewer and Lobby otherwise.

`tests/test_lfg.py` covers every transition in the spec's testing list.

## Task 4: LFG cog
`bot/cogs/lfg.py`:
- `/lfg game players mode when note`. Game choices come from `layout.GAMES`.
  - Creates the forum post, with tags, role pings and the embed with buttons.
  - If the host already has an open post for that game, it updates that post instead.
- `LfgButton(DynamicItem)` with template `lfg:(?P<action>join|leave|close):(?P<post>\d+)`.
  - State is read from SQLite.
  - On full, it posts the full-squad ping.
- Expiry loop every 5 minutes.
- Close disables the buttons, then does a single `thread.edit(name="✓ …", locked, archived)`.
- Reports missing forum, tags or roles at startup and turns `/lfg` off cleanly.

## Task 5: Docs
- README: a "Front Desk bot" section covering portal intents, the invite, role placement,
  running it, Task Scheduler, and the manual checklist for module 1.
- Spec: Python 3.11+ and `tzdata`.
