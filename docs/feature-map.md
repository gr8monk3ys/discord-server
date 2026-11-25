# Feature map

Every feature a person can reach, in the words they would use. Three surfaces: the landing page
(`site/`), Front Desk's slash commands and automatic behaviours (`bot/`), and the owner's
setup scripts (`server/`).

How each row was verified when this map was written:

- **Ran** means it was started or executed locally and the output matched.
- **Test only** means it needs a live Discord token, a Discord server or a production secret, so it
  was not run live. The row names the nearest test file instead.

Commands below use `PY=server/.venv/bin/python` (the shared virtualenv from `CLAUDE.md`; on
Windows `server/.venv/Scripts/python`). Bot tests run from `bot/`, server tests from `server/`.

The slash-command rows list the commands the bot registers today: the modules in `MODULES` in
`bot/main.py`, minus `PARKED`. Parked modules (games, tournaments, creators, partners,
challenges, word game, matchmaking, staff applications, game link, catchup, supporter, forums,
clipcast, houses, clips, vibes, news) register no commands until they are unparked.

## Landing page

Boot it: `cd site && bun install --frozen-lockfile && bun run build && PORT=4173 bun run start`.

| Feature | Where | How to reach it | How to check it works |
|---|---|---|---|
| Home page: what the server is, the games, a normal week, Monkey SMP, live numbers, house rules, FAQ | `site/src/app/page.tsx` | `GET /` | Ran: `curl -s -o /dev/null -w '%{http_code}' localhost:4173/` prints 200; the page carries sections `top`, `games`, `features`, `week`, `smp`, `live`, `rules`, `faq`. Tests: `cd site && bun run test` (`src/app/pages.test.tsx`) |
| Join the server | `site/src/app/join/page.tsx` | `GET /join` | Ran: answers 307 with `location: https://discord.gg/<invite>` (invite code from `site/src/data/site-config.json`) |
| Ban appeal form | `site/src/app/appeal/page.tsx`, `site/src/components/` | `GET /appeal` | Ran: 200, title "Appeal a ban". Submitting is Test only (needs the appeal secrets in the Vercel environment): `site/src/server/appeal.test.ts`, `site/src/lib/appeal-form.test.ts` |
| Ban appeal submission | `site/src/app/api/appeal/route.ts`, `site/src/server/appeal.ts` | `POST /api/appeal` | Ran without secrets: `GET` is 405, `POST` is 503 with `{"ok":false,"error":"unavailable",...}`. The accepted path is Test only: `site/src/server/appeal.test.ts` |
| Linked Roles explainer | `site/src/app/linked-role/page.tsx` | `GET /linked-role` | Ran: 200, title "Linked roles · Chill Gamers" |
| Linked Roles sign-in | `site/src/app/api/linked-role/start/route.ts`, `callback/route.ts`, `site/src/server/linked-role/` | `GET /api/linked-role/start`, then Discord's redirect to `/api/linked-role/callback` | Ran without secrets: both answer 503 with the "Linked roles aren't set up yet." page. The OAuth round trip is Test only: `site/src/server/linked-role/linked-role.test.tsx` |
| Not-found page | `site/src/app/not-found.tsx` | any unknown path, e.g. `GET /nope` | Ran: 404, title "Page not found (404) · Chill Gamers" |
| Per-request Content-Security-Policy and security headers | `site/src/proxy.ts`, `site/src/data/security.json` | every response | Ran: `curl -sI localhost:4173/` shows `content-security-policy` with a nonce and `X-Frame-Options: DENY`. Browser checks: `cd site && bun run test:e2e` (Playwright, not run here) |

## Front Desk slash commands

Test only for every row in this section: Front Desk needs `DISCORD_TOKEN` and a guild to answer
anyone. What was run is the registration: all loaded modules were loaded into a `FrontDesk` bot
without connecting, and the command tree listed 79 commands (78 slash commands and the "Report
message" context menu), all in the table below. Each row's check runs that cog's tests:
`cd bot && $PY -m pytest -q tests/test_<cog>_cog.py`.

| Feature | Where | How to reach it | How to check it works |
|---|---|---|---|
| Find a squad | `bot/cogs/lfg.py` | `/lfg` in the server | `test_lfg_cog.py` |
| Squad voice channels: rename, limit, lock, unlock, invite, kick, claim | `bot/cogs/tempvoice.py` | join the "create" voice channel, then `/squad name\|limit\|lock\|unlock\|invite\|kick\|claim` | `test_tempvoice_cog.py` |
| Game nights and watch parties | `bot/cogs/events.py` | `/gamenight`, `/watchparty` | `test_events_cog.py` |
| Your stats, the activity board, stats opt-out | `bot/cogs/stats.py` | `/stats`, `/leaderboard`, `/privacy` | `test_stats_cog.py` |
| Coins: claim, balance, give, bet, rich list | `bot/cogs/economy.py` | `/daily`, `/balance`, `/give`, `/coinflip`, `/richest` | `test_economy_cog.py`, `test_economy_core.py` |
| Shop, seasons and the weekly raffle | `bot/cogs/shop.py` | `/shop`, `/buy`, `/season`, `/raffle info`, `/raffle buy` | `test_shop_cog.py`, `test_shop_sinks_cog.py` |
| Levels and XP | `bot/cogs/levels.py` | `/rank`, `/levels`; staff `/xp set`, `/xp reset` | `test_levels_cog.py` |
| Profile and badges | `bot/cogs/achievements.py` | `/profile`, `/badges` | `test_achievements_cog.py` |
| Starter quest | `bot/cogs/quests.py` | `/quest` | `test_quests_cog.py` |
| Self-assign roles | `bot/cogs/selfroles.py` | the #roles panel, `/roles` | `test_selfroles_cog.py` |
| Birthdays | `bot/cogs/engagement.py` | `/birthday set\|remove\|list` | `test_engagement_cog.py` |
| Reminders and AFK | `bot/cogs/utility.py` | `/remind`, `/reminders`, `/afk`; staff `/suggestion status` | `test_utility_cog.py` |
| Report a member to the mods | `bot/cogs/community.py` | `/report`, or right-click a message, Apps, "Report message" | `test_community_cog.py` |
| Moderation: warn, timeout, untimeout, ban, purge, case history | `bot/cogs/moderation.py` | mods: `/warn`, `/timeout`, `/untimeout`, `/ban`, `/purge`, `/cases` | `test_moderation_cog.py`, `test_staff_commands.py` |
| Ban appeal review (staff side) | `bot/cogs/appeals.py` | staff: `/appeals list`, `/appeals code` | `test_appeals_cog.py` |
| Invite tracking and Disboard bumps | `bot/cogs/growth.py` | `/invites`, `/recruiters`, `/bumpers`, `/bumpping` | `test_growth_cog.py` |
| Support links | `bot/cogs/votehub.py` | `/vote` | `test_votehub_cog.py` |
| Welcome Crew | `bot/cogs/crew.py` | `/crew`; staff `/crew-stats` | `test_crew_cog.py` |
| Seasonal event | `bot/cogs/seasonal.py` | `/event` | `test_seasonal_cog.py` |
| Year in review card | `bot/cogs/wrapped.py` | `/wrapped` (December 1 to January 15); staff `/wrapped-preview` | `test_wrapped_cog.py` |
| Monkey SMP: link your Minecraft name, status, top players, nudges | `bot/cogs/monkey.py` | `/smp link\|unlink\|status\|top\|nudges`; mods `/smp adopt\|remove` | `test_monkey_cog.py`, `test_smpnudge_cog.py` |
| Monkey SMP factions | `bot/cogs/factions.py` | `/faction register\|charter\|pitch\|recruit\|lfg\|seekers` | `test_factions_cog.py` |
| Monkey SMP referral code | `bot/cogs/referral.py` | `/referral` (off unless `REFERRALS=on`) | `test_referral_cog.py` |
| Base of the Month | `bot/cogs/botm.py` | staff `/botm award` | `test_botm_cog.py` |
| Help and server info | `bot/cogs/helpdesk.py` | `/help`, `/about` | `test_helpdesk_cog.py`, `test_helpdesk.py` |
| Bot health | `bot/cogs/ops.py` | staff `/status` | `test_ops_cog.py` |
| Economy numbers | `bot/cogs/econstats.py` | staff `/economy` | `test_econstats_cog.py` |
| Landing-page live data refresh | `bot/cogs/sitedata.py` | staff `/sitedata refresh` | `test_sitedata_cog.py` |
| Field notes preview | `bot/cogs/fieldnotes.py` | staff `/fieldnotes preview` | `test_fieldnotes_cog.py` |

## Front Desk, no command needed

Test only: these run from Discord events or timers.

| Feature | Where | How to reach it | How to check it works |
|---|---|---|---|
| Welcome flow and mod log | `bot/cogs/community.py`, `bot/cogs/cards.py` | join the server and finish Onboarding | `test_community_cog.py`, `test_cards_cog.py` |
| Anti-spam and anti-raid | `bot/cogs/moderation.py`, `bot/quietgate.py` | post spam, or many joins at once | `test_moderation_cog.py`, `test_quietgate.py` |
| Hall of fame | `bot/cogs/starboard.py` | react with the star to a message | `test_starboard_cog.py` |
| Question of the day, daily poll, counting | `bot/cogs/engagement.py` | scheduled; post in the counting channel | `test_engagement_cog.py` |
| Weekly recap and milestones | `bot/cogs/recap.py` | scheduled | `test_recap_cog.py` |
| "Tonight" line, SMP ping, newcomer voice ping | `bot/cogs/tonight.py`, `smpwatch.py`, `welcomers.py` | people in voice or on Monkey SMP (`TONIGHT=off`, `SMP_WATCH=off` turn them off) | `test_tonight_cog.py`, `test_smpwatch_cog.py`, `test_welcomers_cog.py` |
| Tebex "pass ending" alerts | `bot/cogs/tebex.py` | Tebex webhook (off without `TEBEX_WEBHOOK_SECRET`; `docs/TEBEX_WEBHOOK.md`) | `test_tebex_cog.py`, `test_tebex.py` |
| Heartbeat for the watchdog | `bot/cogs/heartbeat.py` | written every minute to `bot/data/heartbeat` | `test_heartbeat_cog.py` |
| Database backups and health alerts | `bot/cogs/ops.py`, `bot/db.py` | scheduled | `test_ops.py`, `test_db.py` |
| Which modules load | `bot/main.py` (`MODULES`, `PARKED`, `PARKED_MODULES`) | start the bot | `test_parked.py` |

## Owner command-line tools

| Feature | Where | How to reach it | How to check it works |
|---|---|---|---|
| Print the bot's invite link | `bot/main.py` | `cd bot && DISCORD_TOKEN=<token> GUILD_ID=<id> $PY main.py --invite` | Ran with a made-up token: printed `https://discord.com/oauth2/authorize?client_id=123456789012345678&scope=bot+applications.commands&permissions=2833845482286197&guild_id=1&disable_guild_select=true` |
| Run the bot | `bot/main.py`, `Dockerfile`, `docs/HOSTING.md` | `cd bot && $PY main.py` (needs `server/.env`) | Test only: `make check`; the container health path is `test_heartbeat_cog.py` and `server/tests/test_watchdog.py` |
| First server build | `server/setup_server.py` | `cd server && $PY setup_server.py` (dry run), `--apply` | Ran `--help` (exit 0). A dry run needs `DISCORD_TOKEN` and `GUILD_ID`, so Test only: `server/tests/test_server_scripts.py` |
| Community, AutoMod and media-lock polish | `server/polish_server.py` | `$PY polish_server.py [--apply]` | Ran `--help`. Test only: `server/tests/test_server_scripts.py` |
| Later layout changes, game retirement, channel archive | `server/public_mode.py` | `$PY public_mode.py [--apply]` | Ran `--help`. Test only: `server/tests/test_retire_channels.py`, `test_smp_season.py` |
| Shorten the newcomer channel list | `server/focus_mode.py` | `$PY focus_mode.py --plan` (offline), `[--apply]`, `--revert` | Ran `--plan`: printed each channel as `core` or an opt-in group. Test only for the rest: `server/tests/test_focus_mode.py` |
| Emoji and soundboard upload | `server/expressions.py` | `$PY expressions.py [--apply] [--replace]` | Ran `--help`. Test only: `server/tests/test_expressions.py` |
| Server config snapshot | `server/snapshot_server.py`, `server/snapshot/*.json` | `$PY snapshot_server.py [--compare]` | Ran: without a token it exits with "Set DISCORD_TOKEN and GUILD_ID in server/.env". Test only: `server/tests/test_snapshot.py` |
| Publish landing-page live data | `server/publish_site_data.py` | `$PY publish_site_data.py [--apply] [--quiet]` | Ran: with no `bot/data/site/stats.json` it prints `no-file: stats.json has not been written yet`. Test: `server/tests/test_publish_site_data.py` |
| Register Linked Roles metadata | `server/register_linked_roles.py` | `$PY register_linked_roles.py [--apply]` | Ran `--help`. Test only: `server/tests/test_register_linked_roles.py` |
| Move the bot to a new host | `server/migrate_host.py` | `$PY migrate_host.py pack\|unpack [--apply]` | Ran `pack` against an empty data dir: "Stopped: No database at ...". Test: `server/tests/test_migrate_host.py` |
| Watchdog | `server/watchdog.py` | `$PY watchdog.py --check` (also `--pause`, `--resume`, `--alert-only`) | Ran `--check`: reports `heartbeat: missing (STALE)` when no bot is running. Test: `server/tests/test_watchdog.py` |
| Regenerate the site's games and rules | `bot/logic/sitegen.py` | `cd bot && $PY -m logic.sitegen` | Ran: `games.json: already current`, `rules.json: already current`. Test: `test_sitegen.py` |
| Every check CI runs, except the image, Lighthouse and Playwright | `Makefile`, `scripts/verify` | `make check` | Ran on `main`: exit 0 |
