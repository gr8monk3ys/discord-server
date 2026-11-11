// Accessibility regression guard, the same check lscaturchio.xyz runs (e2e/a11y.spec.ts there):
// axe-core on one URL per template, failing on serious or critical WCAG 2.1 A/AA violations.
// axe finds roughly a third of WCAG problems automatically (contrast, labels, landmarks, ARIA
// misuse); it is a floor, not an audit. The two cross-origin fetches are answered here, so nothing
// reaches Discord or GitHub.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import siteConfig from "../src/data/site-config.json";

const PAGES = [
  { name: "home", path: "/" },
  { name: "appeal form", path: "/appeal" },
  { name: "linked role result", path: "/linked-role?r=ok" },
  { name: "not found", path: "/no-such-page" },
];

test.beforeEach(async ({ page }) => {
  await page.route("https://discord.com/api/**", (route) =>
    route.fulfill({ json: { approximate_member_count: 120, approximate_presence_count: 14 } }),
  );
  if (siteConfig.statsUrl) await page.route(siteConfig.statsUrl, (route) => route.fulfill({ status: 404, body: "" }));
});

test.describe("Accessibility (axe-core)", () => {
  for (const { name, path } of PAGES) {
    for (const colorScheme of ["light", "dark"] as const) {
      test(`${name} (${colorScheme}) has no serious or critical WCAG violations`, async ({ page }) => {
        await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
        await page.goto(path, { waitUntil: "networkidle" });

        const results = await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
          .analyze();
        const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");

        if (blocking.length > 0) {
          console.log(
            `AXE_DETAIL ${path} (${colorScheme}):\n` +
              JSON.stringify(
                blocking.map((v) => ({
                  id: v.id,
                  impact: v.impact,
                  help: v.help,
                  nodes: v.nodes.map((n) => ({ target: n.target, summary: n.failureSummary })),
                })),
                null,
                2,
              ),
          );
        }
        expect(blocking).toEqual([]);
      });
    }
  }

  test("home page has one visible h1", async ({ page }) => {
    await page.goto("/", { waitUntil: "networkidle" });
    const h1 = page.locator("h1");
    await expect(h1).toHaveCount(1);
    await expect(h1).toBeVisible();
  });
});
