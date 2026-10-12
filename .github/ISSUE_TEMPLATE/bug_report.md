---
name: Bug report
about: Something in the bot, the setup scripts, the landing page or the theme is wrong
title: ""
labels: bug
---

<!--
This repo is public. Do not paste tokens, webhook URLs, server or user IDs, member names,
or raw lines from bot.log that contain them. Security problems go to a private advisory
instead (see SECURITY.md).
-->

**Which part**

- [ ] Front Desk bot (`bot/`), module or command:
- [ ] Setup scripts (`server/`), script:
- [ ] Landing page (`site/`)
- [ ] Client theme (`theme/`)

**What happened**

**What you expected**

**Steps to reproduce**

1.
2.

**Details**

- Commit (`git rev-parse --short HEAD`):
- For the bot: what `/status` shows (permissions line, modules, recent errors),
  and the error line from `bot/data/bot.log` with IDs and names removed
- For a script: was it a dry run or `--apply`?
- For the site or theme: browser or client mod, and light or dark
