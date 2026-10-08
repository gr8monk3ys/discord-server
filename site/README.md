# Landing page

A static page (`index.html`, `404.html`, `style.css`, `theme.js`, `app.js`, `config.js`, `img/`),
no build step, no framework, no trackers, no scripts from anywhere but this folder (Google Fonts
is the only third party). It lives at **<https://discord.lscaturchio.xyz/>** on Vercel; GitHub
Pages (`.github/workflows/pages.yml`) keeps a mirror at
<https://gr8monk3ys.github.io/discord-server/>, whose canonical tag points back at the subdomain.

## Look

It is styled as part of lscaturchio.xyz and follows that repo's `DESIGN.md` ("The Field
Notebook"): warm paper with a faint grain, one pen (Forest Ink `#184e35`, `#42a979` at night),
sand hairlines instead of boxes, Fraunces for headings, Instrument Sans for reading and IBM Plex
Mono for the small uppercase wall labels. Light ("paper") and dark ("night") themes are CSS
tokens in `style.css`: the page follows `prefers-color-scheme` until the toggle in the header is
used, and `theme.js` remembers that choice in `localStorage` (guarded, so private windows just
don't remember). Everything collapses under `prefers-reduced-motion`. The header links back to
lscaturchio.xyz ("by Lorenzo").

## Set the invite (once)

Edit `config.js` and set `INVITE_CODE` to the bare invite code (the part after `discord.gg/`).
Use a permanent invite: `server/public_mode.py --apply` creates one and prints it. That is the
only place the code lives; every Join button reads it. Until it is set the buttons are greyed out
and the live counts stay hidden.

## Live counts

`app.js` reads `approximate_member_count` and `approximate_presence_count` from Discord's public
`https://discord.com/api/v10/invites/CODE?with_counts=true`, which sends CORS headers for any
origin (checked 2026-10-07). If the request fails or the invite expires, the counts stay hidden.
No guild ID is needed or embedded.

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
this origin, Google Fonts and `https://discord.com` for the counts; `nosniff`;
`strict-origin-when-cross-origin`; a deny-all Permissions-Policy; no framing) and caching: 30 days
for `img/`, revalidate on every request for HTML, CSS and JS (the Vercel CDN still serves them
from cache until the next deploy). The CSP means no inline scripts, styles or event handlers;
the JSON-LD block is a data block and is not affected. `test_sitegen.py` checks the pages
against the policy. `.vercelignore` keeps this README out of the deploy.

`404.html` is served for any missing path, so it links its assets root-absolute (`/style.css`).
The Pages workflow rewrites those to `/discord-server/...` for the mirror.

## Enable the Pages mirror (once)

    gh api -X POST repos/gr8monk3ys/discord-server/pages -f build_type=workflow

(or Settings > Pages > Source: GitHub Actions). Then run the workflow or push to `site/`.

## Preview locally

    python -m http.server -d site 8000

`http.server` sends none of the Vercel headers and shows its own 404; open `/404.html` directly.
