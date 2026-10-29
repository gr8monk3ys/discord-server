// The home page's fixed text that more than one place needs: its metadata, the JSON-LD
// and the Monkey SMP season date (the tests read them from here too).
import { CANONICAL, SITE_ORIGIN } from "@/lib/site";

export const TITLE = "Chill Gamers · Discord server";
export const DESCRIPTION =
  "A small, friendly Discord server for co-op and PvE games: Risk of Rain 2, Gears of War, Overwatch, Marvel Rivals, Minecraft and a few more. People are mostly in voice in the evenings, US Pacific time.";
export const SHARE =
  "A small co-op and PvE Discord server. Risk of Rain 2, Gears of War, Overwatch, Minecraft and a few more, mostly in voice in the evenings, US Pacific time.";
export const OG_ALT = "Chill gamers. No tryhards, no toxicity. Server banner listing the games we play.";

// When Monkey SMP Season I opens (Sat 7 Nov 2026, 5 PM Pacific).
export const SEASON_START = "2026-11-08T01:00:00Z";
export const SEASON_TEXT = "Season I · Sat\u00a07\u00a0Nov, 5\u00a0PM\u00a0Pacific";

export const JSON_LD = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      "@id": `${CANONICAL}#org`,
      name: "Chill Gamers",
      url: CANONICAL,
      logo: `${SITE_ORIGIN}/img/server-icon.png`,
      description:
        "A small, friendly Discord server for co-op and PvE games. People are mostly in voice in the evenings, US Pacific time.",
      founder: { "@type": "Person", name: "Lorenzo Scaturchio", url: "https://lscaturchio.xyz" },
    },
    {
      "@type": "WebSite",
      "@id": `${CANONICAL}#website`,
      name: "Chill Gamers",
      url: CANONICAL,
      inLanguage: "en",
      publisher: { "@id": `${CANONICAL}#org` },
    },
  ],
};
