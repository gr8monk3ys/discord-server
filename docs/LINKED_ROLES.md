# Linked Roles

Discord's own "Linked Roles": a member opens the server menu, chooses **Linked Roles**, presses
**Link** on a role, authorises Front Desk, and gets the role only if their Discord account has a
**verified** connection to the platform the role asks for. Discord hands out and removes the
role itself; the bot is not involved and needs no extra permission.

It is off until every step below is done. Until then `/api/linked-role/start` answers
"not set up yet" (HTTP 503) and nothing else changes.

## What it is, and what it isn't

| | Self-assigned platform roles | `/link steam` | Linked Roles |
|---|---|---|---|
| Where | `#roles` buttons, Onboarding | bot command | server menu > Linked Roles |
| Proof | none, the member says so | a code in the Steam profile, read by the bot | Discord's "verified" mark on the connection |
| Stored by us | the role | the Steam profile and hours (`game_accounts`) | nothing |
| Shows on | member list | `/profile`, `/accounts` | a badge on the role and on the member's profile |

The three do not replace each other. `PlayStation` and `Xbox` (self-assigned, from
`server/layout.py`) say what someone plays on and stay as they are. A linked role says Discord
has checked the account exists and belongs to them. It says nothing about which games they own.

Platforms offered: **Steam, Xbox, PlayStation, Epic Games**, plus a count of how many of those
four are verified. Riot Games and Battle.net are not on Discord's documented list of connection
services (checked 2026-10-12), so there are no flags for them. Discord allows five records per
application and all five are used; adding a platform means dropping one
(`site/src/server/linked-role/metadata.json`).

## Privacy

- **Read:** the member's list of connected accounts (`GET /users/@me/connections`), once, in
  memory, after they approve Discord's consent screen. From each entry only the service
  (`steam`, `xbox`, ...) and the `verified` / `revoked` marks are looked at. The consent screen
  also lists "username and avatar" (the `identify` scope, which Discord's Linked Roles flow is
  documented with); that endpoint is never called.
- **Written:** to Discord, for that member and this application: one yes/no per platform, the
  count, the label "Front Desk" and a summary such as "2 platforms linked". No gamertag, account
  id or account name is written anywhere. Discord shows the label and the summary on the
  member's profile when they hold a linked role.
- **Stored by us:** nothing. No database, no log of who linked. The access token lives for one
  request and is dropped; the only log line on a failure is the step and the HTTP status.
- **Cookie:** one, `__Host-fd_linked_role`: a random value plus a signature, HttpOnly, 10 minutes,
  deleted when the member comes back. It is what stops someone else's sign-in from being
  finished in your browser.
- **Undo:** User Settings > Authorized Apps > Front Desk > Deauthorize. Discord then drops the
  connection and the role.

The answer is a snapshot from the moment Link was pressed. Tokens are not kept, so the site
cannot re-check later: someone who disconnects Steam afterwards keeps the role until they link
again or deauthorise. Fine for a badge; do not hang access to anything sensitive on it.

## Turn it on

Do these in order. Step 3 before steps 1 and 2 would give members a Link button that leads to
the "not set up yet" page.

### 1. Developer Portal: redirect URI

<https://discord.com/developers/applications> > Front Desk > **OAuth2** > Redirects > add,
exactly:

    https://discord.lscaturchio.xyz/api/linked-role/callback

On the same page copy the **Client ID** and reset/copy the **Client Secret** (shown once).

### 2. Vercel: four environment variables

Project > Settings > Environment Variables (Production), then redeploy. Mark the two secrets
Sensitive.

| Name | Value |
|---|---|
| `DISCORD_CLIENT_ID` | the application's Client ID (digits) |
| `DISCORD_CLIENT_SECRET` | the Client Secret from step 1 |
| `LINKED_ROLE_STATE_SECRET` | 32+ random characters, e.g. `openssl rand -base64 48`. Used only to sign the cookie |
| `LINKED_ROLE_REDIRECT_URI` | `https://discord.lscaturchio.xyz/api/linked-role/callback`, identical to step 1 |

If any is missing, or the redirect URI is not an `https://.../api/linked-role/callback` URL, the
feature stays off. None of them belong in the repo or in `server/.env`. Changing
`LINKED_ROLE_STATE_SECRET` only invalidates sign-ins that are in flight.

Check: open <https://discord.lscaturchio.xyz/api/linked-role/start> in a browser. You should
land on Discord's consent screen. Cancel there; you should end on `/linked-role` saying
"Cancelled."

The endpoints need no configuration of their own: they are route handlers in the site's
Next.js app (`site/src/app/api/linked-role/`; the project's root directory is `site`). The
redirect URI must always be the Vercel domain, which is the site's only address.

### 3. Register the metadata and the verification URL

    cd server
    .venv/Scripts/python register_linked_roles.py            # dry run: current vs wanted
    .venv/Scripts/python register_linked_roles.py --apply

It uses `DISCORD_TOKEN` from `server/.env` (the bot token; never printed), writes the five
records from `metadata.json` and sets the application's **Linked Roles Verification URL** to
`https://discord.lscaturchio.xyz/api/linked-role/start`. Run it again after editing
`metadata.json`; a second run with nothing to do says so. The PUT replaces the whole list, so
the dry run also shows records it would remove. Renaming a `key` detaches every role that
required the old key: change names and descriptions freely, keys never.

### 4. Create the roles (Server Settings, by hand)

Server Settings > Roles > Create Role > the **Links** tab > Add requirement > **Front Desk** >
pick the condition. Suggested, named so they cannot be mistaken for the self-assigned ones:

| Role | Requirement |
|---|---|
| `Steam Verified` | Steam |
| `Xbox Verified` | Xbox |
| `PlayStation Verified` | PlayStation |
| `Epic Verified` | Epic Games |
| `Multi-Platform` (optional) | Verified platforms at least 2 |

- Give them no permissions and no colour (the One Pen Rule in `layout.py`); they are badges.
- Put them anywhere below Moderator. Unlike roles the bot hands out, they do not have to sit
  below Front Desk: Discord assigns them. Create them in Server Settings, not with
  `setup_server.py`, which reorders roles.
- A role with a Links requirement cannot be given by hand or by a bot, and cannot be an
  Onboarding or `#roles` button. That is why these are not in `layout.py`.
  `snapshot_server.py` will list them in `roles.json` like any other role.

### 5. Test with a second account

1. With an alt that has **no** connections: server name > Linked Roles > `Steam Verified` >
   Link > Authorise. Expect "You're linked." on the site and, back in Discord, the requirement
   still unmet (no role).
2. On the alt, User Settings > Connections > add Steam. Press Link again. Expect the role, and
   "Front Desk · 1 platform linked" on the alt's profile.
3. Press Link and choose Cancel on Discord's screen: "Cancelled.", nothing changes.
4. Reload the finished `…/callback?code=…` address from the browser history: "That took too
   long." (the cookie is gone and the code is spent).
5. Deauthorise Front Desk on the alt (User Settings > Authorized Apps): the role goes away.

## Turn it off

Remove the Links requirement from the roles (or delete them), and clear the Linked Roles
Verification URL in the Developer Portal > General Information. Removing any of the four Vercel
variables turns the endpoints off on the next deploy.

## Files

- `site/src/server/linked-role/metadata.json`: the five records, the label, the verification URL.
  Read by the site and by `bot/logic/linkedroles.py`.
- `site/src/server/linked-role/lib.ts`: settings, the signed state cookie, the mapping from
  connections to metadata, the three Discord calls.
- `site/src/server/linked-role/flow.ts`: the two endpoints; the routes are
  `site/src/app/api/linked-role/start/route.ts` and `callback/route.ts`.
- `site/src/app/linked-role/page.tsx`, `site/src/lib/linked-role-results.ts`: the page the flow ends on.
- `bot/logic/linkedroles.py`: validation of `metadata.json` and the registration diff.
- `server/register_linked_roles.py`: step 3.

Tests (no network):

    cd site && bun run test src/server/linked-role
    cd bot && ../server/.venv/Scripts/python -m pytest -q -p no:warnings tests/test_linkedroles.py
    cd server && .venv/Scripts/python -m pytest -q tests/test_register_linked_roles.py

CI runs the site tests too (the "Site" job).

## Not verified against the live service

Written from Discord's developer docs as read on 2026-10-12; nothing here has been run against
Discord or deployed. In particular: that a linked role survives the access token expiring
(tokens are dropped, not refreshed), that deauthorising removes the role, that a role with a
Links requirement cannot be assigned by hand. Step 5 covers the first two.
