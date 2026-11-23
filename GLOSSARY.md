# Chill Gamers Discord Server

A Discord server managed as code (`server/`), its own bot Front Desk (`bot/`), a landing page (`site/`) and a client theme (`theme/`). One guild, about ten active people. Terms below are read from `server/layout.py`, `bot/` and `docs/`.

## Language

**Layout**:
`server/layout.py`: the one source of truth for games, roles, categories and channels; the setup scripts and the bot both import it.
_Avoid_: Config, schema

**Front Desk**:
The server's own discord.py bot, one process for one guild with one SQLite file.
_Avoid_: The bot (when naming this one), Helper

**Cog**:
A Front Desk module that does Discord I/O for one feature; its rules live in the **Logic** module of the same name.
_Avoid_: Plugin, extension

**Logic**:
A pure rules module in `bot/logic/` (no Discord, no database, no clock of its own); the cog passes `now` and rows in.
_Avoid_: Service, helper

**Parked module**:
A cog listed in `PARKED` in `bot/main.py`: not loaded, but its tables and data stay.
_Avoid_: Disabled, removed

**Dry run**:
The default of every `server/` script: it prints a plan and changes Discord only with `--apply`.
_Avoid_: Preview mode

## Server structure

**Keeper**:
The owner-level staff role, the only administrator role besides Discord's own owner.
_Avoid_: Admin (the role's old name)

**Moderator**:
The staff role below Keeper; with Keeper it makes a member staff (`cogkit.is_staff`).
_Avoid_: Mod (as a role name)

**Squad**:
The role every member holds after Onboarding (formerly "Member"); also the name of the `/squad` temporary voice channels and of a group formed through **LFG**.
_Avoid_: Member role

**Game**:
A listed game with a ping role, an Onboarding answer and a forum tag; only those in `GAME_CHANNELS` get a channel of their own.
_Avoid_: Title

**Retired game**:
A game moved from `GAMES` to `RETIRED_GAMES`: no longer offered, its role and channel are kept, and the channel moves to the **Archive**.
_Avoid_: Removed game, deleted game

**Archive**:
The staff-only `99 · archive` category that holds retired game channels and `RETIRED_CHANNELS`; nothing in it is deleted.
_Avoid_: Trash

**Onboarding**:
Discord's Community Onboarding: default channels plus the "What do you play?" question that hands out game roles.
_Avoid_: Welcome screen

**Focus mode**:
`server/focus_mode.py`: cuts Onboarding's default channels to the core list and adds one opt-in question; reversible with `--revert`.

**Snapshot**:
The read-only config dump written to `server/snapshot/*.json` by `snapshot_server.py`.
_Avoid_: Backup (the bot's `ops` cog makes the database backups)

## Community

**LFG**:
A squad-up post (`lfg_posts`) in the lfg forum: a host, a game, a size of 2 to 10, joiners, and an expiry of 3 hours.
_Avoid_: Party, lobby

**Welcome Crew**:
Trusted regulars (established account, 14 days on the server, no recent case) who say hi to newcomers and earn a greeting reward.
_Avoid_: Greeters

**House**:
One of three teams (Fern, Kestrel, Otter) every member belongs to; houses compete in a weekly **House Cup** and a quarterly trophy.
_Avoid_: Faction, team

**Case**:
A moderation record (`cases`) written by `/warn`, `/timeout` and `/ban`.
_Avoid_: Strike, ticket

**Appeal**:
A ban appeal submitted on the website with an **Appeal code**, reviewed by staff on a card with Accept and Deny.
_Avoid_: Unban request

**Appeal code**:
`base32(HMAC-SHA256(APPEAL_SECRET, user_id))[:16]`, sent in the ban DM; the site and the bot both check it.

**Quiet rooms**:
The rule that stops a recurring prompt (a **kind**) after 3 ignored posts and wakes it when someone speaks or after 7 days.

**Linked Roles**:
Discord Linked Roles metadata registered by `server/register_linked_roles.py` and served by the site.

**Faction**:
A Monkey SMP team registered by a forum post with up to four Minecraft names and a charter, approved by staff.
_Avoid_: House

**Base of the Month**:
The Monkey SMP award, once a month, to one base's builders; a role moves to them.

**Field notes**:
The owner's weekly post of what they built, heard, watched and played; the owner's own activity only.

## Economy

**Coin**:
Front Desk's currency, held in a wallet and moved only through `bot/economy.py`.
_Avoid_: Points, credits

**Wallet**:
A member's balance row (`wallets`), which also holds the `/daily` streak.

**Ledger**:
The append-only record (`ledger`) of every coin movement; its `reason` column is the complete list of sources and sinks.
_Avoid_: Transaction log

**Ref**:
The unique key on a ledger row that makes a payout idempotent; paying the same ref twice returns `Status.DUPLICATE`.
_Avoid_: Idempotency key

**Source / Sink**:
A ledger reason that mints coins or burns them (`docs/ECONOMY.md`).

**Established**:
An account at least `MIN_ACCOUNT_DAYS` (30) old at the time, the one age rule that keeps fresh alts from being paid.
_Avoid_: Verified, trusted

**Perk**:
A shop item with a duration: name colour, Hype, Shoutout, Spotlight, gift.

**Season**:
A calendar month in the shop: the top three earners are paid at month-end. Not to be mixed with a **Seasonal event** or a House quarter.

**Seasonal event**:
A yearly event (Halloween, Winter Fest) read from `assets/events.json`; one occurrence is a **Run** with id `KEY:YEAR`.

**Supporter**:
A paying-tier member (Tebex) with a profile flair and a monthly stipend.

**Starter quest**:
Five first steps for a new member with a one-time coin reward.
