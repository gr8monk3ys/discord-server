---
name: Feature request
about: Suggest a new module, command or change
title: ""
labels: enhancement
---

<!-- This repo is public: no member names, IDs or anything from the live server's data. -->

**What should happen, and for whom**

Members, staff or the owner? What do they do today instead?

**Where it fits**

- [ ] A new bot module (`bot/logic/` + `bot/cogs/`)
- [ ] A change to an existing module:
- [ ] Server structure (`server/layout.py`: a role, channel or game)
- [ ] Landing page or theme

**Things this would touch** (tick what applies; see `docs/ARCHITECTURE.md`)

- [ ] Stores something new about members (needs a migration and a `PRIVACY.md` update)
- [ ] Pays or charges coins (needs an idempotent ledger ref and an account-age rule)
- [ ] Reposts text that members type (needs the text filter and no pings)
- [ ] Runs on a schedule
- [ ] Needs a new channel, role or bot permission
- [ ] Calls an outside service or needs an API key

**Anything else**
