// The live data check (src/lib/stats.ts): what gets through from stats.json, and the lines
// the page builds from it. Pure functions, a fixed clock.
import { describe, expect, test } from "vitest";
import {
  ago,
  hasContent,
  HOUR_MS,
  houseRows,
  MAX_COUNT,
  MAX_EVENTS,
  readStats,
  seasonLine,
  STALE_MS,
  tickerLine,
  updatedLine,
  dayRows,
} from "./stats";

const NOW = Date.parse("2026-10-29T20:00:00Z");
const iso = (ms: number) => new Date(ms).toISOString();

function raw(extra: Record<string, unknown> = {}) {
  return {
    v: 1,
    generated_at: iso(NOW - 30 * 60 * 1000),
    members: { total: 120, online: 14 },
    events: [
      { title: "Friday game night", start: iso(NOW + 26 * HOUR_MS), kind: "gamenight", game: "Risk of Rain 2" },
      { title: "Helldivers drop", start: iso(NOW + 2 * HOUR_MS), kind: "event", game: null },
    ],
    houses: [
      { name: "Fern", points: 40 },
      { name: "Kestrel", points: 80 },
      { name: "Otter", points: 20 },
    ],
    games: [{ name: "Risk of Rain 2", hours: 12 }],
    week: { messages: 420, voice_hours: 33 },
    matchmaking: { matches_week: 4 },
    ...extra,
  };
}

describe("readStats", () => {
  test("a fresh file gets through, sorted", () => {
    const s = readStats(raw(), NOW)!;
    expect(s.members).toBe(120);
    expect(s.online).toBe(14);
    expect(s.events.map((e) => e.title)).toEqual(["Helldivers drop", "Friday game night"]);
    expect(s.houses.map((h) => h.name)).toEqual(["Kestrel", "Fern", "Otter"]);
    expect(s.games).toEqual([{ name: "Risk of Rain 2", hours: 12 }]);
    expect(s.tally).toEqual([
      [420, "messages this week"],
      [33, "hours in voice this week"],
      [4, "squads matched this week"],
    ]);
    expect(hasContent(s)).toBe(true);
  });

  test("anything not version 1, not an object, stale or from the future is refused", () => {
    for (const bad of [null, [], "x", 1, { ...raw(), v: 2 }, { ...raw(), generated_at: "soon" }]) {
      expect(readStats(bad, NOW)).toBeNull();
    }
    expect(readStats(raw({ generated_at: iso(NOW - STALE_MS) }), NOW)).toBeNull();
    expect(readStats(raw({ generated_at: iso(NOW - STALE_MS + 60000) }), NOW)).not.toBeNull();
    expect(readStats(raw({ generated_at: iso(NOW + 2 * HOUR_MS) }), NOW)).toBeNull();
  });

  test("an old online count is dropped; the member total stays", () => {
    const s = readStats(raw({ generated_at: iso(NOW - 3 * HOUR_MS) }), NOW)!;
    expect(s.online).toBeNull();
    expect(s.members).toBe(120);
  });

  test("IDs, mentions, markup and out-of-range numbers never get through", () => {
    const s = readStats(
      raw({
        members: { total: MAX_COUNT + 1, online: -1 },
        events: [
          { title: "Ping <@1234567890123456>", start: iso(NOW + HOUR_MS), kind: "event" },
          { title: "Night 123456789012345", start: iso(NOW + HOUR_MS), kind: "event" },
          { title: "<b>bold</b>", start: iso(NOW + HOUR_MS), kind: "event" },
          { title: "Mail me@x", start: iso(NOW + HOUR_MS), kind: "event" },
          { title: "Unknown kind", start: iso(NOW + HOUR_MS), kind: "raid" },
          { title: "Long ago", start: iso(NOW - 4 * HOUR_MS), kind: "event" },
          { title: "x".repeat(81), start: iso(NOW + HOUR_MS), kind: "event" },
          { title: "Just started", start: iso(NOW - HOUR_MS), kind: "tournament" },
        ],
        houses: [{ name: "Fern", points: 1.5 }, { name: "Kestrel", points: 3 }],
        games: [{ name: "Zero", hours: 0 }, { name: "<script>", hours: 2 }],
      }),
      NOW,
    )!;
    expect(s.members).toBeNull();
    expect(s.online).toBeNull();
    expect(s.events.map((e) => e.title)).toEqual(["Just started"]);
    expect(s.houses).toEqual([]); // one house left is not a standing
    expect(s.games).toEqual([]);
  });

  test("lists are capped before anything is checked", () => {
    const events = Array.from({ length: MAX_EVENTS + 5 }, (_, i) => ({
      title: `Event ${i}`,
      start: iso(NOW + (i + 1) * HOUR_MS),
      kind: "event",
    }));
    expect(readStats(raw({ events }), NOW)!.events).toHaveLength(MAX_EVENTS);
  });

  test("a file with nothing to show has no content", () => {
    const s = readStats(raw({ events: [], houses: [], games: [] }), NOW)!;
    expect(hasContent(s)).toBe(false);
    const seasonal = readStats(raw({ events: [], houses: [], games: [], seasonal: { name: "Spooky week", ends: iso(NOW + 48 * HOUR_MS) } }), NOW)!;
    expect(hasContent(seasonal)).toBe(true);
    const over = readStats(raw({ seasonal: { name: "Spooky week", ends: iso(NOW - 1) } }), NOW)!;
    expect(over.seasonal).toBeUndefined();
  });
});

describe("the lines the page builds", () => {
  test("house bars: the pen goes to a clear leader only, never thinner than 2%", () => {
    expect(houseRows([{ name: "A", points: 100 }, { name: "B", points: 1 }])).toEqual([
      { name: "A", points: 100, lead: true, width: 100 },
      { name: "B", points: 1, lead: false, width: 2 },
    ]);
    expect(houseRows([{ name: "A", points: 5 }, { name: "B", points: 5 }]).map((r) => r.lead)).toEqual([false, false]);
    expect(houseRows([{ name: "A", points: 0 }, { name: "B", points: 0 }]).map((r) => r.width)).toEqual([0, 0]);
  });

  test("the updated line and the masthead ticker", () => {
    expect(ago(60 * 1000)).toBe("Updated just now");
    expect(ago(30 * 60 * 1000)).toBe("Updated 30 minutes ago");
    expect(ago(5 * HOUR_MS)).toBe("Updated 5 hours ago");
    const s = readStats(raw(), NOW)!;
    expect(updatedLine(s)).toBe("Updated 30 minutes ago. Times are in your time zone.");
    expect(tickerLine(s, NOW)!.startsWith("Next up · Helldivers drop · Today, ")).toBe(true);
    const on = readStats(raw({ events: [{ title: "Live now", start: iso(NOW - 60000), kind: "event" }] }), NOW)!;
    expect(tickerLine(on, NOW)).toBe("On now · Live now");
    const none = readStats(raw({ events: [] }), NOW)!;
    expect(tickerLine(none, NOW)).toBeNull();
  });

  test("day rows carry a machine-readable time for real events only", () => {
    const s = readStats(raw({ seasonal: { name: "Spooky week", ends: iso(NOW + 48 * HOUR_MS) } }), NOW)!;
    const rows = dayRows(s, NOW);
    expect(rows[0]).toMatchObject({ label: "Now", iso: null, title: "Spooky week" });
    expect(rows[1]).toMatchObject({ iso: iso(NOW + 2 * HOUR_MS), title: "Helldivers drop" });
    expect(rows[2].detail).toContain("Game night");
    expect(rows[2].detail).not.toContain("Risk of Rain 2 · Risk"); // the game only once
  });

  test("the season countdown", () => {
    const start = "2026-11-08T01:00:00Z";
    expect(seasonLine(start, "Season I", Date.parse(start) - 9.5 * 24 * HOUR_MS)).toBe("9 days to go\nSeason I");
    expect(seasonLine(start, "Season I", Date.parse(start) - 30 * HOUR_MS)).toBeNull(); // the date says enough
    expect(seasonLine(start, "Season I", Date.parse(start))).toBe("Season I is open");
    expect(seasonLine("not a date", "Season I", NOW)).toBeNull();
  });
});
