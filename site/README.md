# Landing page

A single static page (`index.html`, `style.css`, `app.js`, `config.js`, `img/`), no build step,
no framework, no trackers. `.github/workflows/pages.yml` deploys this folder to GitHub Pages on
every push to `main` that touches `site/**` (or by hand from the Actions tab).

## Set the invite (once)

Edit `config.js` and replace `REPLACE_ME` with the bare invite code (the part after
`discord.gg/`). Use a permanent invite: `server/public_mode.py --apply` creates one and prints it.
That is the only place the code lives; every Join button reads it. Until it is set the buttons
are greyed out and the live counts stay hidden.

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

## Enable Pages (once)

    gh api -X POST repos/gr8monk3ys/discord-server/pages -f build_type=workflow

(or Settings > Pages > Source: GitHub Actions). Then run the workflow or push to `site/`.

## Preview locally

    python -m http.server -d site 8000
