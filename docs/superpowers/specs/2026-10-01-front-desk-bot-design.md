# Front Desk bot: design

Date: 2026-10-01
Status: approved by Lorenzo (all recommendations accepted). Revised after spec review round 1.

## Goal

Lorenzo's Server is a small friend-group gaming server (under ~25 people). `server/setup_server.py`
and `server/polish_server.py` have already set it up: roles, Onboarding, AutoMod, the 🎮・lfg
forum, and Lurkr for levels. The next step is a **persistent custom bot** that makes the group
play and talk together more. It has four modules, built and shipped in this order:

1. **Squad-up (LFG)**: one command posts a "need N more" card with Join buttons.
2. **Stats and leaderboards**: voice time, messages, game time and clips, with a weekly MVP.
3. **Clip of the week**: a Sunday poll over the week's clips; the winner gets a role.
4. **Economy and mini-games**, in two halves:
   - **4a:** wallet, earning, `/daily`, `/give` and coinflip.
   - **4b:** slots, blackjack, trivia and predictions, built only once 4a is in real use.

Each module works on its own. Modules 3 and 4 read data that module 2 records, but nothing
breaks if a module is disabled. They just have less to show.

## Non-goals

- **XP levels.** Lurkr already does these. The economy uses coins, not levels.
- **Moderation, captcha, giveaways.** AutoMod, Captcha.bot and GiveawayBot already cover them.
- **Real money.** Coins can't be bought, sold or cashed out. The bot has no shop.
- **Always-on hosting.** The bot runs on Lorenzo's PC (his decision). Moving it to a host
  later must need no code changes.
- **Multiple servers.** The bot serves one guild (`GUILD_ID`).

## Constraints and decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+, discord.py ≥ 2.4, `tzdata` on Windows | Matches the existing scripts, `.venv` and `layout.py` |
| Location | `bot/` in this repo | Reuses `server/layout.py` and `server/.env` |
| Bot app | Reuse the existing "Front Desk" application and token | Already exists; Lorenzo re-invites it with the link below |
| Storage | SQLite `bot/data/front_desk.db` via `aiosqlite`, WAL mode | No server to install; one file to back up |
| Commands | Guild-scoped slash commands, synced on startup | Appear instantly in one server |
| Style | Field Notebook embeds: colour `FOREST` (0x42A979), mono footer labels like `SQUAD-UP · VALORANT` | Matches the existing rules and welcome posts |
| Timezone | `TZ` in `.env` (default `America/Los_Angeles`) via `zoneinfo` | Weekly jobs and "per day" caps use local days |
| Time | Every `logic/` function takes `now` as a parameter | Deterministic tests |

### Permissions (not Administrator)

Invite URL scopes: `bot applications.commands`. Permissions: View Channels, Send Messages,
Send Messages in Threads, Create Public Threads, Manage Threads, Embed Links, Attach Files,
Read Message History, Add Reactions, Manage Roles, Send Polls.

The bot also gets a **channel override on 📸・clips with Manage Messages**, so that the
channel's 10-second slowmode doesn't block it. `polish_server.py` gets a step that sets this
override. The bot's managed role must sit **above `Clip of the Week`** and below Keeper.

Privileged intents to switch on in the Developer Portal:
- **Server Members:** names on leaderboards, and giving roles.
- **Message Content:** reading attachments and links in 📸・clips. Without it, attachments
  arrive empty too.
- **Presence:** game time.

Discord allows all three without verification for bots in fewer than 100 servers.

### Privacy

The bot records **counts and durations only, never message text**. Recording goes through one
gate, `db.tracking_allowed(user_id)` in `db.py`. Every recorder in every module calls it before
writing, so it still works when module 2 is disabled.

- `/privacy off` adds the user to `privacy_optout` and deletes their rows in `voice_sessions`,
  `game_sessions` and `message_counts`.
- It keeps their `clips` rows (the clips are public posts, and the poll needs them) and their
  wallet and ledger (coins are theirs).
- `/privacy on` removes the opt-out. Tracking restarts from that moment.

Lorenzo adds one line about this to the rules post himself. The bot doesn't edit the rules.

## Architecture

```
server/
  names.py           # NEW: slug() moved here; setup_server.py and polish_server.py import it
bot/
  main.py            # client, intents, loads cogs, syncs commands; --invite prints the invite URL
  config.py          # reads server/.env and layout.py; builds the Game registry (below)
  db.py              # aiosqlite connection, numbered migrations, transaction helper (BEGIN IMMEDIATE)
  style.py           # embed(), footer labels, colours: the only place that knows the look
  cogs/
    lfg.py  stats.py  clips.py  economy.py  games.py
  logic/             # pure functions, no discord imports, unit-tested
    lfg.py  stats.py  clips.py  economy.py  games.py  blackjack.py
  tests/
  requirements.txt   # discord.py, python-dotenv, aiosqlite, pytest
  run_bot.bat        # uses ../server/.venv to run main.py; Task Scheduler target
  data/              # gitignored: front_desk.db, bot.log
```

**Boundary rule:** cogs handle Discord I/O (commands, buttons, events, embeds), and `logic/`
makes every decision that can be tested: roster changes, counted minutes, payouts, winners.

**Game registry:** `config.py` builds one `Game` per `layout.GAMES` tuple
`(emoji, channel, role)`:
- `channel` is matched against channel names with `slug(f"{emoji}・{channel}")`.
- `role` is matched against role names, and also against the 🎮・lfg forum's tag names.

The tuple is the key. Nothing derives the role from the channel slug, which would break for
`counter-strike` and `Counter-Strike 2`. On startup the bot logs every channel, role or tag it
couldn't find, and it disables only the features that need the missing ones.

**Restart-safe buttons:** all buttons that live beyond one interaction use `discord.ui.DynamicItem`
with templates, registered once with `bot.add_dynamic_items(...)`:
- `lfg:(?P<action>join|leave|close):(?P<post>\d+)`
- `pred:(?P<action>bet|lock|resolve|cancel):(?P<id>\d+)(:(?P<opt>[ab]))?`

The state lives in SQLite, never in the view.

**Scheduled jobs** use `discord.ext.tasks`. Each scheduled job has a key and a period, for
example `mvp:2026-W40`. The `jobs(key PRIMARY KEY, done_at)` table records finished periods.
- On startup and every 5 minutes, the **most recent** period whose scheduled time has passed
  and isn't in `jobs` runs, with its data window **anchored to the scheduled time**, not to "now".
  Older missed periods are marked done without running. The very first startup marks every
  past period as done, so the bot doesn't backfill history.
- Payouts are idempotent. `ledger.ref` is `UNIQUE`, for example `mvp:2026-W40:<user_id>`,
  and is inserted with `INSERT OR IGNORE`. A crash between paying and marking the job done
  can't pay twice.

**Heartbeat:** `meta(key, value)` holds `heartbeat` (written every minute). On startup, open
voice and game sessions are closed at the last heartbeat. Then sessions are opened for members
who are currently in voice or currently playing.

**Errors:** a global app-command error handler logs the traceback to `data/bot.log` and replies
ephemerally with "That didn't work. Lorenzo, check bot.log." Button handlers do the same.
Coin changes go through `economy.transfer()` in one `BEGIN IMMEDIATE` transaction. Balances have
`CHECK(balance >= 0)`, and the code checks first so it can give a friendly "not enough coins".

## Module 1: Squad-up (LFG)

**Command:** `/lfg game:<choice> players:<2–10> mode:<optional: Ranked|Casual> when:<text, default "now"> note:<optional>`

- **The post:** the bot creates a 🎮・lfg forum post, tagged with the game's tag plus the
  `mode` tag if given.
  - The title is set once, as `Valorant` or `Valorant · Ranked`. It doesn't contain the
    time, so updates never need a rename. `when` is shown in the embed.
  - The starter message pings the game role and @LFG.
  - An embed shows the **live count and roster** (`2 / 5`, host first). Counts live only
    in the embed, because Discord limits thread renames to about 2 per 10 minutes.
- **Buttons:** Join, Leave, and Close (host and Keepers only).
  - When the roster fills, the bot posts "Squad's full: @a @b @c". It suggests 🎮 Squad when
    the squad is 5 or fewer, and 🔊 Lobby otherwise.
  - Join is turned off while the squad is full. Leave stays on, so a drop reopens a spot.
- **Closing:** a post closes 3 hours after creation, or when Close is pressed. Closing is
  **one** `thread.edit(name="✓ " + title, locked=True, archived=True)` call, and buttons are
  turned off first. A job checks expiry every 5 minutes.
- **Duplicates:** a host can have one open post per game. Running `/lfg` again for the same
  game updates that post (size, when, note) and replies with a link to it.

Tables:
- `lfg_posts(id, thread_id, message_id, game, host_id, size, mode, when_text, note, created_at, closed_at)`
- `lfg_members(post_id, user_id, joined_at, PRIMARY KEY(post_id, user_id))`

## Module 2: Stats and leaderboards

**Recording** (all through the privacy gate):
- **Voice:** `voice_sessions(user_id, channel_id, start, end)`, raw join and leave times.
  Ignores 💤 AFK and staff channels.
- **Counted voice minutes:** computed in `logic/stats.py` from overlapping sessions. A minute
  counts only while 2 or more non-bot members are in the same non-AFK channel. Leaderboards
  and coins both use this function.
- **Messages:** `message_counts(user_id, day, count, PRIMARY KEY(user_id, day))`. Bots and
  staff channels are ignored, and `day` is the local date.
- **Game time:** `game_sessions(user_id, game_name, start, end)` from the "Playing" activity
  in `on_presence_update`. Sessions under 5 minutes are deleted when they close. Crash
  recovery and startup work as described under Heartbeat.
- **Clips:** module 3's `clips` table.

**Commands:**
- `/stats [member]`: this week and all time. Shows counted voice hours, messages, top 3 games
  by hours, and clips.
- `/leaderboard board:<voice|messages|gaming|clips|coins> period:<week|month|all>`: top 10,
  plus your own rank if you're outside the top 10.
  - For `coins`, week and month rank **coins earned** (the sum of positive ledger deltas in
    the window). All time ranks the current balance.
  - The `coins` board appears only once module 4 is loaded.

**Weekly MVP:** job key `mvp:<ISO week>`, scheduled Sundays at 18:00 local, window = the
7 days before that time. The bot posts in 💬・general the top member for each board, and
"MVP" for the best total rank (ties: most counted voice minutes). If module 4 is loaded, the
MVP gets 250 coins, ref `mvp:<week>:<user>`.

## Module 3: Clip of the week

- **Collecting:** a message in 📸・clips becomes a row in `clips(message_id PRIMARY KEY, user_id, url, posted_at)`
  if it has a video attachment, or a link from youtube.com/youtu.be, twitch.tv, medal.tv,
  streamable.com, outplayed.tv or kick.com. Deleting the message deletes the row.
- **Voting:** job key `clips:<ISO week>`, Sundays at 18:05 local, window = the 7 days before
  that time.
  - If there are 2+ clips, the bot sends **one** message in 📸・clips: the numbered list of
    clip links with a native poll attached. The poll is "Clip of the week?", has one answer
    per clip (`#1 · username`, cut to 55 characters), and lasts 24 hours.
  - If there are more than 10 clips, the 10 with the most reactions go in.
  - The poll's message id is stored in `clip_polls(week, message_id, ends_at, winner_id)`.
- **Winner:** a job checks polls whose `ends_at` has passed and that have no winner yet. It
  fetches the message and reads the final poll results.
  - The clip with the most votes wins, and ties go to the earliest post.
  - The winner gets the `Clip of the Week` role, and it's taken off last week's winner.
  - The bot announces the winner with a link to the clip. If module 4 is loaded, the winner
    gets 500 coins, ref `clipweek:<week>`.
  - With 0 votes, there's no winner and the role is left as it is.
- **Role setup:** `Clip of the Week` is added to `layout.ROLES` with no colour, following the
  layout's One Pen Rule, just below Squad. The README tells Lorenzo to drag Front Desk's
  managed role above Squad.
  - At startup and before each hand-out, the bot checks that the role exists and that
    `guild.me.top_role > clip_role`.
  - If either check fails, it says so in 🛡️・mod (at most once a day) and skips handing out
    the role. The announcement and coins still happen.
- **Poll timing:** the winner job also requires `message.poll.is_finalized()`. If the poll
  isn't finalized yet, it retries on the next 5-minute tick.
- **Accepted:** a deleted clip keeps its 25 coins. The 3-per-day cap is enough protection
  for a friend group.

## Module 4a: Economy core

**Earning:** amounts are constants in `logic/economy.py`. Daily caps are counted from
`ledger` by `reason` and local day.

| Action | Coins | Reason / ref |
|---|---|---|
| `/daily` | 100, plus 20 per day of streak, capped at +140 (day 8+). Missing a local day resets the streak | `daily` / `daily:<date>:<user>` |
| Voice | 2 per 5 counted minutes, paid on the 5-minute tick for whoever is currently counting | `voice` / `voice:<user>:<tick>` |
| Messages | 1 per message, max 50 per day | `message` |
| Posting a clip | 25, max 3 per day | `clip` / `clip:<message_id>` |
| Full LFG squad | 20 to each member, once per post | `lfg` / `lfg:<post>:<user>` |
| Weekly MVP / Clip of the week | 250 / 500 | see modules 2 and 3 |

**Commands:**
- `/balance [member]`
- `/give member amount`: no self-gifts and no bots. The amount must be at least 1.
- `/coinflip bet side`: the bet is 10 or more, at most min(balance, 5,000). A win pays 2×.

**Tables:**
- `wallets(user_id PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0), daily_streak, last_daily)`
- `ledger(id, user_id, delta, reason, ref UNIQUE NULL, at)`, append-only

## Module 4b: More games (build after 4a is in use)

The bet rules are the same as coinflip. **The bet is taken from the balance when the game
starts**, so the same coins can't be spent in two games at once.

- **`/slots bet`:** three reels with a paytable in `logic/games.py`, about 95% return to
  player. A test simulates 1,000,000 spins with a fixed seed.
- **`/blackjack bet`:**
  - Hit and Stand buttons. The dealer stands on 17, a blackjack pays 3:2 (rounded down),
    and there's no split or double.
  - The game state lives only in memory. A game left idle for 2 minutes is treated as Stand.
    If the bot restarts mid-game, a startup check refunds open bets from the
    `blackjack_open(user_id, bet, started_at)` table.
- **`/trivia [category]`:**
  - Fetches a question from Open Trivia DB with `type=multiple`. The text is cleaned with
    `html.unescape`, and answers are shuffled and cut to 80 characters for the buttons.
  - Answering is open for 20 seconds, and each person gets one click. The first correct
    click wins 50 coins.
  - If the API fails, the bot says "Trivia's down right now" and pays nothing.
- **Predictions:**
  - `/predict create question option_a option_b` posts the prediction with buttons.
  - **Betting:** others bet with Bet A or Bet B, which opens a small amount picker. The
    stake is taken right away. The creator can't bet on their own prediction.
  - **Ending it:** the creator or a Keeper locks the bets, then resolves with the winning
    option.
  - **Payout:** winners split the whole pool in proportion to their stakes, rounded down.
    The rounding remainder goes to the biggest winning stake, and ties go to the earliest bet.
  - **Refunds:** if nobody backed the winning side, everyone is refunded. Cancelling also
    refunds everyone.
  - **Tables:** `predictions(id, message_id, creator_id, question, option_a, option_b, status open|locked|resolved|cancelled, winner, created_at)`
    and `prediction_bets(prediction_id, user_id, option, amount, at, PRIMARY KEY(prediction_id, user_id))`.
    One bet per person; betting again adds to the same option, and switching sides isn't allowed.

## Running it

1. In the Developer Portal, Lorenzo turns on the three privileged intents for Front Desk.
2. `python bot/main.py --invite` prints the invite URL. Lorenzo opens it and authorizes the bot.
3. In Server Settings → Roles, Lorenzo drags Front Desk's role above Squad.
4. Lorenzo grants Administrator temporarily, as the existing scripts need it, then:
   - `python server/setup_server.py --apply` creates the `Clip of the Week` role from `ROLES`.
   - `python server/polish_server.py --apply` sets the 📸・clips override.
   - Then he removes Administrator again. The README spells this out.
5. `server/.venv/Scripts/pip install -r bot/requirements.txt`, then `bot/run_bot.bat`.
6. Optional: a Task Scheduler entry "At log on" runs `run_bot.bat`. The README documents it.

## Testing

- **Unit tests (pytest)** on `logic/`, with `now` passed in:
  - LFG roster transitions: join, leave, full, reopen, host rules, duplicate post.
  - Counted voice minutes: AFK, alone, overlapping sessions, recovery at heartbeat.
  - Clip link detection.
  - Poll winner and ties.
  - Job windows anchored to the scheduled time, including late catch-up.
  - Daily streak edges: local midnight and DST.
  - Daily caps.
  - Slots RTP, blackjack hand values (soft aces), and prediction payouts (rounding, remainder,
    no winners, cancel).
- **DB tests:**
  - Migrations apply to an empty database.
  - `transfer()` refuses overdrafts.
  - A duplicate `ref` doesn't pay twice.
  - Two transfers interleaved with `BEGIN IMMEDIATE` stay consistent.
- **Manual checklist per module** in the README, for example "start an LFG with 2 slots,
  join from a second account, confirm the full ping and the 20 coins".

## Open questions

None blocking. Coin amounts and the AFK, alone and short-session rules are constants, easy to
tune after a week of real use.
