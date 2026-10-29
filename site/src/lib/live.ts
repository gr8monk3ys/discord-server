// Browser-side plumbing shared by the islands on the home page: one fetch of the live
// data for everyone who needs it, the ink-motion hook the live section calls when it
// opens, and the "open the live section" request from the masthead line.
import { STATS_URL } from "@/lib/site";
import { readStats, TIMEOUT_MS, type Stats } from "@/lib/stats";

/** JSON from `url`, or null on any failure (offline, blocked, timeout, not JSON). */
export function getJson(url: string): Promise<unknown> {
  const ctrl = typeof AbortController === "function" ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), TIMEOUT_MS) : null;
  return fetch(url, { credentials: "omit", signal: ctrl ? ctrl.signal : undefined })
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null)
    .then((data) => {
      if (timer) clearTimeout(timer);
      return data;
    });
}

export type Loaded = { stats: Stats; now: number };

let statsReady: Promise<Loaded | null> | null = null;

/** The checked live data, fetched once per page view; null when off, missing or stale. */
export function loadStats(): Promise<Loaded | null> {
  if (!statsReady) {
    statsReady =
      !STATS_URL || typeof fetch !== "function"
        ? Promise.resolve(null)
        : getJson(STATS_URL).then((raw) => {
            try {
              const now = Date.now();
              const stats = readStats(raw, now);
              return stats ? { stats, now } : null;
            } catch {
              return null; // a surprise in the data: the page stays as it was
            }
          });
  }
  return statsReady;
}

/** Test hook: forget the cached fetch. */
export function resetStats(): void {
  statsReady = null;
}

// Ink motion (PageMotion) installs the real arm(); until then, and when the reader asked
// for less motion, it does nothing.
let armImpl: (scope: ParentNode) => void = () => {};
export function setArm(fn: (scope: ParentNode) => void): void {
  armImpl = fn;
}
export function arm(scope: ParentNode): void {
  armImpl(scope);
}

// The masthead line links to #live; the section may still be waiting for a quiet moment
// to open, so a click opens it on demand.
const OPEN_EVENT = "chill:open-live";
export function requestOpenLive(): void {
  window.dispatchEvent(new Event(OPEN_EVENT));
}
export function onOpenLive(fn: () => void): () => void {
  window.addEventListener(OPEN_EVENT, fn);
  return () => window.removeEventListener(OPEN_EVENT, fn);
}
