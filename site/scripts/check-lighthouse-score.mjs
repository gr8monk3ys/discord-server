import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { devNull, tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

// Lighthouse score floors, run against a production build in CI. Ported from lscaturchio.xyz
// (scripts/check-lighthouse-score.mjs there), with the same rules:
//
// Every floor is a MEASURED value, not an aspiration. Performance sits a couple of points below
// the lowest score seen across repeated runs; the other three categories sit at the value the
// site actually reaches. A floor set at an unmet number keeps the check red and therefore
// ignored; a floor that flakes on runner noise gets bypassed. Raise a floor whenever the real
// score improves.
//
// The runs use Lighthouse's desktop preset: mobile throttling is too noisy on shared runners to
// gate on.
const SCORE_FLOORS = {
  performance: 97,
  accessibility: 100,
  bestPractices: 100,
  seo: 100,
};

// Per-route exceptions, each naming its cause. /appeal is noindex on purpose (the ban DM links
// it; it is not for search), and Lighthouse's is-crawlable audit takes SEO down for that alone.
const ROUTE_FLOOR_OVERRIDES = {
  "/appeal": { seo: 66 },
};

// Document byte budget for the home route, compressed, as a browser receives it. Anything that
// grows the HTML shell (inlined CSS, serialized data, a new section) is invisible to the scores
// until it moves LCP, so the size is gated directly. About 25% above the measured value; the
// measured number is printed on every run so drift shows before it fails.
const HOME_DOCUMENT_BYTE_CAP = 16_000; // measured 12,948 on the CI runner, 2026-11-11 (Windows curl reads ~17.7 KB)

const MAX_ATTEMPTS = 4;
const isWindows = process.platform === "win32";
const artifactDir = join(process.cwd(), "artifacts", "lighthouse");
const [baseUrl, ...routes] = process.argv.slice(2);

if (!baseUrl || routes.length === 0) {
  console.error("Usage: node scripts/check-lighthouse-score.mjs <baseUrl> <route...>");
  process.exit(1);
}

const outputDir = mkdtempSync(join(tmpdir(), "discord-site-lighthouse-"));

try {
  mkdirSync(artifactDir, { recursive: true });

  for (const route of routes) {
    const url = new URL(route, baseUrl).toString();
    const floors = { ...SCORE_FLOORS, ...ROUTE_FLOOR_OVERRIDES[route] };
    warmRoute(url);
    let bestAttempt = null;
    let attemptsRun = 0;

    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
      attemptsRun = attempt;
      const reportPath = join(outputDir, `${artifactStem(route)}-attempt-${attempt}.json`);
      const scores = runLighthouse(url, reportPath);

      console.log(`[lighthouse] ${route} attempt ${attempt} -> ${JSON.stringify(scores)}`);

      if (!bestAttempt || totalScore(scores) > totalScore(bestAttempt.scores)) {
        bestAttempt = { attempt, scores, reportPath };
      }

      if (Object.entries(scores).every(([k, score]) => score >= (floors[k] ?? 100))) {
        break;
      }
    }

    const failures = Object.entries(bestAttempt.scores).filter(([k, score]) => score < (floors[k] ?? 100));
    copyFileSync(bestAttempt.reportPath, join(artifactDir, `${artifactStem(route)}.json`));

    if (failures.length > 0) {
      console.error(
        `[lighthouse] ${route} fell below its score floors after ${attemptsRun} attempt(s): ${failures
          .map(([category, score]) => `${category}=${score}`)
          .join(", ")}`,
      );
      logFailureDiagnostics(bestAttempt.reportPath);
      process.exit(1);
    }
  }

  checkDocumentByteBudget(new URL("/", baseUrl).toString());
} finally {
  rmSync(outputDir, { recursive: true, force: true });
}

function artifactStem(route) {
  return route === "/" ? "root" : route.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "");
}

function runLighthouse(url, reportPath) {
  const result = spawnSync(
    "npx",
    [
      "-y",
      "lighthouse",
      url,
      "--preset=desktop",
      "--quiet",
      `"--chrome-flags=--headless=new --no-sandbox --disable-dev-shm-usage"`,
      "--only-categories=performance,accessibility,best-practices,seo",
      "--output=json",
      `--output-path=${reportPath}`,
    ],
    // A shell so the quoted Chrome flags stay one argument, and so Windows finds npx.cmd.
    { encoding: "utf-8", shell: true },
  );

  if (result.error) {
    console.error(`Failed to run Lighthouse for ${url}:`, result.error.message);
    process.exit(1);
  }

  if (result.status !== 0) {
    if (result.stdout) process.stdout.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
    // Lighthouse on Windows can exit non-zero when it fails to delete Chrome's temp profile
    // (EPERM) after writing a good report; trust the report when there is one.
    if (!isWindows) process.exit(result.status ?? 1);
  }

  const report = JSON.parse(readFileSync(reportPath, "utf-8"));
  return {
    performance: Math.round(report.categories.performance.score * 100),
    accessibility: Math.round(report.categories.accessibility.score * 100),
    bestPractices: Math.round(report.categories["best-practices"].score * 100),
    seo: Math.round(report.categories.seo.score * 100),
  };
}

function warmRoute(url) {
  spawnSync("curl", ["-fsSLo", devNull, url], { stdio: "ignore" });
}

function totalScore(scores) {
  return Object.values(scores).reduce((sum, score) => sum + score, 0);
}

function checkDocumentByteBudget(url) {
  const result = spawnSync("curl", ["--compressed", "-sSo", devNull, "-w", "%{size_download}", url], {
    encoding: "utf-8",
  });
  const bytes = Number.parseInt(result.stdout, 10);

  if (result.status !== 0 || !Number.isFinite(bytes) || bytes <= 0) {
    console.error(`[lighthouse] could not measure the document size of ${url}: ${result.stderr || result.stdout}`);
    process.exit(1);
  }

  console.log(
    `[lighthouse] home document: ${bytes} bytes compressed (cap ${HOME_DOCUMENT_BYTE_CAP}, ${Math.round(
      (bytes / HOME_DOCUMENT_BYTE_CAP) * 100,
    )}% of budget)`,
  );

  if (bytes > HOME_DOCUMENT_BYTE_CAP) {
    console.error(
      `[lighthouse] home document is ${bytes} bytes compressed, over the ${HOME_DOCUMENT_BYTE_CAP} byte cap. ` +
        "Something grew the HTML shell. Shrink it, or raise the cap deliberately with the measured " +
        "number in the commit message.",
    );
    process.exit(1);
  }
}

function logFailureDiagnostics(reportPath) {
  const report = JSON.parse(readFileSync(reportPath, "utf-8"));
  const metrics = report.audits.metrics?.details?.items?.[0];

  if (metrics) {
    const ms = (value) => `${Math.round(value)}ms`;
    console.error(
      `[lighthouse] metrics: fcp=${ms(metrics.firstContentfulPaint)} lcp=${ms(metrics.largestContentfulPaint)} tbt=${ms(metrics.totalBlockingTime)} si=${ms(metrics.speedIndex)} cls=${metrics.cumulativeLayoutShift}`,
    );
  }

  const opportunities = Object.values(report.audits)
    .filter((audit) => audit.details?.type === "opportunity" && typeof audit.numericValue === "number")
    .sort((left, right) => right.numericValue - left.numericValue)
    .slice(0, 5)
    .map((audit) => `${audit.id}:${Math.round(audit.numericValue)}ms`);

  if (opportunities.length > 0) {
    console.error(`[lighthouse] top opportunities: ${opportunities.join(", ")}`);
  }

  // Annotated with each audit's weight, because not every failing audit costs a point
  // (valid-source-maps is weight 0: Next ships no browser source maps in production).
  const weightById = new Map();
  for (const category of Object.values(report.categories)) {
    for (const ref of category.auditRefs) {
      weightById.set(ref.id, (weightById.get(ref.id) ?? 0) + ref.weight);
    }
  }

  const failedAudits = Object.values(report.audits)
    .filter((audit) => audit.score !== null && audit.score < 1 && audit.scoreDisplayMode === "binary")
    .slice(0, 10)
    .map((audit) => {
      const weight = weightById.get(audit.id) ?? 0;
      return weight > 0 ? `${audit.id} (weight ${weight})` : `${audit.id} (weight 0, scores nothing)`;
    });

  if (failedAudits.length > 0) {
    console.error(`[lighthouse] failed binary audits: ${failedAudits.join(", ")}`);
  }
}
