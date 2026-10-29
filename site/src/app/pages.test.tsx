// The pages as the server renders them (what a crawler, a reader without JavaScript and the
// first paint see), and their metadata. Ported from the markup checks that bot/tests/
// test_sitegen.py and test_sitedata_site.py ran against the old static HTML files.
import { existsSync } from "node:fs";
import path from "node:path";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import Home, { metadata as homeMeta } from "@/app/page";
import NotFound, { metadata as notFoundMeta } from "@/app/not-found";
import Appeal, { metadata as appealMeta } from "@/app/appeal/page";
import Join, { metadata as joinMeta } from "@/app/join/page";
import LinkedRolePage from "@/app/linked-role/page";
import { RuleList } from "@/components/Rules";
import games from "@/data/generated/games.json";
import rules from "@/data/generated/rules.json";
import { FIELDS } from "@/lib/appeal-form";
import { DESCRIPTION, JSON_LD, SEASON_TEXT, SHARE, TITLE } from "@/lib/home";
import { CANONICAL, INVITE_URL } from "@/lib/site";

const render = (el: unknown) => renderToStaticMarkup(el as ReactElement);
const unescape = (s: string) =>
  s.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const HOME = render(Home());
const NOT_FOUND = render(NotFound());
const APPEAL = render(Appeal());
const JOIN = render(Join());
const PAGES = { home: HOME, notFound: NOT_FOUND, appeal: APPEAL, join: JOIN };

function section(html: string, id: string): string {
  const m = html.match(new RegExp(String.raw`<section id="${id}"[^>]*>[\s\S]*?</section>`));
  if (!m) throw new Error(`no section #${id}`);
  return m[0];
}
const tag = (html: string, re: RegExp) => {
  const m = html.match(re);
  if (!m) throw new Error(`no match for ${re}`);
  return m[0];
};

describe("home page: copy", () => {
  test("the headline is fixed and the first screen says what this is", () => {
    expect(HOME).toContain("<h1>Chill gamers. <em>No tryhards, no toxicity.</em></h1>");
    const hero = unescape(tag(HOME, /<section class="hero"[\s\S]*?<\/section>/));
    for (const want of ["co-op and PvE", "Risk of Rain 2", "Gears of War", "Minecraft", "evenings", "US Pacific", "data-join"]) {
      expect(hero, want).toContain(want);
    }
  });

  test("it leads with what a new member does", () => {
    // A small server listing a dozen systems reads as empty machinery: four first-week entries
    // with a title each, then one short untitled list for the rest.
    const features = unescape(section(HOME, "features"));
    expect(features.match(/<li><h3 class="card-title">/g)).toHaveLength(4);
    expect((features.match(/<li\b/g) ?? []).length).toBeLessThanOrEqual(8);
    for (const want of ["Lobby", "/lfg", "Fern, Kestrel or Otter", "Monkey SMP", "#smp"]) expect(features, want).toContain(want);
  });

  test("the copy names no retired game and keeps the house style", () => {
    const list = section(HOME, "games");
    const copy = HOME.replace(list, "");
    for (const gone of ["Valorant", "Call of Duty", "CoD", "Fortnite", "League of Legends", "Counter-Strike", "GTA", "Roblox"]) {
      expect(copy, gone).not.toContain(gone);
    }
    expect(HOME).not.toContain("—"); // no em dashes
    expect(DESCRIPTION + SHARE).not.toContain("18+");
  });

  test("the week strip and the FAQ promise only what happens", () => {
    const week = unescape(section(HOME, "week"));
    expect(week).toContain("<h2>A normal week</h2>");
    // the Friday game night is the bot's calendar entry, not a promise of a crowd
    expect(week).toContain("puts a 9 PM game night on the calendar");
    expect(week).not.toContain("Tournament night");
    const faq = unescape(section(HOME, "faq"));
    const details = (faq.match(/<details>/g) ?? []).length;
    expect(details).toBeGreaterThanOrEqual(5);
    expect(details).toBeLessThanOrEqual(8);
    for (const want of ["How busy is it?", "Monkey SMP", "US Pacific"]) expect(faq, want).toContain(want);
    for (const machinery of ["/partner", "Mythic", "/shop"]) expect(faq, machinery).not.toContain(machinery);
    expect(HOME).toMatch(/<p class="counts label" id="counts"/); // live counts stay, as a wall label
  });

  test("the Monkey SMP season date is in the page before any script runs", () => {
    const smp = section(HOME, "smp");
    expect(smp).toContain(`data-start="2026-11-08T01:00:00Z">${SEASON_TEXT}</p>`);
  });
});

describe("home page: structure", () => {
  test("sections are ledgers led by their heading, with no numbers", () => {
    for (const id of ["features", "week", "games", "rules", "faq"]) {
      const s = section(HOME, id);
      expect(s, id).toMatch(/<div class="ledger-head"><h2>[^<]+<\/h2>/);
      expect(s, id).toContain('<div class="ledger-body">');
    }
    expect(HOME).not.toMatch(/<p class="label">\s*\d\d\b/);
    const main = tag(HOME, /<main[\s\S]*?<\/main>/);
    expect(main).not.toMatch(/<p class="label">[^<]*<\/p>\s*<h[12]/); // nothing sits on top of a heading
    expect(HOME.match(/<h1/g)).toHaveLength(1);
    expect(HOME).not.toMatch(/btn-big|btn-small/);
  });

  test("the games and rules lists come from the generated data, as text", () => {
    const list = section(HOME, "games");
    const names = [...list.matchAll(/<li class="game"><span>([^<]+)<\/span><\/li>/g)].map((m) => unescape(m[1]));
    expect(names).toEqual(games);
    const ruleList = section(HOME, "rules");
    expect(ruleList.match(/<li>/g)).toHaveLength(rules.length);
    expect(ruleList).toContain("<code>/report</code>");
    // a hostile rule renders as text
    const html = render(RuleList({ items: [{ lead: ["Be <b>kind</b>."], body: ["Use ", { code: "<i>" }, "."] }] }));
    expect(html).toBe("<ol class=\"rules\"><li><span><strong>Be &lt;b&gt;kind&lt;/b&gt;.</strong> Use <code>&lt;i&gt;</code>.</span></li></ol>");
  });

  test("every join button goes to the invite, and the counts come from Discord", () => {
    const joins = [...HOME.matchAll(/<a [^>]*data-join=""[^>]*>/g)].map((m) => m[0]);
    expect(joins.length).toBeGreaterThanOrEqual(3);
    for (const a of joins) expect(a).toContain(INVITE_URL ? `href="${INVITE_URL}"` : 'aria-disabled="true"');
  });

  test("the live section is hidden until the client fills it", () => {
    const live = section(HOME, "live");
    expect(live.startsWith('<section id="live" hidden="">')).toBe(true);
    for (const block of ["live-events", "live-houses", "live-games", "live-tally"]) {
      expect(live, block).toMatch(new RegExp(`id="${block}" hidden=""`));
    }
    expect(HOME).toMatch(/<p class="now label" id="now" hidden="">/);
    // between "Monkey SMP" and "House rules", outside the numbered sequence
    expect(HOME.indexOf('<section id="smp">')).toBeLessThan(HOME.indexOf('<section id="live"'));
    expect(HOME.indexOf('<section id="live"')).toBeLessThan(HOME.indexOf('<section id="rules">'));
    expect(live).toMatch(/<div class="ledger-head"><h2>This week<\/h2>/);
    expect(live).not.toMatch(/<p class="label">\d\d\b/);
    expect(live).not.toMatch(/<li|<img/); // no placeholder content, no avatars
    expect(HOME).toContain('href="#live"');
  });

  test("the header mirrors lscaturchio.xyz", () => {
    for (const [name, page] of Object.entries(PAGES)) {
      const header = tag(page, /<header class="top">[\s\S]*?<\/header>/);
      const brand = tag(header, /<a class="brand-home" href="https:\/\/lscaturchio\.xyz"[^>]*>[\s\S]*?<\/a>/);
      expect(brand, name).toMatch(/<img class="wordmark" src="\/img\/cursive\.svg" alt="Lorenzo Scaturchio"/);
      expect(brand, name).toContain('<span class="brand-name">Lorenzo Scaturchio</span>');
      // the icon names the theme you are in, as on the main site: sun first, moon stacked on it
      expect(header.indexOf('class="sun"'), name).toBeLessThan(header.indexOf('class="moon"'));
      expect(header, name).toContain('data-theme-toggle=""');
      expect(header, name).toContain('aria-label="Toggle theme"');
      // One Pen Rule: the header carries no filled button, the masthead keeps the only fill
      expect(header, name).not.toMatch(/class="btn[ "]/);
    }
    const header = tag(HOME, /<header class="top">[\s\S]*?<\/header>/);
    const order = ['class="brand"', '<nav class="sections" aria-label="Primary">', 'class="controls"'].map((s) => header.indexOf(s));
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(order[0]).toBeGreaterThanOrEqual(0);
    expect(header).toMatch(/<a class="btn-quiet" data-join/);
  });

  test("the footer is the site map and links back", () => {
    const footer = tag(HOME, /<footer>[\s\S]*?<\/footer>/);
    expect(footer).toContain('<h2 class="card-title">');
    const columns = [...footer.matchAll(/<nav aria-label="([^"]+)"><h3 class="label">([^<]+)<\/h3>/g)];
    expect(columns.length).toBeGreaterThanOrEqual(2);
    for (const [, label, heading] of columns) expect(label).toBe(heading);
    expect(footer).toMatch(/<a class="ink" href="https:\/\/lscaturchio\.xyz" rel="author">/);
    expect((footer.match(/class="ink"/g) ?? []).length).toBe((footer.match(/<li>/g) ?? []).length); // every link gets the pen
    const legal = tag(footer, /<div class="foot-legal">[\s\S]*?<\/div>/);
    // fine print is sentence case, not a wall label: a whole line in tracked capitals is hard to read
    expect(legal.match(/<p class="fine">/g)).toHaveLength(2);
    expect(legal).toContain("Not affiliated with Discord");
    expect(legal).not.toContain('class="label"');
  });
});

describe("every page", () => {
  test("no footer promises a site without cookies; the page that ends the sign-in says so", async () => {
    for (const [name, page] of Object.entries(PAGES)) {
      const legal = unescape(tag(page, /<div class="foot-legal">[\s\S]*?<\/div>/));
      expect(legal, name).toContain('<p class="fine">This page doesn\'t track you.</p>');
      expect(legal.toLowerCase(), name).not.toContain("cookie");
    }
    const linked = renderToStaticMarkup(await LinkedRolePage({ searchParams: Promise.resolve({}) }));
    expect(tag(linked, /<div class="foot-legal">[\s\S]*?<\/div>/)).toContain("sets one cookie");
  });

  test("nothing inline but the JSON-LD data block: no styles, handlers or scripts", async () => {
    const linked = renderToStaticMarkup(await LinkedRolePage({ searchParams: Promise.resolve({ r: "ok" }) }));
    for (const [name, page] of Object.entries({ ...PAGES, linked })) {
      expect(page, name).not.toMatch(/<style|\sstyle="/);
      expect(page, name).not.toMatch(/\son[a-z]+=/);
      for (const m of page.matchAll(/<script\b([^>]*)>/g)) expect(m[1], name).toContain('type="application/ld+json"');
      expect(page, name).not.toContain("github" + ".io"); // split: the bot tests ban the literal in site/
      expect(page, name).not.toContain("fonts.googleapis" + ".com");
    }
    expect(HOME.match(/<script\b/g)).toHaveLength(1);
  });

  test("absolute links to this site use the one canonical origin", () => {
    const urls = new Set([...HOME.matchAll(/"(https:\/\/discord\.lscaturchio\.xyz[^"]*)"/g)].map((m) => m[1]));
    expect(urls.size).toBeGreaterThan(0);
    for (const u of urls) expect(u.startsWith(CANONICAL)).toBe(true);
  });

  test("local links are root-absolute, so they work at any depth", () => {
    for (const [name, page] of Object.entries(PAGES)) {
      for (const m of page.matchAll(/(?:src|href)="([^"#]+)"/g)) {
        expect(m[1].startsWith("/") || m[1].startsWith("https://"), `${name}: ${m[1]}`).toBe(true);
      }
    }
  });
});

describe("metadata", () => {
  test("canonical URL, Open Graph and Twitter cards", () => {
    expect(homeMeta.alternates?.canonical).toBe(CANONICAL);
    const og = homeMeta.openGraph as Record<string, unknown> & { images: Record<string, unknown>[] };
    const tw = homeMeta.twitter as Record<string, unknown> & { images: Record<string, unknown>[] };
    expect(og.url).toBe(CANONICAL);
    for (const key of ["title", "description", "siteName", "type"]) expect(String(og[key] ?? "").trim(), key).not.toBe("");
    expect(og.images[0].url).toBe(`${CANONICAL}img/og.png`); // crawlers need an absolute URL
    expect([og.images[0].width, og.images[0].height]).toEqual([1200, 630]);
    expect(String(og.images[0].alt).trim()).not.toBe("");
    expect(tw.card).toBe("summary_large_image");
    expect(tw.images[0].url).toBe(og.images[0].url);
    expect(tw.images[0].alt).toBe(og.images[0].alt);
    expect(tw.description).toBe(og.description);
    expect(String(og.description).length).toBeLessThanOrEqual(200);
    expect(TITLE.length).toBeLessThanOrEqual(70);
    expect(homeMeta.description).toBe(DESCRIPTION);
  });

  test("the JSON-LD is a minimal Organization and WebSite", () => {
    const blocks = [...HOME.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)];
    expect(blocks).toHaveLength(1);
    const data = JSON.parse(blocks[0][1]);
    expect(data).toEqual(JSON_LD);
    const types = Object.fromEntries(data["@graph"].map((n: { "@type": string }) => [n["@type"], n]));
    expect(Object.keys(types).sort()).toEqual(["Organization", "WebSite"]);
    expect(types.WebSite.url).toBe(CANONICAL);
    expect(types.Organization.url).toBe(CANONICAL);
    expect(types.Organization.logo.startsWith(`${CANONICAL}img/`)).toBe(true);
    expect(existsSync(path.join(__dirname, "../../public", types.Organization.logo.slice(CANONICAL.length)))).toBe(true);
  });

  test("the pages that should not be indexed say so", () => {
    expect(notFoundMeta.robots).toEqual({ index: false, follow: true });
    expect(String((notFoundMeta.title as { absolute: string }).absolute)).toContain("404");
    expect(appealMeta.robots).toEqual({ index: false, follow: false });
    expect(joinMeta.robots).toEqual({ index: false, follow: true });
    expect(joinMeta.alternates?.canonical).toBe(CANONICAL);
    expect(homeMeta.robots).toBeUndefined();
  });
});

describe("the other pages", () => {
  test("404", () => {
    expect(NOT_FOUND.match(/<h1/g)).toHaveLength(1);
    expect(NOT_FOUND).toContain('href="/"');
  });

  test("/join, when it renders, sends to the invite or explains", () => {
    expect(JOIN.match(/<h1/g)).toHaveLength(1);
    if (INVITE_URL) expect(JOIN).toMatch(new RegExp(`<a class="btn btn-lg" data-join="" href="${INVITE_URL}"`));
    else expect(JOIN).toContain("The invite link isn&#x27;t set up yet.");
    expect(JOIN).not.toMatch(/http-equiv/i); // no meta refresh: the redirect is next.config.ts's
  });

  test("the appeal form is labelled and never submits natively", () => {
    expect(APPEAL.match(/<h1/g)).toHaveLength(1);
    const form = tag(APPEAL, /<form\b[^>]*>[\s\S]*?<\/form>/);
    const open = tag(form, /<form\b[^>]*>/);
    expect(open).not.toContain("action=");
    expect(open).toContain('noValidate=""');
    const names = [...form.matchAll(/<(?:input|textarea)\b[^>]*\bname="([^"]+)"/g)].map((m) => m[1]);
    expect(names).toEqual(FIELDS.map((f) => f.name));
    for (const f of FIELDS) {
      expect(form, f.name).toContain(`<label class="field-label" for="${f.name}">`);
      expect(form, f.name).toContain(`id="${f.name}-error"`);
      expect(form, f.name).toContain(`aria-describedby="${f.name}-hint ${f.name}-error"`);
      const field = tag(form, new RegExp(`<(?:input|textarea)\\b[^>]*id="${f.name}"[^>]*>`));
      expect(field.includes('required=""'), f.name).toBe(f.required);
      if (f.max) expect(field, f.name).toContain(`maxLength="${f.max}"`);
    }
    expect(APPEAL).toContain('role="alert"');
    expect(APPEAL).toContain('id="appeal-done"');
    expect(APPEAL).toContain("<noscript>");
  });
});
