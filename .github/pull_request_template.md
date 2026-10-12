## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was checked

<!-- Say what you ran and what you did not. -->

## Checklist

- [ ] Bot tests pass: `cd bot && ../server/.venv/Scripts/python -m pytest -q`
- [ ] Server tests pass: `cd server && .venv/Scripts/python -m pytest -q tests`
- [ ] No secrets or live data: no tokens, webhook URLs, API keys, server or user IDs,
      member names, or anything from `bot/data/` or `.env`
- [ ] Schema changes are a **new** entry appended to `MIGRATIONS` in `bot/db.py`; no shipped
      migration was edited
- [ ] `README.md` is updated if behaviour, commands, schedules or the runbook changed
- [ ] `PRIVACY.md` is updated if what the bot stores about members changed (and the
      `front-desk-bot` branch is fast-forwarded after merge)
- [ ] `CHANGELOG.md` has a line under Unreleased

<!-- Delete the lines that do not apply to this change. -->

- [ ] Coins move only through `bot/economy.py`, with a `ref` on anything that can be retried
- [ ] Rewards are gated on account age (`logic/quests.established`) and respect `/privacy`
- [ ] Member-supplied text is filtered, escaped and sent with `ping_only(...)` or
      `AllowedMentions.none()`
- [ ] A new cog is in `MODULES` and its channels, permissions and roles are in
      `bot/logic/ops.py`
- [ ] `docs/ARCHITECTURE.md` still matches (module map, data model)
- [ ] Server scripts were dry-run first; the snapshot was refreshed after an `--apply`
