# Contributing

This is the code for one Discord server and its bot, Front Desk. Fixes and ideas are welcome.
Read `CLAUDE.md` (conventions and gotchas) and `docs/ARCHITECTURE.md` (how the parts fit)
before a first change; `README.md` is the feature list and runbook.

## This repo is public

Never commit, paste into an issue or include in a test:

- `.env` files, bot tokens, webhook URLs or API keys
- IDs of the live server, its channels, roles or members
- member names, or anything from `bot/data/` (database, logs, backups)

Use made-up IDs and names in tests. `server/snapshot/*.json` is the one place live
configuration is recorded, by name only. If you find a secret in the history, report it
privately (see `SECURITY.md`).

## Setup

Python 3.11. The bot and the server scripts share one virtualenv at `server/.venv`.

```bash
python -m venv server/.venv
server/.venv/Scripts/pip install -r requirements-dev.txt   # runtime + pytest + ruff
```

On Linux or macOS the interpreter is `server/.venv/bin/python`. `requirements-dev.txt`
includes `bot/requirements.txt`, which is a superset of `server/requirements.txt`.

To run the bot you need your own test server and bot application: copy
`server/.env.example` to `server/.env`, fill in your token and server ID, and enable the
Server Members, Message Content and Presence intents in the Developer Portal. `python
main.py --invite` (in `bot/`) prints an invite link with the permissions the bot needs.
Never point a development copy at the live server: two bots on one token both answer every
click.

## Checks to run before pushing

```bash
cd bot && ../server/.venv/Scripts/python -m pytest -q          # about 4,200 tests, a few minutes
cd server && .venv/Scripts/python -m pytest -q tests           # 275 tests, seconds
cd site/api && node --test                                     # 34 tests, the site functions
server/.venv/Scripts/python -m ruff check .                    # from the repo root
```

The suites need no token, no `.env` and no network: they use fakes and in-memory or temporary
SQLite databases. CI runs all of them. Run both Python suites,
since `bot/` imports `server/layout.py` and a layout change can break bot tests (and
`bot/tests/test_sitegen.py` fails when `site/index.html` drifts from the games list).

## Conventions

- **Layers.** Rules go in `bot/logic/<name>.py` (pure, with tests); Discord I/O goes in
  `bot/cogs/<name>.py`. A new module is registered in `MODULES` in `bot/main.py` with the
  privileged intents it needs, and what it needs from the server (channels, permissions,
  roles) goes in the tables in `bot/logic/ops.py`.
- **Migrations are append-only.** New tables or columns are a new entry at the end of
  `MIGRATIONS` in `bot/db.py`. Never edit an entry that has shipped.
- **Coins** move only through `bot/economy.py` (the `*_tx` variants inside
  `db.transaction()`); any payout that could be retried carries a `ref`.
- **Rewards** are gated on account age (`logic/quests.established`, 30 days) and respect the
  privacy opt-out (`db.tracking_allowed`).
- **Member text** the bot reposts goes through `logic/textfilter`, has its markdown escaped
  and is sent with `ping_only(...)` or `AllowedMentions.none()`.
- **Scheduled jobs** follow `logic/schedule.py`: local time, catch up once after downtime,
  never run twice. A cog runs one through `bot/jobs.py` (`run_due`, or `due` and `done`).
- **Server scripts** dry-run by default and change Discord only with `--apply`. Later
  changes to the live server go through `public_mode.py` (additive, apart from archiving the
  channels of retired games); do not add anything that reorders or deletes roles or channels.
- **Snapshots** use names, not IDs, and sorted output.
- **Docs travel with the change.** Update `README.md` when behaviour, commands or schedules
  change, `PRIVACY.md` when what is stored about members changes, `docs/ECONOMY.md` for a
  new coin source or sink, `docs/ARCHITECTURE.md` for a new module or table, and add a line
  to `CHANGELOG.md` under Unreleased.
- Lint is `ruff check .` with the small rule set in `ruff.toml` (real bugs only, no
  formatter). Match the style of the file you are editing; lines up to 120 columns.

## Pull requests

- All changes reach `main` through a pull request from a branch; do not push to `main`.
- One topic per pull request. Fill in the template checklist honestly: say what you ran and
  what you did not.
- Commit subjects are short and imperative; a `feat:` / `fix:` / `docs:` / `chore:` prefix
  is welcome.
- Do not force-push `main`, and keep the `front-desk-bot` branch: the bot's public privacy
  link points at `PRIVACY.md` on it.
- A change is not live when it merges. The owner updates the checkout the bot runs from and
  restarts the bot task (README runbook).

## Reporting bugs and ideas

Use the issue templates. Security problems go to a private advisory instead: `SECURITY.md`.

## Continuous integration

Every pull request and push to `main` runs `ruff check .` and both test suites on Linux
(`.github/workflows/ci.yml`). Run them locally first; a red check blocks nothing by itself, but
do not merge on red.
