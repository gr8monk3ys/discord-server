// Shared code for the Linked Roles route handlers (src/app/api/linked-role/start and
// callback). No dependencies beyond node:crypto.
//
// What the flow reads and writes is in docs/LINKED_ROLES.md. In short: the member's list of
// connected accounts is read once, reduced to "which game platforms are verified", written to
// Discord as booleans, and forgotten. Tokens are never stored and never logged; account names
// never leave this file.
import crypto from "node:crypto";
import definitions from "./metadata.json";

export { definitions };

export const AUTHORIZE_URL = "https://discord.com/oauth2/authorize";
export const API = "https://discord.com/api/v10";
export const SCOPES = ["identify", "connections", "role_connections.write"];
export const CALLBACK_PATH = "/api/linked-role/callback";

// Every redirect this feature sends goes to AUTHORIZE_URL or to RESULT_PAGE + one of FLAGS.
export const RESULT_PAGE = "/linked-role?r=";
export const FLAGS = ["ok", "cancelled", "expired", "busy", "failed"] as const;
export type Flag = (typeof FLAGS)[number];

// __Host-: the browser only accepts it with Secure, Path=/ and no Domain, so a sibling
// subdomain cannot plant one.
export const COOKIE = "__Host-fd_linked_role";
export const STATE_TTL = 600; // seconds between pressing Link and coming back from Discord
export const STATE_SHAPE = /^[A-Za-z0-9_-]{43}$/; // 32 random bytes, base64url
export const CODE_SHAPE = /^[A-Za-z0-9._~-]{1,512}$/;
const MIN_SECRET = 32;
const FETCH_TIMEOUT = 5000;
const MAX_CONNECTIONS = 200; // entries of the reply looked at; Discord offers ~25 services

export const BOOLEAN_EQUAL = 7;

export type Config = { clientId: string; clientSecret: string; stateSecret: string; redirectUri: string };
type Env = Record<string, string | undefined>;
export type FetchLike = (url: string, init: RequestInit & { method: string }) => Promise<{
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}>;
type Definitions = {
  platform_name: string;
  records: { key: string; name: string; description: string; type: number; service?: string; count?: boolean }[];
};

// ------------------------------------------------------------------ settings
function setting(env: Env, name: string): string {
  const value = env[name];
  return typeof value === "string" ? value.trim() : "";
}

/** The four settings, or null when any is missing or unusable (the feature is then off). */
export function readConfig(env: Env): Config | null {
  const clientId = setting(env, "DISCORD_CLIENT_ID");
  const clientSecret = setting(env, "DISCORD_CLIENT_SECRET");
  const stateSecret = setting(env, "LINKED_ROLE_STATE_SECRET");
  const redirectUri = setting(env, "LINKED_ROLE_REDIRECT_URI");
  if (!/^[0-9]{15,22}$/.test(clientId) || !clientSecret || stateSecret.length < MIN_SECRET) return null;
  let url: URL;
  try {
    url = new URL(redirectUri);
  } catch {
    return null;
  }
  // The redirect URI must be this deployment's own callback, exactly as registered with Discord.
  if (url.protocol !== "https:" || url.pathname !== CALLBACK_PATH) return null;
  if (url.search || url.hash || url.username || url.password || url.href !== redirectUri) return null;
  return { clientId, clientSecret, stateSecret, redirectUri };
}

// ------------------------------------------------------------------ state cookie
function sign(secret: string, payload: string): string {
  return crypto.createHmac("sha256", secret).update(payload).digest("base64url");
}

export function sameString(a: unknown, b: unknown): boolean {
  const x = Buffer.from(String(a));
  const y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

export function newState(randomBytes: (n: number) => Buffer = crypto.randomBytes): string {
  return randomBytes(32).toString("base64url");
}

/** Cookie value "state.expiry.signature"; nowMs is the clock in milliseconds. */
export function stateCookieValue(secret: string, state: string, nowMs: number): string {
  const payload = `${state}.${Math.floor(nowMs / 1000) + STATE_TTL}`;
  return `${payload}.${sign(secret, payload)}`;
}

/** The state inside a cookie we signed that has not expired, else null. */
export function readStateCookie(secret: string, value: unknown, nowMs: number): string | null {
  if (typeof value !== "string" || value.length > 200) return null;
  const parts = value.split(".");
  if (parts.length !== 3) return null;
  const [state, expiry, signature] = parts;
  if (!sameString(signature, sign(secret, `${state}.${expiry}`))) return null;
  if (!STATE_SHAPE.test(state) || !/^[0-9]{1,12}$/.test(expiry)) return null;
  if (Number(expiry) * 1000 <= nowMs) return null;
  return state;
}

export function setCookieHeader(value: string): string {
  return `${COOKIE}=${value}; Max-Age=${STATE_TTL}; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

export function clearCookieHeader(): string {
  return `${COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

/** Our cookie from a raw Cookie header (the first one wins). */
export function cookieFrom(header: unknown): string | null {
  if (typeof header !== "string" || header.length > 8192) return null;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq).trim() === COOKIE) return part.slice(eq + 1).trim();
  }
  return null;
}

// ------------------------------------------------------------------ the mapping
function plural(n: number): string {
  if (n === 0) return "No platforms linked";
  return n === 1 ? "1 platform linked" : `${n} platforms linked`;
}

/**
 * What is written to Discord for one member, from their /users/@me/connections reply.
 * A platform counts only when Discord marks the connection verified and not revoked.
 * Nothing but the service type is read: account names and ids never reach the result.
 */
export function computeRoleConnection(connections: unknown, defs: Definitions = definitions as Definitions) {
  const verified = new Set<string>();
  const list = Array.isArray(connections) ? connections.slice(0, MAX_CONNECTIONS) : [];
  for (const c of list) {
    if (c && typeof c === "object" && typeof c.type === "string" && c.verified === true && c.revoked !== true) {
      verified.add(c.type);
    }
  }
  const metadata: Record<string, string> = {};
  let count = 0;
  for (const record of defs.records) {
    if (!record.service) continue;
    const has = verified.has(record.service);
    if (has) count += 1;
    // Discord stores booleans as the integers 1 and 0, sent as strings.
    metadata[record.key] = has ? "1" : "0";
  }
  for (const record of defs.records) {
    if (record.count) metadata[record.key] = String(count);
  }
  return { platform_name: defs.platform_name, platform_username: plural(count), metadata };
}

// ------------------------------------------------------------------ Discord
export class DiscordError extends Error {
  stage: string;
  status: number;
  constructor(stage: string, status: number) {
    super(`${stage}: ${status}`);
    this.stage = stage;
    this.status = status;
  }
}

async function call(fetchImpl: FetchLike, stage: string, url: string, options: RequestInit & { method: string }) {
  let response;
  try {
    response = await fetchImpl(url, { ...options, redirect: "error", signal: AbortSignal.timeout(FETCH_TIMEOUT) });
  } catch {
    throw new DiscordError(stage, 0); // network error or timeout; the cause may hold the URL only
  }
  if (!response.ok) throw new DiscordError(stage, response.status);
  try {
    return await response.json();
  } catch {
    throw new DiscordError(stage, -1);
  }
}

/** Exchange the authorization code; returns the access token (kept in memory only). */
export async function exchangeCode(fetchImpl: FetchLike, config: Config, code: string): Promise<string> {
  const basic = Buffer.from(`${config.clientId}:${config.clientSecret}`).toString("base64");
  const body = new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: config.redirectUri });
  const reply = (await call(fetchImpl, "token", `${API}/oauth2/token`, {
    method: "POST",
    headers: { Authorization: `Basic ${basic}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  })) as Record<string, unknown> | null;
  const token = reply && reply.access_token;
  const granted = reply && typeof reply.scope === "string" ? reply.scope.split(" ") : [];
  if (typeof token !== "string" || !token || !/^bearer$/i.test(String(reply && reply.token_type))) {
    throw new DiscordError("token", -1);
  }
  if (!["connections", "role_connections.write"].every((s) => granted.includes(s))) {
    throw new DiscordError("scope", -1);
  }
  return token;
}

export async function fetchConnections(fetchImpl: FetchLike, token: string): Promise<unknown[]> {
  const reply = await call(fetchImpl, "connections", `${API}/users/@me/connections`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!Array.isArray(reply)) throw new DiscordError("connections", -1);
  return reply;
}

export async function writeRoleConnection(fetchImpl: FetchLike, config: Config, token: string, roleConnection: object) {
  await call(fetchImpl, "role-connection", `${API}/users/@me/applications/${config.clientId}/role-connection`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(roleConnection),
  });
}

// ------------------------------------------------------------------ responses
const BASE_HEADERS = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex" };

export function redirect(location: string, extra: Record<string, string> = {}): Response {
  return new Response(null, { status: 302, headers: { ...BASE_HEADERS, ...extra, Location: location } });
}

/** Back to the result page with one flag from FLAGS (anything else becomes "failed"). */
export function finish(flag: string, extra: Record<string, string> = {}): Response {
  return redirect(RESULT_PAGE + ((FLAGS as readonly string[]).includes(flag) ? flag : "failed"), extra);
}

function page(status: number, title: string, text: string, extra: Record<string, string> = {}): Response {
  // Constant text only: nothing from the request is ever written into this page. It has no
  // stylesheet of its own (the site's CSS is a hashed build file); the result page at
  // /linked-role is where every normal outcome lands.
  const html =
    '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<meta name="robots" content="noindex"><meta name="color-scheme" content="light dark">' +
    `<title>${title}</title></head>` +
    '<body><main class="lost"><div class="wrap"><div class="narrow">' +
    `<p class="label">Chill Gamers · Linked roles</p><h1>${title}</h1><p class="lede">${text}</p>` +
    '<hr><nav class="recover"><a class="label" href="/">← Back to the server page</a></nav>' +
    "</div></div></main></body></html>";
  return new Response(html, {
    status,
    headers: { ...BASE_HEADERS, ...extra, "Content-Type": "text/html; charset=utf-8" },
  });
}

export function notSetUp(): Response {
  return page(503, "Linked roles aren't set up yet.", "Nothing is wrong on your side. Check back soon.");
}

export function wrongMethod(): Response {
  return page(405, "That isn't how this link works.", "Start again from the Linked Roles menu in Discord.", {
    Allow: "GET",
  });
}

/** One log line per failure: where and which HTTP status. Never a token, code, body or name. */
export function logFailure(log: (line: string) => void, error: unknown): void {
  if (error instanceof DiscordError) log(`linked-role: ${error.stage} failed (${error.status})`);
  else log("linked-role: unexpected error");
}
