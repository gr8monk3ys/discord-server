// The ONLY place the invite lives. Owner: replace REPLACE_ME with the bare
// invite code (the part after discord.gg/, e.g. "chill-gamers"), not a URL.
// server/public_mode.py prints the permanent invite it made or found.
// Use a permanent invite (never expires, unlimited uses), or the Join
// button and the live counts stop working when it expires.
//
// STATS_URL is the live data the bot publishes: one small JSON file of
// server-wide aggregates (no names, no IDs) on this repo's site-data branch,
// pushed by server/publish_site_data.py. Empty string turns the live section off.
window.SITE_CONFIG = {
  INVITE_CODE: "C25hy9Ca9W",
  STATS_URL: "https://raw.githubusercontent.com/gr8monk3ys/discord-server/site-data/stats.json",
};
