// The live data the bot publishes (stats.json on the site-data branch), checked again here
// field by field: only whole numbers in range and short plain strings get through, and a
// string that looks like an ID or a mention is dropped even though the bot and the
// publisher (server/publish_site_data.py, server/site_schema.py) already refuse those.
// Everything is rendered as React text, never as HTML. Pure functions, no DOM.

export const TIMEOUT_MS = 6000;
export const HOUR_MS = 3600 * 1000;
export const STALE_MS = 48 * HOUR_MS; // older than this: the live section stays hidden
export const ONLINE_FRESH_MS = 2 * HOUR_MS; // an online count older than this is not worth showing
export const STARTED_GRACE_MS = 3 * HOUR_MS; // an event that started this recently still shows, as "Now"
export const MAX_COUNT = 10000000;
export const MAX_EVENTS = 8;
export const MAX_HOUSES = 8;
export const MAX_GAMES = 5;
export const MAX_TITLE = 80;
export const MAX_NAME = 40;

export const KINDS = { gamenight: "Game night", tournament: "Tournament", event: "Server event" } as const;
export type Kind = keyof typeof KINDS;

export type LiveEvent = { title: string; start: number; kind: Kind; game: string | null };
export type House = { name: string; points: number };
export type PlayedGame = { name: string; hours: number };
export type Tally = [number, string];

export type Stats = {
  generated: number;
  age: number;
  members: number | null;
  online: number | null;
  events: LiveEvent[];
  seasonal?: { name: string; ends: number };
  houses: House[];
  games: PlayedGame[];
  tally: Tally[];
};

type Obj = Record<string, unknown>;

export function count(x: unknown): number | null {
  return typeof x === "number" && isFinite(x) && Math.floor(x) === x && x >= 0 && x <= MAX_COUNT ? x : null;
}

export function text(x: unknown, max: number): string | null {
  if (typeof x !== "string") return null;
  const s = x.trim();
  if (!s || s.length > max || /\d{15,}/.test(s) || /[@<>]/.test(s)) return null;
  return s;
}

export function when(x: unknown): number | null {
  if (typeof x !== "string" || x.length > 40) return null;
  const t = Date.parse(x);
  return isFinite(t) ? t : null;
}

const list = (x: unknown, max: number): unknown[] => (Array.isArray(x) ? x.slice(0, max) : []);
const obj = (x: unknown): Obj => (x && typeof x === "object" && !Array.isArray(x) ? (x as Obj) : {});
const isKind = (k: unknown): k is Kind => typeof k === "string" && Object.prototype.hasOwnProperty.call(KINDS, k);

export function readStats(raw: unknown, now: number): Stats | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw) || (raw as Obj).v !== 1) return null;
  const r = raw as Obj;
  const generated = when(r.generated_at);
  if (generated === null) return null;
  const age = now - generated;
  if (!(age < STALE_MS) || age < -HOUR_MS) return null; // stale, or a clock that makes no sense

  const members = obj(r.members);
  const out: Stats = {
    generated,
    age: Math.max(age, 0),
    members: count(members.total),
    online: age < ONLINE_FRESH_MS ? count(members.online) : null,
    events: [],
    houses: [],
    games: [],
    tally: [],
  };

  for (const item of list(r.events, MAX_EVENTS)) {
    const e = obj(item);
    const title = text(e.title, MAX_TITLE);
    const start = when(e.start);
    if (!title || start === null || !isKind(e.kind) || start < now - STARTED_GRACE_MS) continue;
    out.events.push({ title, start, kind: e.kind, game: text(e.game, MAX_NAME) });
  }
  out.events.sort((a, b) => a.start - b.start);

  const seasonal = obj(r.seasonal);
  const ends = when(seasonal.ends);
  const name = text(seasonal.name, MAX_NAME);
  if (name && ends !== null && ends > now) out.seasonal = { name, ends };

  for (const item of list(r.houses, MAX_HOUSES)) {
    const h = obj(item);
    const houseName = text(h.name, MAX_NAME);
    const points = count(h.points);
    if (houseName && points !== null) out.houses.push({ name: houseName, points });
  }
  out.houses.sort((a, b) => b.points - a.points);
  if (out.houses.length < 2) out.houses = [];

  for (const item of list(r.games, MAX_GAMES)) {
    const g = obj(item);
    const gameName = text(g.name, MAX_NAME);
    const hours = count(g.hours);
    if (gameName && hours) out.games.push({ name: gameName, hours });
  }

  const week = obj(r.week);
  const mm = obj(r.matchmaking);
  const rows: [number | null, string][] = [
    [count(week.messages), "messages this week"],
    [count(week.voice_hours), "hours in voice this week"],
    [count(mm.matches_week), "squads matched this week"],
  ];
  for (const [n, label] of rows) if (n) out.tally.push([n, label]);
  return out;
}

export function fmt(n: number): string {
  try {
    return new Intl.NumberFormat().format(n);
  } catch {
    return String(n);
  }
}

export function dateLabel(t: number, now: number): string {
  if (t <= now) return "Now";
  const d = new Date(t);
  const today = new Date(now);
  const days = Math.round(
    (new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() -
      new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) /
      (24 * HOUR_MS),
  );
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  try {
    return new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" }).format(d);
  } catch {
    return d.toDateString();
  }
}

export function timeLabel(t: number): string {
  const d = new Date(t);
  try {
    return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(d);
  } catch {
    return d.toLocaleTimeString();
  }
}

export function shortDate(t: number): string {
  try {
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(t));
  } catch {
    return new Date(t).toDateString();
  }
}

export function ago(ms: number): string {
  const minutes = Math.round(ms / 60000);
  if (minutes < 2) return "Updated just now";
  if (minutes < 90) return "Updated " + minutes + " minutes ago";
  const hours = Math.round(minutes / 60);
  return "Updated " + hours + " hours ago";
}

/** One row of the "Coming up" ledger. `iso` is set for real events (a <time> element). */
export type DayRow = { label: string; iso: string | null; title: string; detail: string };

export function dayRows(stats: Stats, now: number): DayRow[] {
  const rows: DayRow[] = [];
  if (stats.seasonal) {
    const s = stats.seasonal;
    rows.push({ label: "Now", iso: null, title: s.name, detail: "Seasonal event · ends " + shortDate(s.ends) });
  }
  for (const e of stats.events) {
    let detail = (e.start <= now ? "Started " : "") + timeLabel(e.start) + " · " + KINDS[e.kind];
    if (e.game && e.title.indexOf(e.game) === -1) detail += " · " + e.game;
    rows.push({ label: dateLabel(e.start, now), iso: new Date(e.start).toISOString(), title: e.title, detail });
  }
  return rows;
}

/** House standings: the pen goes to a clear leader only (a tie is drawn as a tie); each bar
 *  is a share of the top score, never thinner than 2%. */
export function houseRows(houses: House[]): { name: string; points: number; lead: boolean; width: number }[] {
  const top = houses.length ? houses[0].points : 0;
  return houses.map((h, i) => ({
    name: h.name,
    points: h.points,
    lead: i === 0 && (houses.length < 2 || h.points > houses[1].points),
    width: top > 0 ? Math.max(2, Math.round((h.points / top) * 100)) : 0,
  }));
}

/** Whether the live section has anything to show. */
export function hasContent(stats: Stats): boolean {
  return Boolean(stats.seasonal || stats.events.length || stats.houses.length || stats.games.length);
}

export function updatedLine(stats: Stats): string {
  return ago(stats.age) + (stats.events.length ? ". Times are in your time zone." : ".");
}

/** The masthead line: the next thing on, or the seasonal event that is running. */
export function tickerLine(stats: Stats, now: number): string | null {
  const next = stats.events[0];
  if (next) {
    return (
      (next.start <= now ? "On now · " : "Next up · ") +
      next.title +
      (next.start <= now ? "" : " · " + dateLabel(next.start, now) + ", " + timeLabel(next.start))
    );
  }
  if (stats.seasonal) return "On now · " + stats.seasonal.name + " · ends " + shortDate(stats.seasonal.ends);
  return null;
}

/** The Monkey SMP season line: a countdown before the opening, then "open". Null leaves
 *  the server-rendered date as it is (it is still true). */
export function seasonLine(startIso: string, base: string, now: number): string | null {
  const start = Date.parse(startIso);
  if (isNaN(start)) return null;
  const left = start - now;
  const days = Math.floor(left / (24 * HOUR_MS));
  if (left <= 0) return "Season I is open";
  if (days >= 2) return days + " days to go\n" + base;
  return null;
}
