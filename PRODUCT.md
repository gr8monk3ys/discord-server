# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The site's primary visitor is a stranger who found the server on a listing (discord.me, Discadia
and similar) and is deciding in a minute or two whether to join. They want to know who is there,
what gets played, when people are on, what the rules are and what happens after they click Join.
Current members are a secondary audience: they come back for the rules, the week's schedule and
Monkey SMP details.

## Product Purpose

Chill Gamers (Discord "Lorenzo's Server") is a small friend-group server for co-op and PvE games,
mostly in voice in the evenings, US Pacific time. The landing page at discord.lscaturchio.xyz
exists to turn the right strangers into members who actually show up in voice. Success is a join
that turns into a regular, not a raw member count. The gap is activity, not features (owner's
rule since 2026-10-12): pages should help a newcomer get into voice, not advertise more bot
commands.

## Positioning

A real small group run by one named person, with its own bot (Front Desk, open source in this
repo) and the owner's modded Minecraft server. Honest about size: some nights are quiet, and the
page says so instead of inflating it.

## Operating Context

- Discord server managed as code: `server/layout.py` is the source of truth for channels, roles,
  games and the rules embed; the site should match it, not drift from it.
- Front Desk publishes live aggregates (no names, no IDs) to the `site-data` branch; the page
  shows them only when fresh.
- Monkey SMP is the owner's paid-access modded Minecraft server (one access price for everyone via
  Tebex, cosmetic-only perks). Season I opens Sat 7 Nov 2026, 5 PM Pacific (owner, 2026-10-29). The site gives it a
  short section (what it is, the Season I date) that links to monkeysmp.lscaturchio.xyz; prices,
  requirements and its rules live there and are not repeated here (owner, 2026-10-29).
- Ban appeals live at `/appeal`; `/join` redirects to the permanent invite.

## Capabilities and Constraints

- A Next.js app in `site/` on the lscaturchio.xyz stack (App Router, TypeScript, Tailwind v4, bun),
  since the owner asked for it on 2026-10-29; it was static HTML before. No trackers and no third
  party: next/font serves the fonts from this origin. The appeal form and Linked Roles are route
  handlers. One origin, on Vercel; the GitHub Pages mirror was retired with the port.
- All-ages and SFW (since 2026-10-29; the 18+ rule was dropped). Everything must work without JavaScript and under `prefers-reduced-motion`.
- The visual language is borrowed from lscaturchio.xyz ("The Field Notebook"); site/README.md
  records it. Refinements keep it.

## Brand Commitments

- Name: Chill Gamers. Tagline in use: "Chill gamers. No tryhards, no toxicity."
- Voice: plain, softer but honest, short sentences. No emoji on the site (owner, 2026-10-12: "the
  emojis look ass"). No hype, no invented activity.
- Assets: `site/img/server-icon.png`, `site/img/cursive.svg` (owner wordmark), `site/img/og.png`.

## Evidence on Hand

Real: the games list, the weekly bot schedule, live counts and standings from Front Desk, the
rules embed, the Monkey SMP rules and season date. Absent, never to be fabricated: testimonials,
member quotes, screenshots of busy voice rooms, activity numbers beyond what Front Desk publishes.

## Product Principles

1. Honest beats impressive: describe the server as it is on a normal night.
2. One source of truth: rules, games and channels come from `server/layout.py`.
3. Get people into voice: every section should make the first evening easier.
4. Quiet and fast: no trackers, server-rendered, works without JavaScript.

## Accessibility & Inclusion

Keyboard and screen-reader usable, reduced-motion respected, both light and dark themes.
