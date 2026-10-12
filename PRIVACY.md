# Front Desk privacy policy

Front Desk is the bot for one Discord server, Lorenzo's Server. It isn't offered to other
servers. This page says what it stores, why, and how to get it deleted.

_Last updated: 2026-10-11_

## What Front Desk stores

| Data | Why |
|---|---|
| Your Discord user ID | To tie the records below to you |
| Voice time: which voice channel and when you joined/left | `/stats`, leaderboards, weekly MVP, voice coins |
| How many messages you sent per day (**a count only, never the text**) | `/stats`, leaderboards, message coins |
| Which game Discord shows you playing, and for how long | Top games in `/stats` |
| Which invite you joined with, and whether you left | `/invites`, the Recruiter role |
| Links to clips you post in the clips channel | Clip of the week |
| Stars on messages that reach the hall of fame | The hall of fame |
| Coins, purchases and game results | The coin economy |
| Squad-up posts, game nights, reports you file | Those features |
| XP and level (from the message and voice counts above) | Levels, `/rank`, level roles |
| Badges you've earned, tournament entries and results | Achievements, `/profile`, tournaments |
| Starter-quest steps you've done | `/quest` and its one-time reward |
| YouTube/Twitch channels you choose to link with `/creator` | Announcing your uploads and streams |
| Partner applications you submit | Staff review and the partners channel |
| Your Daily Word guesses and results | The game, streaks and its leaderboard |
| Weekly challenges you've claimed | Paying each reward once |
| Seasonal event drops you grabbed and daily event quests you finished | Event points, the `/event` leaderboard, paying each reward once, the event badge |
| A Steam account you choose to link with `/link` (SteamID, profile name, recently played games and hours, as shown on your public Steam profile) | Showing it on your `/profile` and `/accounts`; `/unlink` deletes it |
| When you bump the server on Disboard | `/bumpers`, `/vote` and the weekly Supporter thanks |
| How many `/catchup` summaries you asked for today (a count and token totals, never the text) | The daily limits |

Front Desk does **not** store the text of your messages. The only exceptions are a message you
report (a short quote goes to the moderators) and a message copied to the hall of fame.

## Where it's kept

In a database on the server owner's own computer. It isn't sold or shared with anyone, and
nothing about you is sent anywhere except Discord itself. If you link a creator channel,
Front Desk reads that channel's public page, feed or stream status from YouTube or Twitch;
it sends them only the channel's ID or handle, nothing about you. If you link a Steam account, Front Desk reads that
account's public Steam profile.

**AI summaries.** When a member runs `/catchup`, recent messages from that one public channel
are sent to Anthropic to write the summary, which only that member sees. Messages from bots and
from members who turned tracking off (`/privacy tracking:off`) are left out, and Front Desk does
not keep the messages or the summary. If no API key is configured, nothing is sent.

## Your choices

- `/privacy tracking:off` stops voice, message and game tracking and **deletes** your
  voice, message and game history and your XP right away. `/privacy tracking:on` starts it again.
- To have everything about you deleted (including coins and invite records), message a
  Keeper or Moderator on the server and it'll be removed.
- Leaving the server stops all tracking.

## Contact

Ask in the server, or open an issue at <https://github.com/gr8monk3ys/discord-server/issues>.
