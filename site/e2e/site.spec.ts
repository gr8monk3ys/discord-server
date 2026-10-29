// The production build in a real browser: headers and the nonce CSP, the /join redirect,
// the page without JavaScript, the live section with fresh, stale and failed data, the theme
// toggle, the scroll-spy and the appeal form. Every request off this origin is answered by a
// route here, so nothing reaches Discord or GitHub.
import { expect, test, type Page } from "@playwright/test";
import siteConfig from "../src/data/site-config.json";

const INVITE = `https://discord.gg/${siteConfig.inviteCode}`;
const HOUR = 3600 * 1000;

function stats(ageMs: number) {
  const now = Date.now();
  return {
    v: 1,
    generated_at: new Date(now - ageMs).toISOString(),
    members: { total: 120, online: 14 },
    events: [{ title: "Friday game night", start: new Date(now + 26 * HOUR).toISOString(), kind: "gamenight", game: null }],
    houses: [
      { name: "Fern", points: 40 },
      { name: "Kestrel", points: 80 },
    ],
    games: [{ name: "Risk of Rain 2", hours: 12 }],
    week: { messages: 420, voice_hours: 33 },
  };
}

/** Answer the two cross-origin fetches; `live` is the stats.json reply (an object, or a status). */
async function offline(page: Page, live: object | number) {
  await page.route("https://discord.com/api/**", (route) =>
    route.fulfill({ json: { approximate_member_count: 120, approximate_presence_count: 14 } }),
  );
  await page.route(siteConfig.statsUrl, (route) =>
    typeof live === "number" ? route.fulfill({ status: live, body: "" }) : route.fulfill({ json: live }),
  );
}

/** Collect CSP violations the page reports, and console errors. */
function watch(page: Page) {
  const problems: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" && !/Failed to load resource: the server responded with a status of 404/.test(msg.text())) {
      problems.push(msg.text());
    }
  });
  page.on("pageerror", (err) => problems.push(String(err)));
  return problems;
}

test.describe("headers and CSP", () => {
  test("every page gets the security headers and a fresh nonce its scripts carry", async ({ page, request }) => {
    const nonces = new Set<string>();
    for (const url of ["/", "/appeal", "/linked-role", "/nope"]) {
      const res = await request.get(url);
      const h = res.headers();
      expect(h["x-content-type-options"], url).toBe("nosniff");
      expect(h["referrer-policy"], url).toBe("strict-origin-when-cross-origin");
      expect(h["x-frame-options"], url).toBe("DENY");
      expect(h["permissions-policy"], url).toContain("camera=()");
      expect(h["x-powered-by"], url).toBeUndefined();
      const csp = h["content-security-policy"];
      const nonce = csp.match(/script-src 'self' 'nonce-([A-Za-z0-9+/=]+)'/)?.[1];
      expect(nonce, url).toBeTruthy();
      expect(csp).not.toContain("unsafe");
      expect(csp).toContain("form-action 'none'");
      expect(csp).toContain("frame-ancestors 'none'");
      nonces.add(nonce!);
      // every script tag in the HTML is either external or carries this response's nonce
      const html = await res.text();
      for (const [tag] of html.matchAll(/<script\b[^>]*>/g)) {
        if (tag.includes('type="application/ld+json"')) continue;
        if (!tag.includes(" src=")) expect(tag, url).toContain(`nonce="${nonce}"`);
      }
      expect(html, url).not.toMatch(/\sstyle="/);
    }
    expect(nonces.size).toBe(4);

    await offline(page, stats(30 * 60 * 1000));
    const problems = watch(page);
    for (const url of ["/", "/appeal", "/linked-role?r=ok", "/nope"]) {
      await page.goto(url);
      await page.waitForLoadState("networkidle");
    }
    expect(problems.filter((p) => /Content Security Policy|Refused to/.test(p))).toEqual([]);
    expect(problems).toEqual([]);
  });

  test("the noindex pages say so in a header, and images get the asset policy", async ({ request }) => {
    expect((await request.get("/appeal")).headers()["x-robots-tag"]).toBe("noindex, nofollow");
    expect((await request.get("/linked-role")).headers()["x-robots-tag"]).toBe("noindex, nofollow");
    expect((await request.get("/api/appeal")).headers()["x-robots-tag"]).toBe("noindex, nofollow");
    expect((await request.get("/")).headers()["x-robots-tag"]).toBeUndefined();
    const og = await request.get("/img/og.png");
    expect(og.status()).toBe(200);
    expect(og.headers()["content-security-policy"]).toBe("default-src 'none'; frame-ancestors 'none'");
    expect(og.headers()["cache-control"]).toContain("max-age=2592000");
    expect((await request.get("/img/favicon.png")).status()).toBe(200);
  });
});

test.describe("routes", () => {
  test("/join goes straight to the invite", async ({ request }) => {
    for (const url of ["/join", "/join/"]) {
      const res = await request.get(url, { maxRedirects: 0 });
      expect([307, 308], url).toContain(res.status());
      if (res.status() === 307) expect(res.headers().location).toBe(INVITE);
    }
  });

  test("a missing page is a 404 with the house page", async ({ page }) => {
    const res = await page.goto("/nope/deeper");
    expect(res?.status()).toBe(404);
    await expect(page).toHaveTitle("Page not found (404) · Chill Gamers");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Nothing at this address");
  });

  test("the API routes answer without their secrets the way they always did", async ({ request }) => {
    expect((await request.get("/api/appeal")).status()).toBe(405);
    const appeal = await request.post("/api/appeal", { data: {}, headers: { origin: "https://discord.lscaturchio.xyz" } });
    expect(appeal.status()).toBe(503);
    const start = await request.get("/api/linked-role/start", { maxRedirects: 0 });
    expect(start.status()).toBe(503);
    expect(await start.text()).toContain("aren't set up yet");
    expect((await request.post("/api/linked-role/callback")).status()).toBe(405);
  });
});

test.describe("the home page", () => {
  test("reads in full without JavaScript", async ({ browser }) => {
    const context = await browser.newContext({ javaScriptEnabled: false });
    const page = await context.newPage();
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Chill gamers.");
    await expect(page.locator("#rules ol.rules li").first()).toBeVisible();
    await expect(page.locator("#games li").first()).toBeVisible();
    await expect(page.locator("#season")).toContainText("Season I");
    await expect(page.locator("#live")).toBeHidden();
    await expect(page.locator("#now")).toBeHidden();
    await expect(page.locator("[data-theme-toggle]")).toBeHidden(); // a button that would do nothing
    await expect(page.locator("a[data-join]").first()).toHaveAttribute("href", INVITE);
    await context.close();
  });

  test("the live section opens with fresh data", async ({ page }) => {
    await offline(page, stats(30 * 60 * 1000));
    await page.goto("/");
    const live = page.locator("#live");
    await expect(live).toBeVisible();
    await expect(page.locator("#live-updated")).toHaveText("Updated 30 minutes ago. Times are in your time zone.");
    await expect(page.locator("#live-events-list li")).toHaveCount(1);
    await expect(page.locator("#live-events-list time")).toHaveAttribute("datetime", /Z$/);
    await expect(page.locator("#live-houses-list li")).toHaveCount(2);
    await expect(page.locator("#live-houses-list .bar-fill").first()).toHaveAttribute("style", "width: 100%;");
    await expect(page.locator("#live-tally")).toContainText("420");
    await expect(page.locator("#now")).toContainText("Friday game night");
    await expect(page.locator("#count-members")).toContainText("120");
  });

  for (const [name, reply] of [
    ["stale", stats(49 * HOUR)],
    ["failed", 500],
    ["not JSON", 200],
  ] as const) {
    test(`the live section stays hidden when the data is ${name}`, async ({ page }) => {
      if (name === "not JSON") {
        await page.route(siteConfig.statsUrl, (route) => route.fulfill({ status: 200, body: "<html>" }));
        await page.route("https://discord.com/api/**", (route) => route.fulfill({ status: 500, body: "" }));
      } else {
        await offline(page, reply as object | number);
      }
      const problems = watch(page);
      await page.goto("/");
      await page.waitForLoadState("networkidle");
      await expect(page.locator("#live")).toBeHidden();
      await expect(page.locator("#now")).toBeHidden();
      expect(problems.filter((p) => !/status of 500/.test(p))).toEqual([]);
    });
  }

  test("the theme toggle switches and remembers", async ({ page }) => {
    await offline(page, 404);
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/");
    const html = page.locator("html");
    const toggle = page.locator("[data-theme-toggle]");
    await expect(toggle).toBeVisible();
    await toggle.click();
    await expect(html).toHaveAttribute("data-theme", "dark");
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await page.reload();
    await expect(html).toHaveAttribute("data-theme", "dark");
    await toggle.click();
    await expect(html).toHaveAttribute("data-theme", "light");
  });

  test("the nav marks the section being read", async ({ page }) => {
    await offline(page, 404);
    await page.goto("/");
    await page.locator("#rules").scrollIntoViewIfNeeded();
    await page.evaluate(() => document.getElementById("rules")!.scrollIntoView({ block: "center" }));
    await expect(page.locator('nav.sections a[href="#rules"]')).toHaveAttribute("aria-current", "location");
    await expect(page.locator('nav.sections a[aria-current="location"]')).toHaveCount(1);
  });

  test("reduced motion leaves everything in place and visible", async ({ page }) => {
    await offline(page, stats(30 * 60 * 1000));
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");
    await expect(page.locator("#live")).toBeVisible();
    const behavior = await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior);
    expect(behavior).toBe("auto");
  });
});

test.describe("the appeal page", () => {
  test("checks the fields on the page, then says appeals aren't open without the secrets", async ({ page }) => {
    await page.goto("/appeal");
    await page.getByRole("button", { name: "Send appeal" }).click();
    await expect(page.locator("#user_id-error")).toHaveText("This one is needed.");
    await expect(page.locator("#form-error")).toHaveText("Please check the marked fields.");
    await page.fill("#user_id", "1234567890" + "12345678");
    await page.fill("#code", "td7y-ul66-oych-iztl");
    await page.fill("#what", "I spammed.");
    await page.fill("#why", "I read the rules.");
    await page.getByRole("button", { name: "Send appeal" }).click();
    await expect(page.locator("#form-error")).toHaveText(
      "Appeals aren't open on this site right now. Please try again another day.",
    );
    await expect(page.locator("#appeal-done")).toBeHidden();
  });
});
