# Security policy

This repo holds the code for one Discord server: the setup scripts in `server/`, the Front
Desk bot in `bot/` and the landing page in `site/`. Only the `main` branch is supported;
fixes land there and the live bot is updated from it.

## Reporting a vulnerability

Please report privately, not in a public issue and not in the Discord server.

1. Open <https://github.com/gr8monk3ys/discord-server/security/advisories/new>
   (the repo's **Security** tab, then **Report a vulnerability**).
2. Say what is affected (file, command or page), how to reproduce it, and what an attacker
   gains. A minimal example is more useful than a long description.
3. Do not test against the live server or its members. The bot runs locally against a
   throwaway server with your own token (see `CONTRIBUTING.md`).

You should get a reply within a week. Once a fix is on `main` the advisory is published and
you are credited unless you ask not to be.

## In scope

- The bot (`bot/`): coin duplication or theft, rewards that can be farmed with fresh
  accounts, staff-only commands or buttons reachable by members, member-supplied text that
  can ping roles or everyone, render masked links or slip past the text filter, anything
  that reveals another member's data or ignores `/privacy tracking:off`, and unsafe
  handling of fetched content (feeds, Steam and creator pages, invite checks).
- The setup scripts and tools (`server/`): a dry run that changes the server, anything
  that deletes or reorders without `--apply`, the watchdog leaking its webhook, and
  `migrate_host.py` packing or unpacking more than the database.
- The landing page (`site/`): script injection, a bypass of the Content-Security-Policy in
  `vercel.json`, or an open redirect through `/join`.
- The container kit (`Dockerfile`, `docker-compose.yml`, `bot/run_container.sh`).
- A secret, live ID or member data that was committed by mistake. Report it the same way,
  so it can be rotated before it is discussed.

## Out of scope

- Discord itself, and the hosted bots the server uses (Captcha.bot, GiveawayBot, Disboard).
- The client theme. Vencord and BetterDiscord are third-party client mods; using them is
  the user's own decision (see `README.md`).
- Denial of service that needs staff permissions, and social engineering of members or staff.
- Moderation matters (a member breaking the rules): use `/report` in the server.

## What is never in this repo

- The bot token, the server ID, the watchdog webhook URL and any API keys. They live only in
  `server/.env` on the machine that runs the bot; `.env` is gitignored and
  `server/.env.example` holds names and instructions, never values.
- The database, logs, lock file, heartbeat and backups (`bot/data/`, gitignored apart from
  the two question banks).
- Member names and IDs. `server/snapshot/*.json` records the server's configuration by
  name only and never members, messages or invites.

If you find any of these in the history, that is a vulnerability: please report it.
