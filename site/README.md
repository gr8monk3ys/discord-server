# Landing page

A Next.js app on the same stack as lscaturchio.xyz: Next.js 16 (App Router, TypeScript),
Tailwind v4 through `@tailwindcss/postcss`, next-themes, bun with `bun.lock`, Vitest and
Playwright, the same ESLint base and `engines` (Node 22 or later). It became one on 2026-10-29,
at the owner's request ("make it framework based just like how I have lscaturchio.xyz setup");
until then it was static HTML with no build step. The look, the copy and the behaviour did not
change.

It lives at **<https://discord.lscaturchio.xyz/>** on Vercel (root directory `site`). There is
no mirror any more: the GitHub Pages copy could not run the route handlers, so it was retired.

No trackers, no analytics, no third-party scripts, styles or fonts: next/font serves the three
faces from this origin. Left out on purpose, although lscaturchio.xyz has them: Sentry, analytics,
the chat/RAG and Supabase code, the MDX blog machinery and `@gr8monk3ys/next-kit` (its Stripe and
Clerk helpers have nothing to do here, and its in-memory rate limiter does nothing on serverless).

## Where things live

| Path | What |
| --- | --- |
| `src/app/page.tsx` | the home page (server-rendered; the copy is here) |
| `src/app/appeal/`, `src/app/linked-role/`, `src/app/join/`, `src/app/not-found.tsx` | the other pages |
| `src/app/api/appeal/route.ts`, `src/app/api/linked-role/{start,callback}/route.ts` | the route handlers |
| `src/server/` | what the handlers do (`appeal.ts`, `linked-role/`), with their tests |
| `src/components/` | header, footer and the small client islands (counts, live section, season line, theme toggle, motion, appeal form) |
| `src/lib/` | pure helpers: `stats.ts` (the live data check), `site.ts` (invite, CSP), `home.ts` (metadata, JSON-LD) |
| `src/data/site-config.json` | the invite code and the live-data URL, nowhere else |
| `src/data/security.json` | the Content-Security-Policy and the other security headers |
| `src/data/generated/` | games and rules, written by `bot/logic/sitegen.py` |
| `src/app/field-notebook.css`, `appeal.css`, `globals.css` | the stylesheet (was `style.css`), the appeal form, the Tailwind layers |
| `src/proxy.ts` | the per-request CSP nonce |
| `public/img/` | wordmark, icons, the social card |
| `e2e/` | Playwright tests against the production build |

## Commands

From `site/`, with bun 1.3 or later:

    bun install --frozen-lockfile
    bun run dev          # http://localhost:3000 (development CSP: allows eval and inline styles)
    bun run lint
    bun run typecheck
    bun run test         # Vitest: handlers, page markup and metadata, the live-data check
    bun run build
    bun run test:e2e     # builds, starts on 127.0.0.1:4173, runs Playwright (locally on Edge)

CI (`.github/workflows/ci.yml`, job "Site") runs the same steps with bun 1.3.9. The lockfile is
the version-1 text format that bun 1.3 reads; regenerate it with `bunx bun@1.3.9 install`
if a newer bun rewrites it.

## Look

It is styled as a page of lscaturchio.xyz and takes its values from that repo (`DESIGN.md`, "The
Field Notebook", plus `src/app/globals.css` and the home page components): warm paper with a
faint grain, one pen (Forest Ink `#184e35`, `#42a979` at night), sand hairlines instead of boxes,
Fraunces for headings, Instrument Sans for reading and IBM Plex Mono for the small uppercase wall
labels. The structure is the main site's too: the same header (cursive wordmark at the left, which
links back to lscaturchio.xyz; nav pill; theme toggle), a masthead with a square plate, ledger
sections with a sticky wall-label rail, and the site-map footer. `public/img/cursive.svg` is a
copy of that repo's `public/cursive.svg`. The fonts are the cuts next/font serves there (Fraunces
without the optical-size axis), with the metric-matched fallbacks it generates.

The styles are the house classes in `field-notebook.css`, carried over unchanged. Tailwind's
theme and utilities layers are loaded, mapped to the same tokens (`bg-page`, `text-pen`,
`font-display` ...), for new work; its preflight reset is not, so nothing about the old look
moved. Light ("paper") and dark ("night") themes are CSS tokens: the page follows
`prefers-color-scheme` until the toggle is used, and next-themes remembers the choice in
`localStorage` under `theme` (the same key and `data-theme` attribute as before). Without
JavaScript the toggle is hidden, since it could do nothing. Everything that moves stops under
`prefers-reduced-motion`.

Motion is scroll-driven and quiet. `PageMotion` arms the sections below the fold and inks them in
as they arrive (headings and rows wipe in left to right with a short stagger, the house bars
fill); whatever is already on screen at load is never hidden. The header pill keeps the pen under
the section you are reading (`aria-current="location"`). On wide screens, browsers with
`animation-timeline: view()` draw a reading line under each section's wall label (CSS only).
Without JavaScript nothing is armed, so nothing is hidden.

## Set the invite (once)

Set `inviteCode` in `src/data/site-config.json` to the bare invite code (the part after
`discord.gg/`). Use a permanent invite: `server/public_mode.py --apply` creates one and prints
it. That is the only place the code lives; every Join button, the `/join` redirect and the bot's
own-invite allowlist (`bot/logic/textfilter.py`) read it. Until it is set the buttons are greyed
out, `/join` explains instead of redirecting, and the live counts stay hidden.

## Short join link

<https://discord.lscaturchio.xyz/join> is a 307 redirect to `https://discord.gg/CODE`
(`next.config.ts`, from the config above), so it works without JavaScript and without a page in
between. It carries `X-Robots-Tag: noindex, follow`. The bot's `/vote` hands out this link
(`bot/logic/votehub.py` `SHARE_URL`).

## Live counts

`Counts` reads `approximate_member_count` and `approximate_presence_count` from Discord's public
`https://discord.com/api/v10/invites/CODE?with_counts=true`, which sends CORS headers for any
origin (checked 2026-10-07). If the request fails or the invite expires, the counts fall back to
the member count in the live data below, and stay hidden when that is missing too.
No guild ID is needed or embedded.

## Live section

The page shows what is happening on the server: upcoming events in the visitor's time zone, house
standings as a bar strip (when the houses module exists), the most played games of the week, a
line of weekly totals, and a "next up" line in the masthead. The data is one small JSON file of
server-wide aggregates, with no member names, avatars or IDs:

1. The bot (`bot/cogs/sitedata.py`, rules in `bot/logic/sitedata.py`) writes
   `bot/data/site/stats.json` every 30 minutes, or on `/sitedata refresh` (staff). Anything that
   describes fewer than 3 members is left out, numbers are rounded, game names come from
   `layout.py` only, and event titles are always one of the bot's own templates ("Overwatch game
   night", "Town Hall", "Watch party", "Overwatch tournament"); an event with any other title is
   left out, so a tournament's name, a watch party's subject and a hand-made event's title
   never reach the page. House standings are whole-number points per member, published only
   when every house has 3 or more members who scored that week.
2. `server/publish_site_data.py --apply` (run it from a scheduled task every 30 minutes) checks the
   file again against `server/site_schema.py` and pushes it as the single file `stats.json` on the
   orphan branch `site-data`, only when the content changed or the published copy is a day old.
   Without `--apply` it is a dry run.
3. The page fetches `statsUrl` from `src/data/site-config.json`
   (`https://raw.githubusercontent.com/gr8monk3ys/discord-server/site-data/stats.json`) in the
   browser, with a 6 second timeout, checks every field again (`src/lib/stats.ts`) and renders it
   as React text only.

The section and the masthead line are server-rendered with the `hidden` attribute. If the fetch
fails, the file is older than 48 hours, or JavaScript is off, they stay hidden and the page is
unchanged. The section opens between "Monkey SMP" and "House rules" without moving what the
visitor is looking at: below the fold it just appears, above the viewport the scroll position is
corrected, and while that spot is on screen it waits. Set `statsUrl` to `""` to turn it off.
`bot/tests/test_sitedata_site.py` holds the site's limits to the bot's schema; `e2e/site.spec.ts`
opens the section with a routed fixture and checks that stale, failed and broken data keep it
hidden.

## Ban appeals (`/appeal`, `/api/appeal`)

A banned member cannot use the server, so they appeal here. `/appeal` is a form with five fields:
Discord user ID, the appeal code Front Desk sent them by DM just before the ban, and three
answers (what happened, why reconsider, anything else). `AppealForm` checks the fields
(`src/lib/appeal-form.ts`) and posts them as JSON to `/api/appeal`. The page is `noindex` and is
not linked from the home page; the ban DM carries the link.

`src/server/appeal.ts` (route `src/app/api/appeal/route.ts`) has no dependencies:

- It is off (HTTP 503, "appeals aren't open") until `APPEAL_SECRET` and `APPEAL_WEBHOOK_URL` are
  both set in the Vercel project.
- It accepts only a JSON `POST` of at most 8 KB sent from this site's own origin. The body is
  read as a stream and cut off at that size whatever `Content-Length` says.
- The code is an HMAC of the user ID under `APPEAL_SECRET`; `bot/logic/appeals.py` computes the
  same one. Only a matching code is forwarded, as a small JSON payload, to the Discord webhook in
  the staff-only 🔒・appeals channel. There the bot (`bot/cogs/appeals.py`) verifies it again,
  deletes the raw message and posts a review card.
- A wrong code gets the same answer as a right one, so the endpoint cannot be used to test IDs
  or codes. Every check on the request's shape and size happens before the code is compared.
  The one answer only a right code can get is 502, when Discord did not take the appeal, so
  that a banned member is not told "Received" for an appeal that was lost.
- It has no rate limit of its own; a Vercel firewall rule on `/api/appeal` is the place for one
  (`docs/WAVE9_SETUP.md`).
- It never logs the secret, the code, the answers or the webhook URL, and it stores
  nothing.

The question keys and length limits are repeated in `src/lib/appeal-form.ts`,
`src/server/appeal.ts` and `bot/logic/appeals.py`: change all three together
(`bot/tests/test_sitegen.py` and `src/lib/appeal-form.test.ts` check that they agree).

## Linked Roles (`/api/linked-role/*`, `/linked-role`)

Discord's Linked Roles for verified Steam, Xbox, PlayStation and Epic Games connections. What it
is, the privacy notes and the full setup are in `docs/LINKED_ROLES.md`; this is only the map.

- `/api/linked-role/start`: where Discord sends a member who presses Link. Sets the signed
  state cookie (`__Host-fd_linked_role`, HttpOnly, 10 minutes) and redirects to Discord's consent
  screen.
- `/api/linked-role/callback`: where Discord sends them back. Checks the cookie, exchanges the
  code, reads the member's connections once, writes one yes/no per platform to Discord, clears
  the cookie and redirects to `/linked-role` with a result flag.
- `src/server/linked-role/lib.ts` and `flow.ts` (the code), `metadata.json` (the five records;
  also read by `bot/logic/linkedroles.py` and `server/register_linked_roles.py`).
- `/linked-role`: the page the flow ends on. It picks its text from a fixed list by the flag in
  the address (`src/lib/linked-role-results.ts`); nothing from the address is shown.

Both endpoints answer 503 ("not set up yet") until all four variables below are set. This cookie
is the only one the site ever sets, which is why the page footers say "This page doesn't track
you" and not "No cookies".

## Environment variables (Vercel)

Project > Settings > Environment Variables (Production), then redeploy. The pages need none.
None of these belong in the repo; only `APPEAL_SECRET` also goes in `server/.env`. The names and
values are the same as before the port.

| Name | Used by | Value |
| --- | --- | --- |
| `APPEAL_SECRET` | `/api/appeal` | 16+ random characters, **the same value** as `APPEAL_SECRET` in `server/.env` |
| `APPEAL_WEBHOOK_URL` | `/api/appeal` | the URL of a webhook created in the 🔒・appeals channel (`https://discord.com/api/webhooks/...`) |
| `APPEAL_ORIGIN` | `/api/appeal` | optional: the origin the form is served from, when it is not `https://discord.lscaturchio.xyz` |
| `DISCORD_CLIENT_ID` | `/api/linked-role/*` | the Front Desk application's Client ID |
| `DISCORD_CLIENT_SECRET` | `/api/linked-role/*` | its OAuth2 Client Secret |
| `LINKED_ROLE_STATE_SECRET` | `/api/linked-role/*` | 32+ random characters; signs the cookie |
| `LINKED_ROLE_REDIRECT_URI` | `/api/linked-role/*` | `https://discord.lscaturchio.xyz/api/linked-role/callback` |

## Games list and rules

`src/data/generated/games.json` is generated from `server/layout.py` `GAMES`, and `rules.json`
from the `#rules` embed in `layout.POSTS`. After changing either, run from `bot/`:

    ../server/.venv/Scripts/python -m logic.sitegen

`bot/tests/test_sitegen.py` fails if the data files and `layout.py` disagree.

## Link previews

The home page's metadata (`src/lib/home.ts`, `src/app/page.tsx`) carries Open Graph and Twitter
card tags, a canonical URL (`https://discord.lscaturchio.xyz/`, the one origin used in every
absolute URL on the page) and a minimal JSON-LD Organization/WebSite block, so links unfurl with
`public/img/og.png` (1200x630). The image is a centred crop of `assets/listing/hero.png`; after
regenerating the hero, rebuild it from `bot/`:

    ../server/.venv/Scripts/python -m logic.sitegen --og

## Copy

The page describes a small co-op and PvE server and nothing bigger. The headline is fixed
("Chill gamers. No tryhards, no toxicity."); the two paragraphs under it say what gets played,
when people are on, how small it is and how to join. "Your first week" holds four entries and
one short "Also here" list: add a system there only if a new member would use it in week one.
No member counts, hours or activity claims in the static text (the live section shows real
numbers when there are any), and no retired games outside the generated games list
(`src/app/pages.test.tsx` checks both).

The "A normal week" strip is static copy and lists only what happens without anyone hosting it:
people in voice, and what Front Desk posts on a timer (`bot/cogs/houses.py`, `events.py`,
`engagement.py`). Update it by hand if those schedules change, and word anything that depends
on members showing up as exactly that.

## Security headers and the CSP

Every page and API response gets `nosniff`, `strict-origin-when-cross-origin`, a deny-all
Permissions-Policy, `X-Frame-Options: DENY` and `Cross-Origin-Opener-Policy: same-origin`
(`src/data/security.json`, applied in `next.config.ts`), and a Content-Security-Policy built per
request in `src/proxy.ts`:

    default-src 'self'; script-src 'self' 'nonce-…'; style-src 'self' 'nonce-…'; font-src 'self';
    img-src 'self' data:; connect-src 'self' https://discord.com https://raw.githubusercontent.com;
    manifest-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none';
    object-src 'none'; upgrade-insecure-requests

Why a nonce: Next.js puts small inline scripts in every page (the React payload that hydrates
it), and next-themes needs one in the head so the right theme is set before the first paint. The
old `script-src 'self'` would block them, and `'unsafe-inline'` would let any injected script run
too. A fresh random nonce per request allows exactly the scripts the server wrote and nothing
else; Next reads it from the request's CSP header and stamps it on its own scripts, and the layout
hands it to next-themes. There is no `'strict-dynamic'`: every script file still has to come from
this origin. The house bars set their width through the CSSOM (React's `style` prop on the
client), which the policy allows; no `style` attribute is ever server-rendered.

The price: a nonce has to be new on every response, so the pages render per request (dynamic)
and their HTML is not cached by the CDN (`Cache-Control: private, no-cache, no-store`). The
static assets (`/_next/static`, `/img`) still are, and the pages are small and fast to render.
In `next dev` the policy also allows `'unsafe-eval'` and inline styles, which React's dev overlay
needs; production never does. Images get their own fixed policy
(`default-src 'none'; frame-ancestors 'none'`) and a 30-day cache. `/appeal`, `/linked-role` and
`/api/*` also get `X-Robots-Tag: noindex, nofollow`; `/join` gets `noindex, follow`.

## Deploy on Vercel

`vercel.json` sets the framework (Next.js), the install command (`bun install
--frozen-lockfile`) and the build command (`bun run build`); everything else is in
`next.config.ts`. The project settings that matter: Root Directory `site`, Production Branch
`main`, and no Output Directory override (Next.js decides it). The domain is
`discord.lscaturchio.xyz` (`CNAME discord -> cname.vercel-dns.com` at the DNS host).
