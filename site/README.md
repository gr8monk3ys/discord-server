# Landing page

A static page (`index.html`, `404.html`, `join/index.html`, `style.css`, `theme.js`, `app.js`,
`join.js`, `config.js`, `img/`),
no build step, no framework, no trackers, no scripts from anywhere but this folder (Google Fonts
is the only third party). It lives at **<https://discord.lscaturchio.xyz/>** on Vercel; GitHub
Pages (`.github/workflows/pages.yml`) keeps a mirror at
<https://gr8monk3ys.github.io/discord-server/>, whose canonical tag points back at the subdomain.

## Look

It is styled as a page of lscaturchio.xyz and takes its values from that repo (`DESIGN.md`, "The
Field Notebook", plus `src/app/globals.css` and the home page components): warm paper with a
faint grain, one pen (Forest Ink `#184e35`, `#42a979` at night), sand hairlines instead of boxes,
Fraunces for headings, Instrument Sans for reading and IBM Plex Mono for the small uppercase wall
labels. The structure is the main site's too: the same header (cursive wordmark at the left, which
links back to lscaturchio.xyz; nav pill; theme toggle), a masthead with a square plate, numbered
ledger sections with a sticky wall-label rail, and the site-map footer. `img/cursive.svg` is a copy
of that repo's `public/cursive.svg`. The fonts are the same cuts next/font serves there (Fraunces
without the optical-size axis); here they still come from Google Fonts. Light ("paper") and dark ("night") themes are CSS
tokens in `style.css`: the page follows `prefers-color-scheme` until the toggle in the header is
used, and `theme.js` remembers that choice in `localStorage` (guarded, so private windows just
don't remember). Everything collapses under `prefers-reduced-motion`.

## Set the invite (once)

Edit `config.js` and set `INVITE_CODE` to the bare invite code (the part after `discord.gg/`).
Use a permanent invite: `server/public_mode.py --apply` creates one and prints it. That is the
only place the code lives; every Join button reads it. Until it is set the buttons are greyed out
and the live counts stay hidden.

## Short join link

<https://discord.lscaturchio.xyz/join> sends people straight to the invite: `join/index.html`
loads `config.js` and `join.js`, which checks the code the same way `app.js` does and calls
`location.replace("https://discord.gg/CODE")`. The page shows a Join button in case the redirect
is blocked and a note for readers without JavaScript; it is `noindex` (meta tag and an
`X-Robots-Tag` header in `vercel.json`) with its canonical on the home page. With `cleanUrls` on
and `trailingSlash` off, Vercel serves it at `/join` and redirects `/join/` there. Its assets
are linked parent-relative (`../style.css`), so it also works on the Pages mirror and in the
local preview. The bot's `/vote` hands out this link (`bot/logic/votehub.py` `SHARE_URL`).

## Live counts

`app.js` reads `approximate_member_count` and `approximate_presence_count` from Discord's public
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
   `layout.py` only and event titles that are not the bot's own template go through the text filter.
2. `server/publish_site_data.py --apply` (run it from a scheduled task every 30 minutes) checks the
   file again against `server/site_schema.py` and pushes it as the single file `stats.json` on the
   orphan branch `site-data`, only when the content changed or the published copy is a day old.
   Without `--apply` it is a dry run.
3. `app.js` fetches `STATS_URL` from `config.js`
   (`https://raw.githubusercontent.com/gr8monk3ys/discord-server/site-data/stats.json`) with a
   6 second timeout, checks every field again and renders with `textContent` only.

The section and the masthead line carry the `hidden` attribute in the HTML. If the fetch fails,
the file is older than 48 hours, or JavaScript is off, they stay hidden and the page is the static
page. The section opens between "This week" and "Games" without moving what the visitor is
looking at: below the fold it just appears, above the viewport the scroll position is corrected,
and while that spot is on screen it waits. Set `STATS_URL` to `""` to turn it off.
To try it locally, serve `site/` and point `STATS_URL` at a fixture file next to the page
(do not commit that change; `bot/tests/test_sitedata_site.py` checks the real URL).

## Games list

The games block between `<!-- games:start -->` and `<!-- games:end -->` is generated from
`server/layout.py` `GAMES`. After changing the games, run from `bot/`:

    ../server/.venv/Scripts/python -m logic.sitegen

`bot/tests/test_sitegen.py` fails if the page and `layout.py` disagree.

## Link previews

`index.html` carries Open Graph and Twitter card tags, a canonical URL
(`https://discord.lscaturchio.xyz/`, the one origin used in every absolute URL on the page) and a
minimal JSON-LD Organization/WebSite block, so links unfurl with `img/og.png` (1200x630). The
image is a centred crop of `assets/listing/hero.png`; after regenerating the hero, rebuild it
from `bot/`:

    ../server/.venv/Scripts/python -m logic.sitegen --og

The "This week on the server" strip is static copy; update it by hand if the schedule changes.

## Deploy on Vercel (once)

Import `gr8monk3ys/discord-server` as a new Vercel project with:

| Setting | Value |
| --- | --- |
| Framework Preset | Other |
| Root Directory | `site` |
| Build Command | empty (override on, nothing in the box) |
| Install Command | empty (override on; `vercel.json` also sets `""`) |
| Output Directory | `.` |
| Production Branch | `main` |

Then Settings > Domains > add `discord.lscaturchio.xyz`, and at the DNS host add the record
Vercel shows (normally `CNAME discord -> cname.vercel-dns.com`).

`vercel.json` sets clean URLs, the security headers (a Content-Security-Policy that allows only
this origin, Google Fonts, `https://discord.com` for the counts and
`https://raw.githubusercontent.com` for the live data; `nosniff`;
`strict-origin-when-cross-origin`; a deny-all Permissions-Policy; no framing) and caching: 30 days
for `img/`, revalidate on every request for HTML, CSS and JS (the Vercel CDN still serves them
from cache until the next deploy). The CSP means no inline scripts, styles or event handlers;
the JSON-LD block is a data block and is not affected. The house bars set their width through
the CSSOM (`element.style.width`), which `style-src 'self'` allows. `test_sitegen.py` checks the pages
against the policy. `.vercelignore` keeps this README out of the deploy.

`404.html` is served for any missing path, so it links its assets root-absolute (`/style.css`).
The Pages workflow rewrites those to `/discord-server/...` for the mirror.

## Enable the Pages mirror (once)

    gh api -X POST repos/gr8monk3ys/discord-server/pages -f build_type=workflow

(or Settings > Pages > Source: GitHub Actions). Then run the workflow or push to `site/`.

## Preview locally

    python -m http.server -d site 8000

`http.server` sends none of the Vercel headers and shows its own 404; open `/404.html` directly.
