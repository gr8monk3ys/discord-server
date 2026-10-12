// Shared code for the Linked Roles functions (start.js, callback.js). The leading underscore
// keeps Vercel from publishing this file as an endpoint of its own. No dependencies.
//
// What the flow reads and writes is in docs/LINKED_ROLES.md. In short: the member's list of
// connected accounts is read once, reduced to "which game platforms are verified", written to
// Discord as booleans, and forgotten. Tokens are never stored and never logged; account names
// never leave this file.
"use strict";

const crypto = require("node:crypto");
const definitions = require("./metadata.json");

const AUTHORIZE_URL = "https://discord.com/oauth2/authorize";
const API = "https://discord.com/api/v10";
const SCOPES = ["identify", "connections", "role_connections.write"];
const CALLBACK_PATH = "/api/linked-role/callback";

// Every redirect this feature sends goes to AUTHORIZE_URL or to RESULT_PAGE + one of FLAGS.
const RESULT_PAGE = "/linked-role?r=";
const FLAGS = ["ok", "cancelled", "expired", "busy", "failed"];

// __Host-: the browser only accepts it with Secure, Path=/ and no Domain, so a sibling
// subdomain cannot plant one.
const COOKIE = "__Host-fd_linked_role";
const STATE_TTL = 600; // seconds between pressing Link and coming back from Discord
const STATE_SHAPE = /^[A-Za-z0-9_-]{43}$/; // 32 random bytes, base64url
const CODE_SHAPE = /^[A-Za-z0-9._~-]{1,512}$/;
const MIN_SECRET = 32;
const FETCH_TIMEOUT = 5000;
const MAX_CONNECTIONS = 200; // entries of the reply looked at; Discord offers ~25 services

const BOOLEAN_EQUAL = 7;

// ------------------------------------------------------------------ settings
function setting(env, name) {
  const value = env[name];
  return typeof value === "string" ? value.trim() : "";
}

/** The four settings, or null when any is missing or unusable (the feature is then off). */
function readConfig(env) {
  const clientId = setting(env, "DISCORD_CLIENT_ID");
  const clientSecret = setting(env, "DISCORD_CLIENT_SECRET");
  const stateSecret = setting(env, "LINKED_ROLE_STATE_SECRET");
  const redirectUri = setting(env, "LINKED_ROLE_REDIRECT_URI");
  if (!/^[0-9]{15,22}$/.test(clientId) || !clientSecret || stateSecret.length < MIN_SECRET) return null;
  let url;
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
function sign(secret, payload) {
  return crypto.createHmac("sha256", secret).update(payload).digest("base64url");
}

function sameString(a, b) {
  const x = Buffer.from(String(a));
  const y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

function newState(randomBytes = crypto.randomBytes) {
  return randomBytes(32).toString("base64url");
}

/** Cookie value "state.expiry.signature"; nowMs is the clock in milliseconds. */
function stateCookieValue(secret, state, nowMs) {
  const payload = `${state}.${Math.floor(nowMs / 1000) + STATE_TTL}`;
  return `${payload}.${sign(secret, payload)}`;
}

/** The state inside a cookie we signed that has not expired, else null. */
function readStateCookie(secret, value, nowMs) {
  if (typeof value !== "string" || value.length > 200) return null;
  const parts = value.split(".");
  if (parts.length !== 3) return null;
  const [state, expiry, signature] = parts;
  if (!sameString(signature, sign(secret, `${state}.${expiry}`))) return null;
  if (!STATE_SHAPE.test(state) || !/^[0-9]{1,12}$/.test(expiry)) return null;
  if (Number(expiry) * 1000 <= nowMs) return null;
  return state;
}

function setCookieHeader(value) {
  return `${COOKIE}=${value}; Max-Age=${STATE_TTL}; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

function clearCookieHeader() {
  return `${COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

/** Our cookie from a raw Cookie header (the first one wins). */
function cookieFrom(header) {
  if (typeof header !== "string" || header.length > 8192) return null;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq).trim() === COOKIE) return part.slice(eq + 1).trim();
  }
  return null;
}

// ------------------------------------------------------------------ the mapping
function plural(n) {
  if (n === 0) return "No platforms linked";
  return n === 1 ? "1 platform linked" : `${n} platforms linked`;
}

/**
 * What is written to Discord for one member, from their /users/@me/connections reply.
 * A platform counts only when Discord marks the connection verified and not revoked.
 * Nothing but the service type is read: account names and ids never reach the result.
 */
function computeRoleConnection(connections, defs = definitions) {
  const verified = new Set();
  const list = Array.isArray(connections) ? connections.slice(0, MAX_CONNECTIONS) : [];
  for (const c of list) {
    if (c && typeof c === "object" && typeof c.type === "string" && c.verified === true && c.revoked !== true) {
      verified.add(c.type);
    }
  }
  const metadata = {};
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
class DiscordError extends Error {
  constructor(stage, status) {
    super(`${stage}: ${status}`);
    this.stage = stage;
    this.status = status;
  }
}

async function call(fetchImpl, stage, url, options) {
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
async function exchangeCode(fetchImpl, config, code) {
  const basic = Buffer.from(`${config.clientId}:${config.clientSecret}`).toString("base64");
  const body = new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: config.redirectUri });
  const reply = await call(fetchImpl, "token", `${API}/oauth2/token`, {
    method: "POST",
    headers: { Authorization: `Basic ${basic}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  const token = reply && reply.access_token;
  const granted = reply && typeof reply.scope === "string" ? reply.scope.split(" ") : [];
  if (typeof token !== "string" || !token || !/^bearer$/i.test(String(reply.token_type))) {
    throw new DiscordError("token", -1);
  }
  if (!["connections", "role_connections.write"].every((s) => granted.includes(s))) {
    throw new DiscordError("scope", -1);
  }
  return token;
}

async function fetchConnections(fetchImpl, token) {
  const reply = await call(fetchImpl, "connections", `${API}/users/@me/connections`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!Array.isArray(reply)) throw new DiscordError("connections", -1);
  return reply;
}

async function writeRoleConnection(fetchImpl, config, token, roleConnection) {
  await call(fetchImpl, "role-connection", `${API}/users/@me/applications/${config.clientId}/role-connection`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(roleConnection),
  });
}

// ------------------------------------------------------------------ responses
function baseHeaders(res) {
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Referrer-Policy", "no-referrer");
  res.setHeader("X-Robots-Tag", "noindex");
}

function redirect(res, location) {
  baseHeaders(res);
  res.statusCode = 302;
  res.setHeader("Location", location);
  res.end();
}

/** Back to the result page with one flag from FLAGS (anything else becomes "failed"). */
function finish(res, flag) {
  redirect(res, RESULT_PAGE + (FLAGS.includes(flag) ? flag : "failed"));
}

function page(res, status, title, text) {
  baseHeaders(res);
  res.statusCode = status;
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  // Constant text only: nothing from the request is ever written into this page.
  res.end(
    '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width, initial-scale=1">' +
      '<meta name="robots" content="noindex"><meta name="color-scheme" content="light dark">' +
      `<title>${title}</title><link rel="stylesheet" href="/style.css"></head>` +
      '<body><main class="lost"><div class="wrap"><div class="narrow">' +
      `<p class="label">Chill Gamers · Linked roles</p><h1>${title}</h1><p class="lede">${text}</p>` +
      '<hr><nav class="recover"><a class="label" href="/">← Back to the server page</a></nav>' +
      "</div></div></main></body></html>",
  );
}

function notSetUp(res) {
  page(res, 503, "Linked roles aren't set up yet.", "Nothing is wrong on your side. Check back soon.");
}

function wrongMethod(res) {
  res.setHeader("Allow", "GET");
  page(res, 405, "That isn't how this link works.", "Start again from the Linked Roles menu in Discord.");
}

/** One log line per failure: where and which HTTP status. Never a token, code, body or name. */
function logFailure(log, error) {
  if (error instanceof DiscordError) log(`linked-role: ${error.stage} failed (${error.status})`);
  else log("linked-role: unexpected error");
}

module.exports = {
  API,
  AUTHORIZE_URL,
  CODE_SHAPE,
  COOKIE,
  DiscordError,
  FLAGS,
  RESULT_PAGE,
  SCOPES,
  STATE_SHAPE,
  STATE_TTL,
  BOOLEAN_EQUAL,
  clearCookieHeader,
  computeRoleConnection,
  cookieFrom,
  definitions,
  exchangeCode,
  fetchConnections,
  finish,
  logFailure,
  newState,
  notSetUp,
  readConfig,
  readStateCookie,
  redirect,
  sameString,
  setCookieHeader,
  stateCookieValue,
  wrongMethod,
  writeRoleConnection,
};
