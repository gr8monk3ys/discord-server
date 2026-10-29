# Front Desk privacy policy

Front Desk is the bot for one Discord server, Lorenzo's Server. It isn't offered to other
servers. This page says what it stores, why, and how to get it deleted.

_Last updated: 2026-10-29_

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
| When you bump the server on Disboard | `/bumpers`, `/vote` and the weekly bump thanks |
| How many `/catchup` summaries you asked for today (a count and token totals, never the text) | The daily limits |
| Which house you are in, when you joined it and when you left; each closed week's house scores and which members of the winning house were paid | Houses, the House Cup, the 30-day wait after leaving |
| When you arrived (for the Welcome Crew ping), and which crew member greeted which newcomer and when | The crew ping, the greeting reward, `/crew` |
| That the Welcomers were pinged about you and when (kept 30 days) | Pinging them once per newcomer |
| Whether you hold the Supporter role, since when, and when it ended | The thank-you, the `/profile` flair, the monthly stipend |
| Guide and setup posts you make: the post's ID, its title and when you posted it (not the post's text) | The one-time post reward |
| Watch parties you schedule (the event, you as host, the start time) | Reminders and the one-event-per-host limit |
| Clips shortlisted for the server's YouTube: the message ID, your answer, who gave it and when, the name you asked to be credited as, and the video's YouTube ID once posted | Clipcast, proof of consent, taking a clip down |
| A Minecraft name you link with `/smp link`, and how long you were online on the Minecraft server each day | Passing your in-game chat to the Monkey SMP channel, playtime coins, `/smp top` |
| When you last shared a Wrapped card, and your level on January 1 | The share cooldown, "levels gained" on next year's card |
| When you were banned; a ban appeal you send (your user ID, your answers, the decision, who made it and their reason) | The waiting period before an appeal, staff review |

Front Desk does **not** store the text of your messages. The only exceptions are a message you
report (a short quote goes to the moderators), a message copied to the hall of fame, the title
of a guide or setup post, and the answers on a ban appeal.

## Where it's kept

In a database on the server owner's own computer. It isn't sold or shared with anyone. Nothing
about you is sent anywhere except Discord itself, apart from the cases this page names: a clip
you agreed to have posted (YouTube), a Minecraft name and chat in the Minecraft channel (the
server's own Minecraft server), server-wide totals with no names (the public website), and the
two website features, ban appeals and Linked Roles (Vercel, which hosts the site). If you link a creator channel,
Front Desk reads that channel's public page, feed or stream status from YouTube or Twitch;
it sends them only the channel's ID or handle, nothing about you. If you link a Steam account, Front Desk reads that
account's public Steam profile.

**AI summaries.** When a member runs `/catchup`, recent messages from that one public channel
are summarised for that member only. Messages from bots and from members who turned tracking
off (`/privacy tracking:off`) are left out, and Front Desk does not keep the messages or the
summary. The summary is written either by a model running on the server owner's own computer
(nothing leaves it) or, if the owner has set that up instead, by Anthropic's API, in which
case those messages are sent to Anthropic. The summary's footer says which one wrote it.

**Houses.** Front Desk sorts members into one of three houses and keeps which house you are in.
A house's weekly score is worked out from the records above (chat days, voice time, squads and
so on); nothing new is counted for it. Standings name houses, never members. With tracking off
you are not sorted into a house and your activity does not score. If you were already in one,
you stay in it and keep its role until you run `/house leave`; your house and the date you left
are kept after that, because the 30-day wait before rejoining depends on them.

**Welcome Crew.** When you arrive, Front Desk notes the time. The crew is pinged once, and only
for a first join, not when someone leaves and comes back. If you answer a crew member within 48 hours, it records who greeted whom and
when. It does not read or keep what was said. Nothing is recorded when either of you has
tracking off. If either of you turns it off later, the greeting records are deleted at once.
The note of when you arrived (your ID and times, nothing else) is kept, because it is how
Front Desk tells a first join from someone leaving and coming back; it no longer leads to a ping.

**Welcomers.** In your first 14 days, the first time you join a public voice channel Front Desk
may post one line in general that pings members who opted into the Welcomer role, mentions you
(without pinging you) and names the channel. It decides "first time" from the voice time it
already keeps; nothing new is recorded about your voice use. If it posts, it notes your ID and
the time so it happens only once, and deletes that note after 30 days. With tracking off you are
skipped: no line is posted about you.

**Supporter tier.** Front Desk has no payment code and never sees a payment, an email address
or an amount. Ko-fi or Discord gives you the Supporter role and Front Desk only notices the
role. It keeps the date you got it and the date it ended. `/privacy` does not change this.

**Guides and setups.** A day after you post, Front Desk reads the post once to check it is more
than a line or two, and does not keep the text. With tracking off your posts are not recorded.
If you turn tracking off later, the records of all your posts are deleted at once. The posts
themselves are yours and stay in the forum.

**Clipcast (YouTube).** A clip of yours leaves Discord only after you press yes for that clip,
or if you set `/clipcast consent always` yourself. Then the video file, a title made from your
caption and a description go to the server's YouTube channel. The description has your display
name if you chose to be credited, or no name if you chose anonymous. Front Desk keeps your
answer, who gave it and when. Deleting the clip, leaving, a ban or `/privacy tracking:off`
withdraws your yes for anything not posted yet, at once, and a standing "always" goes back to
"ask" within a few minutes. Setting `/clipcast consent ask` yourself turns clips your standing
yes had already approved, and that are not posted yet, back into a question. **A clip that is
already on YouTube stays there until you run `/clipcast remove`**; turning tracking off does not
take it down. `/clipcast remove` deletes the video through YouTube's API when it can, and
otherwise tells the owner to remove it by hand. The record of the clip and your answer is kept
either way, because it is the proof of what you agreed to.

**Monkey SMP (Minecraft).** The Minecraft server belongs to the server owner and runs on the
owner's own computer or home network. `/smp link` keeps the Minecraft name you give with your Discord ID; it is
not sent to the Minecraft server, and Front Desk never changes who may join it (that is a pass staff grant).
`/smp unlink`, leaving or a ban deletes the link. What you type in the Monkey SMP channel is
shown to players in the game with your display name, whatever your `/privacy` setting, and what
linked players say in the game is posted in that channel; Front Desk keeps neither. In-game chat is
passed on only from a player whose name is linked to a member who can post in that channel right now, so
nothing comes through from someone who is not linked, is timed out, or has left or been banned. While you are
linked, Front Desk counts how long you are online there each day while at least one other
player is on too. With tracking off that time is not counted, and what was counted is deleted
at once. Your linked name stays until you unlink it.

**Wrapped.** Your year-in-review card is drawn from the records above when you ask for it and
is shown only to you unless you press Share, which works only in a channel you can post in
yourself. Nothing about your year is stored for it. A card can name up to three members you
played with most; members who have tracking off or have left are never named. With tracking
off you get no card and your level is removed from the January snapshot at once. The time you
last shared a card is deleted too, unless you shared within the last 6 hours: then it is kept,
so that turning tracking off and on cannot skip the wait between shares.

**The website's live numbers.** Every 30 minutes Front Desk writes a small file of server-wide
totals (members, messages and voice hours this week, most played games, upcoming events, house
scores) and the owner's computer publishes it to a public branch of this repository, where the
landing page reads it. It holds no names, avatars or IDs. A number that describes fewer than
three members is left out, the rest are rounded, and members with tracking off are left out of
every total that comes from a per-member record. House scores appear only when every house
has at least three members who scored that week. Event titles are only Front Desk's own
wording ("Overwatch game night", "Watch party", "Town Hall", "Overwatch tournament"): what a
host typed, a tournament's name or the title of an event made by hand is never published. The
branch keeps only the latest file.

**The voice line in general.** While two or more people are in a public voice channel, Front
Desk keeps one line in the general channel that says how many are there, which channel, and
the game most of them have in common if there is one. Nobody is named, and nothing about
members is stored for it. Members with tracking off are counted as people in the room, but
Front Desk does not read which game they are playing. When the room empties the line changes
to how long the session ran and how many people it reached, and stays in the channel.

**The weekly games-list check.** Each Monday the server owner's digest, a private message to
the owner only, compares the last 28 days of game time with the server's games list. It shows
game names with a player count and hours, never who played. A game that fewer than three
members played is never named, and members with tracking off are not counted.

**The weekly newcomers check.** The same digest has a "Newcomers" field. For the last week and
the last 28 days it says how many people joined, how many of them have left since, and how many
picked roles, said something or joined voice; for the people who joined the week before, how
many are still here and how many came back. It is counts only, never a name or an ID, and it
is worked out from the records above: nothing new is stored. Members with tracking off are
counted as joined and left, which the server shows everyone anyway, and in nothing else. The
field does say how many recent joiners have tracking off, so when only one person joined, the
owner can tell that this person turned it off.

**Questions nobody answers.** Before it posts the next question of the day or daily poll, Front
Desk re-reads its own last one (votes, reactions, how many messages its answers thread has)
and, if those show nothing, checks whether a member replied or wrote in the channel after it;
it keeps only whether that question was answered, nothing about who answered or what was said.

**Bans and appeals.** A ban is stored as a moderation case, with the time. If appeals are on,
Front Desk DMs you an appeal code before the ban. The appeal form is on the website, which
Vercel hosts: your user ID, the code and your answers go to a small function there, which
keeps and logs none of them and passes them to a private staff channel on Discord. Vercel
records requests, including IP addresses, in its usual server logs. Front Desk
then stores the appeal. Your answers are blanked 180 days after the decision; the rest (that
you appealed, the decision, who made it and their reason) is kept. The form sets no cookie.
`/privacy` does not apply to bans or appeals.

**Linked Roles.** This is Discord's own feature and it runs on the website, not in the bot. If
you press Link, Discord asks you to approve it, the site reads which platforms on your Discord
account are verified (not your names on them), tells Discord yes or no per platform, and keeps
nothing. It sets one cookie for at most 10 minutes during sign-in. To undo it: User Settings >
Authorized Apps > Front Desk > Deauthorize. Details: `docs/LINKED_ROLES.md`.

## Your choices

- `/privacy tracking:off` stops voice, message and game tracking and **deletes** your
  voice, message and game history and your XP right away. `/privacy tracking:on` starts it again.
- Turning tracking off also acts on the newer features, in the same moment: your Minecraft
  playtime, the records of your guide and setup posts and your Welcome Crew greetings are
  deleted, your level is removed from the Wrapped snapshot, and your yes is withdrawn for any
  clip not yet posted.
- It removes the detail, never a limit. Coins you were paid stay in the coin ledger, which is
  not deleted, and daily and weekly caps are counted from it, so turning tracking off and on
  again does not let anyone earn a reward twice. For the same reason three bare records are kept:
  when you first arrived (so a rejoin is not treated as a first join), for up to 6 hours,
  when you last shared a Wrapped card, and, for up to 30 days, the note that the Welcomers were
  pinged about you.
- Not touched by it: your house, a linked Minecraft name, Supporter dates, clipcast answers, a
  clip already on YouTube, bans and appeals. Use the command named in each paragraph above
  (`/house leave`, `/smp unlink`, `/clipcast remove`), or ask.
- To have everything about you deleted (including coins and invite records), message a
  Keeper or Moderator on the server and it'll be removed.
- Leaving the server stops all tracking.

## Contact

Ask in the server, or open an issue at <https://github.com/gr8monk3ys/discord-server/issues>.
