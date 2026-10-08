# Economy audit

How Front Desk's coins come into the server (sources), how they leave it (sinks), and roughly how
fast an active member's balance grows. All numbers come from the code as of 2026-10-07. Live
numbers are in `/economy` (staff only): coins in circulation, the last 7 days minted vs burned,
and the biggest sources and sinks, read straight from the ledger.

Every coin moves through `bot/economy.py` (`apply`, `apply_tx`, `transfer`), so the ledger
`reason` column is the complete list of sources and sinks below.

## Sources

| Source (ledger reason) | Where | Amount | Limit | Alt-proof? |
| --- | --- | --- | --- | --- |
| `daily` | `logic/coins.py` | 100, +20 per streak day, 240 from day 8 | once per local day | no (capped) |
| `voice` | `logic/coins.py` | 2 per 5 min with 2+ undeafened humans | 120 a day (5 h) | no (capped) |
| `message` | `logic/coins.py` | 1 per message | 50 a day | no (capped) |
| `clip` | `logic/coins.py` | 25 per clip posted | 3 a day (75) | no (capped) |
| `lfg` | `logic/coins.py` | 20 when a squad fills | 60 a day | no (capped) |
| `trivia` | `logic/games.py` | 50 per correct answer | 250 a day | no (capped) |
| `word` (Daily Word) | `logic/wordgame.py` | 50 + 10 per unused guess (50 to 100) | once a day | yes |
| `challenge` | `logic/challenges.py` | 150 + 250 + 400, +300 bonus | 1,100 a week | yes |
| `quest` (starter quest) | `logic/quests.py` | 500 | once ever | yes |
| `mvp` | `logic/coins.py` | 250 | one member a week | n/a (one winner) |
| `clipweek` | `logic/coins.py` | 500 | one member a week | n/a |
| `season` | `logic/shop.py` | 1,000 / 500 / 250 | top 3 a month | n/a |
| `invitecontest` | `logic/recap.py` | 1,500 / 750 / 300 | top 3 a month | **no**: invitees only have to stay (`growth.stayed`), their account age is not checked |
| `tournament` | `logic/tournaments.py` | 1,000 first, 400 second | per tournament (one auto a month) | yes (sign-up age check) |
| `motm` | `logic/vibes.py` | 1,000 | one member a month | yes |
| `boost` | `logic/vibes.py` | 1,000 | once per boost | yes |
| `booststipend` | `logic/vibes.py` | 500 | per booster per month | yes |
| `birthday` | `cogs/engagement.py` | 250 | once a year | no |
| `raffle` (win) | `logic/shop.py` | 80% of the week's pot | one winner a week | buying needs an established account |

Zero-sum or near zero-sum (they move coins around more than they create or destroy them):

- `give`: member to member, net 0.
- `predict`: winners split the losers' stakes, net 0 (refunds when nobody backed the winner).
- `coinflip`: fair 50/50, expected net 0, but a big variance amplifier.
- `slots`: return to player is 95.4%, so about 4.6% of everything bet is burned.
- `blackjack`: dealer stands on soft 17, blackjack pays 3:2 rounded down; a small house edge.

"Alt-proof" means fresh accounts (younger than `quests.MIN_ACCOUNT_DAYS`, 30 days) are not paid.
The capped activity sources pay alts too, but an alt earns at most about 600 a day that way and
it takes real time in voice and chat; `/give` is the only way to move that to a main account.

## Sinks

| Sink | Price | Burned? | Limit |
| --- | --- | --- | --- |
| Name colour | 2,000 per 30 days | all of it | none |
| Hype | 500 per 24 h | all of it | none |
| Shoutout | 300 | all of it | once per 24 h per member |
| Gift Hype (new) | 500 per 24 h for a friend | all of it | none |
| Spotlight (new) | 1,500, pinned in general for 24 h | all of it | one at a time server-wide, once a week per member |
| Raffle tickets (new) | 100 each | 20% of the pot | 10 per member per week |
| Slots / blackjack house edge | a share of each bet | the edge | bet caps |

## Weekly inflow, an active member

An established member who claims `/daily` every day, spends about 2 hours a day in voice with
friends, chats a fair bit, plays the Daily Word most days and finishes most weekly challenges:

| Source | Typical week | Grinder's ceiling |
| --- | --- | --- |
| Daily (streak maxed) | 1,680 | 1,680 |
| Voice | 340 (2 h a day) | 840 |
| Messages | 210 (30 a day) | 350 |
| Clips | 50 | 525 |
| Squads | 60 | 420 |
| Trivia | 100 | 1,750 |
| Daily Word | 320 (4 wins) | 700 |
| Weekly challenges | 550 | 1,100 |
| **Total** | **about 3,300** | **about 7,350** |

On top of that come the lumpy prizes: a monthly winner (season, invite contest, member of the
month, a tournament) collects 1,000 to 1,500 at once, and a booster gets 500 a month.

## Is there enough to spend it on?

Before this change, the only sinks were the colour (2,000 a month, about 470 a week), Hype and
shoutouts. A typical active member keeping a colour and buying a shoutout or two a week spends
around 1,000 to 1,200 a week, so their balance still grew by about 2,000 a week, and a grinder's
by over 6,000. Balances pile up, `/richest` drifts upward, and the shop stops feeling like a goal.

The new sinks:

- **Raffle**: up to 1,000 a week per member. It mostly moves coins from many players to one, but
  20% of every pot is burned; at 20 members buying 5 tickets each, that's 2,000 burned a week. It
  is a fun reason to *spend*, and a 30-day-old account is needed to buy, so alts can't enter.
- **Spotlight**: 1,500 for a 24-hour pinned message in general. One slot for the whole server
  keeps general from filling up with pins, so it is scarce (at most 10,500 a week server-wide)
  and stays special.
- **Gift Hype**: 500, the same as Hype but for a friend, with a DM telling them who sent it. It
  gives members with big balances a social way to spend.

Left out on purpose: buying XP or levels (levels should mean activity), renaming channels
(moderation risk) and premium temp-voice renames (`/squad name` is already free).

## What to watch in `/economy`

- **Net per week vs. supply**: a supply that grows more than 10% a week is inflating; consider
  raising sink prices or trimming a payout (the trivia cap and voice cap are the easiest knobs).
- **Top sources**: `daily` should lead. If `coinflip` or `slots` shows up as a *source*, the
  house lost that week (variance); if it stays a source for weeks, check the odds.
- **Top 10 share**: when the 10 biggest wallets hold most of the coins, the raffle and the
  Spotlight are the sinks those members will actually use.
- **Season points never count spending or winnings** (`shop.SEASON_REASONS`), so raffle wins
  and gifts don't affect the season.

## Known gaps

- Closed: the monthly invite contest used to count any invitee who stayed 3 days; invitees
  must now have an established account (`quests.established`) when they join.
