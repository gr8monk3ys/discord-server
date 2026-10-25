# Wave 9 setup

One checklist for turning on the features added on 2026-10-12. Work from the top: the first
steps cost nothing and need no secrets. Every feature is separate. Skip any of them and the rest
still work; a feature that is not set up says so when someone uses it.

Words used below:

- **Deploy** means: pull `main` on the bot's PC and restart the bot
  (`Stop-ScheduledTask 'Front Desk bot'; Start-ScheduledTask 'Front Desk bot'`). The database
  updates itself and the new commands appear in Discord within a minute.
- **`server/.env`** is the bot's settings file on the bot's PC. Restart the bot after editing it.
- **Vercel env vars** are in the Vercel project: Settings > Environment Variables (Production).
  Redeploy the site after changing them. They are for the website's functions only.

Never put a secret in the repo. `server/.env` is gitignored; Vercel keeps its own.

## A. On after deploy

Nothing to do for these.

| Feature | How to see it working |
|---|---|
| Houses | Within 10 minutes members are sorted in and one "Houses are here" post appears in 📣・announcements. `/house standings` shows three houses. The House Cup result is posted there on Mondays at 10:10 |
| Wrapped | `/wrapped-preview` (staff) shows your card now. Its Share button only works in a channel you can post in. Members get `/wrapped` from December 1 to January 15 |
| Live site data, bot half | `/sitedata refresh` answers "Site data rebuilt" and lists the sections. The file is `bot\data\site\stats.json`. Nothing is public until step C.4 |
| `/watchparty` | Run it. It makes a server event. Until step B.3 it uses the 🔊 Lobby voice channel |

### A.1 One command: create the new roles and channels

Most of the other features wait for a role or a channel that does not exist yet. This creates
them. It only adds things and never moves or deletes anything.

    cd server
    .venv\Scripts\python public_mode.py            # dry run: read what it would create
    .venv\Scripts\python public_mode.py --apply

It creates the roles Welcome Crew, Supporter, House Fern, House Kestrel and House Otter, and the
channels 💜・supporters, 📚・guides, 🖥・setups, ⛏️・monkey-smp, 🎙・stage and 🔒・appeals. Afterwards run
`.venv\Scripts\python snapshot_server.py` and commit `server/snapshot/`, so the weekly drift
check stays quiet. Until you run it, the permission self-check lists the three house roles and
the other missing roles and channels in 📋・mod-log; that note is expected and clears afterwards.
🎙・stage is left out of the Onboarding default channels on purpose; it shows itself to everyone
while an event is live.

That turns these on:

| Feature | How to see it working |
|---|---|
| House roles | Within 10 minutes members carry their house's role |
| Welcome Crew | `/crew` shows the roster and a Join button. `/crew-stats` (staff) shows pings and greetings. The crew is pinged only for first-time joins, so an account that leaves and rejoins is not a test. Optional: `CREW_PING_MINUTES` and `CREW_QUIET_HOURS` in `server/.env` change how often and when the crew is pinged (defaults: every 30 minutes at most, never from 22:00 to 09:00) |
| Forums | Post in 📚・guides. Front Desk reacts with 👍 at once. A post that is still up a day later, and is more than a line or two, pays 30 coins |
| Supporter tier, by hand | `/supporter grant` on yourself: a thank-you appears in 💬・general and `/profile` shows the flair. `/support` says supporting "isn't open yet" until step D.2 |

Check 📋・mod-log a minute after the next restart. The permission self-check lists anything the
bot still cannot do, and says so again when it is fixed. `/status` shows the same count.

## B. Discord settings only the owner can change

Front Desk cannot give itself permissions or move roles above itself. These are all in Server
Settings > Roles.

1. **Ban Members** on the Front Desk role. Needed for `/ban` and for ban appeals (step C.5).
   Check: the mod-log note about a missing Ban Members permission clears, and `/ban` works on a
   test account.
2. **Mute Members** on the Front Desk role. Needed for Stage events.
3. **The stage channel.** In 🎙・stage, Front Desk needs View Channel, Manage Channels, Mute
   Members and Move Members. If the role has them server-wide, nothing more is needed.
   Check: `/watchparty` now puts the event in 🎙・stage, not the Lobby. The Town Hall then appears
   as a server event one week before the first Saturday of each month, with one post in
   📣・announcements. To change its time put `TOWNHALL_TIME=7pm` (or `off`) in `server/.env`.
   A watch party's reminder pings the Game Night role only when staff host it; a member's
   watch party reminds the people who tapped Interested and nobody else.
4. **Role order.** Welcome Crew and the three House roles must sit below the Front Desk role.
   `public_mode.py` creates them at the bottom, so this is only a problem if you drag them up.
   The Supporter role may sit anywhere.
5. **Linked Roles roles.** These are made by hand in Server Settings, after the website part is
   set up. Do all of `docs/LINKED_ROLES.md`; its step 4 is this one.

## C. Secrets, per feature

Each block is one feature. The table says exactly which name goes where.

### C.1 Supporter link (no secret)

| Name | Where | Value |
|---|---|---|
| `SUPPORT_URL` | `server/.env` | your Ko-fi page, for example `https://ko-fi.com/yourname`. Only https links on ko-fi.com, patreon.com or discord.com are accepted |

Check: `/support` shows a link button.

### C.2 Clipcast (YouTube)

Without these, clipcast still asks authors for consent on Sundays and lists the approved clips in
📋・mod-log on Tuesdays for you to post by hand. With them, Front Desk uploads the clips itself.
Get the values in step D.1.

| Name | Where | Value |
|---|---|---|
| `YOUTUBE_CLIENT_ID` | `server/.env` | from Google Cloud |
| `YOUTUBE_CLIENT_SECRET` | `server/.env` | from Google Cloud |
| `YOUTUBE_REFRESH_TOKEN` | `server/.env` | for the YouTube channel's Google account. Treat it like a password |
| `YOUTUBE_PRIVACY` | `server/.env` | optional: `unlisted` (the default, so you can review first), `private` or `public` |

Check: `/clipcast status` (staff) says "YouTube: connected" and names the privacy level. If a
setting is missing it names which one. The first upload is on a Tuesday at 18:30, and only if an
author said yes.

### C.3 Monkey SMP (Minecraft bridge)

Four parts. Each turns on by its own lines; leave a line empty to leave that part off. Do step E
on the Minecraft server first.

| Name | Where | Value | Turns on |
|---|---|---|---|
| `REALMS_ADDRESS` | `server/.env` | the Minecraft server as the bot's PC sees it, for example `127.0.0.1:25565` | the status panel |
| `REALMS_RCON_PASSWORD` | `server/.env` | the `rcon.password` from step E | `/smp link` (whitelist) and Discord-to-Minecraft chat |
| `REALMS_RCON_HOST` | `server/.env` | optional; empty means `127.0.0.1`. Only this machine or a private-network address is accepted | |
| `REALMS_RCON_PORT` | `server/.env` | optional; empty means `25575` | |
| `REALMS_LOG_PATH` | `server/.env` | the full path of the Minecraft server's `logs\latest.log`. The bot only reads it | Minecraft-to-Discord chat |

Check: a "Monkey SMP" panel appears in ⛏️・monkey-smp and is pinned. `/status` and `/smp status`
(as staff) have a line "Monkey SMP bridge: ok (status, whitelist, chat to Minecraft, chat from
Minecraft)" listing the parts that are on, or saying what is wrong ("RCON refused the password",
"log file not found"). Then type in ⛏️・monkey-smp and see it in the game, and type in the game and
see it in the channel.

Things to know when testing the whitelist:

- `/smp link` works for accounts 30+ days old that have been in the server 14 days.
- A name that is already on the whitelist is refused ("ask a mod"), because the bot did not put
  it there. That includes your own name from step E and anyone you whitelisted by hand. Link
  those with `/smp adopt <member> <name>` (mods). An adopted name is then handled like any
  other link.
- The bot only ever removes names it added or you adopted. Names you whitelisted by hand and
  never adopted stay on the whitelist whatever happens in Discord.
- Playtime and its coins count only while at least two players are online, so nothing shows
  while you test alone.
- Chat in the game reaches the channel only from players whose name is linked to a member who
  can post there right now. Players you whitelisted by hand stay silent in Discord until a mod
  runs `/smp adopt` for them (`/smp link` refuses a name that is already whitelisted).
  Join and leave lines show for everyone.

### C.4 Live site data, public half

No secret in a file. The bot's PC needs git that can push to the repo without asking for a
password.

1. Dry run: `cd server`, then `.venv\Scripts\python publish_site_data.py`. It says what it would
   push.
2. Push once by hand: `.venv\Scripts\python publish_site_data.py --apply`. The first run creates
   the `site-data` branch.
3. Create a Windows scheduled task that repeats every 30 minutes. Program:
   `server\.venv\Scripts\pythonw.exe`. Arguments: `publish_site_data.py --apply --quiet`. Start
   in: the repo's `server` folder.

Check: <https://discord.lscaturchio.xyz/> shows the live section (upcoming events, top games,
weekly totals) between "This week" and "Games". If it does not, run the dry run again; it says
why it has nothing to push.

To turn the whole thing off: `SITE_DATA=off` in `server/.env` stops the bot writing the file, and
the page hides the section by itself once the published data is 48 hours old.

Never commit to the `site-data` branch by hand. The script replaces it every time.

### C.5 Ban appeals

Needs step B.1 (Ban Members) and the 🔒・appeals channel from A.1.

1. Make the secret, once: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. Make a webhook: 🔒・appeals > Edit Channel > Integrations > Webhooks > New Webhook > Copy
   Webhook URL. Anyone with this URL can post in that channel, so treat it like a password.

| Name | Where | Value |
|---|---|---|
| `APPEAL_SECRET` | `server/.env` **and** Vercel env vars | the secret from 1. The two must be identical |
| `APPEAL_WEBHOOK_URL` | Vercel env vars | the webhook URL from 2 |
| `APPEAL_COOLING_DAYS` | `server/.env` | optional: days after a ban before an appeal is read (default 7) |
| `APPEAL_REAPPEAL_DAYS` | `server/.env` | optional: days after a denial before the next appeal (default 90) |
| `APPEAL_ORIGIN` | Vercel env vars | optional: only if the form is served from another address than `https://discord.lscaturchio.xyz` |
| `SITE_URL` | `server/.env` | optional: only if the site moves; it is the address the ban DM links to |

Then restart the bot and redeploy the site.

Check, cheapest first:

- `/appeals list` shows a list (empty is fine) and no longer says appeals aren't set up.
- `/appeals code` with any user ID shows a code.
- Full test with a spare account: set `APPEAL_COOLING_DAYS=0` for the test, `/ban` the spare
  account, and look for the appeal code in its DMs. Open <https://discord.lscaturchio.xyz/appeal>,
  fill it in. A review card with Accept and Deny appears in 🔒・appeals within a few seconds.
  Press Accept and the account is unbanned. Remove `APPEAL_COOLING_DAYS=0` afterwards.

If the form answers "Appeals aren't open on this site right now", a Vercel variable is missing or
the site was not redeployed. If a mod-log note says a message "failed verification", the two
`APPEAL_SECRET` values differ.

Optional: add a rate limit in front of the form. In the Vercel project's Firewall settings, add
a custom rule that rate-limits requests to the path `/api/appeal` per IP address (a few requests
a minute is plenty; a real appeal is one request). The function has no rate limit of its own.
The bot already drops repeats and writes at most one mod-log note per member a day, so this only
saves function calls and webhook traffic. The menu names are Vercel's and were not checked here.

### C.6 Linked Roles

Four Vercel env vars (`DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `LINKED_ROLE_STATE_SECRET`,
`LINKED_ROLE_REDIRECT_URI`) and nothing in `server/.env`. The steps, in the order they must be
done, and the tests are in `docs/LINKED_ROLES.md`.

## D. Outside accounts you create

### D.1 Google Cloud and YouTube (for C.2)

1. Have a YouTube channel for the server, on a Google account you control.
2. At <https://console.cloud.google.com/>: create a project, enable **YouTube Data API v3**, and
   set up the OAuth consent screen.
3. APIs & Services > Credentials > Create credentials > OAuth client ID > type **Desktop app**.
   That gives `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`.
4. Get a refresh token by signing in once as the channel's Google account with that client and
   the scope `https://www.googleapis.com/auth/youtube.upload`. Add
   `https://www.googleapis.com/auth/youtube.force-ssl` if `/clipcast remove` should be able to
   delete a video itself; without it the bot tells you to delete it by hand. The repo has no
   helper for this step; any standard "installed app" OAuth sign-in for Google works.

Two Google rules to know about, both read from Google's own pages on 2026-10-12 (not tested
with a real upload):

- A consent screen left in "Testing" with an external user type hands out refresh tokens that
  expire after 7 days. Set the publishing status to "In production" before you take the token.
- Uploads from an unverified API project are not forced to private, so `YOUTUBE_PRIVACY`
  decides what members see. `videos.insert` is limited to 100 calls a day; Front Desk posts at
  most three clips a week.

### D.2 Ko-fi, or Discord Server Subscriptions (for the Supporter role)

Front Desk takes no payments. It only reacts to the Supporter role, so something else has to
hand that role out:

- **Ko-fi:** create a Ko-fi page, connect its Discord integration to this server, and choose
  **Supporter** as the role supporters get. Ko-fi's own bot role must sit above Supporter in the
  role list to be able to give it. Put the page in `SUPPORT_URL` (C.1).
- **Discord Server Subscriptions:** set up a tier in Server Settings and let it grant the
  Supporter role.
- **Neither:** `/supporter grant` and `/supporter revoke` (staff) do it by hand.

Check: when the role lands on a member, a thank-you appears in 💬・general and a line in
📋・mod-log. Members who already held the role the first time the bot saw it are recorded
without a thank-you.

### D.3 Discord Developer Portal (for C.6)

Only the redirect URI and the client secret. See `docs/LINKED_ROLES.md`, step 1.

## E. Minecraft `server.properties` (for C.3)

On the Minecraft server, set these lines and restart it:

    enable-status=true
    enable-rcon=true
    rcon.port=25575
    rcon.password=PUT-A-LONG-RANDOM-PASSWORD-HERE
    white-list=true

- `enable-status` is what the status panel asks. It is on by default.
- `rcon.password` is the value for `REALMS_RCON_PASSWORD`. Use a long random one.
- `white-list=true` makes the whitelist count. Without it `/smp link` still adds names, but
  anyone can join anyway. Add yourself first (`whitelist add YourName` in the server console) so
  you are not locked out. Names added this way are not the bot's: it will not link them through
  `/smp link` and will never remove them. Use `/smp adopt` to hand one over to the bot.
- Optional: `broadcast-rcon-to-ops=false` stops operators in the game seeing a line for every
  command the bot sends.
- Do not forward the RCON port (25575) on your router. The bot only talks to it on this machine
  or your home network, and refuses any other address.

Front Desk never starts, stops or reconfigures the Minecraft server. It sends three kinds of
command over RCON: `list`, `whitelist add/remove NAME` and `tellraw` for chat.

## If something stays off

- 📋・mod-log: the permission self-check names missing channels, permissions and roles.
- `/status` (staff): overall health and the count of permission problems.
- `/clipcast status`, `/smp status`, `/appeals list`, `/sitedata refresh`: each says what it
  is missing, by setting name, never by value.
- `bot\data\bot.log`: the last lines after a restart.
