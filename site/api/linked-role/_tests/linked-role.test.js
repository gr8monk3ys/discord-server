// node --test site/api/linked-role/_tests/
// Fake req/res and a fake fetch: nothing here touches the network or the real clock.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const lib = require("../_lib.js");
const start = require("../start.js");
const callback = require("../callback.js");

const ENV = {
  // Made up, and built at run time: bot/tests/test_sitegen.py refuses ID-like numbers in site/.
  DISCORD_CLIENT_ID: "1".repeat(18),
  DISCORD_CLIENT_SECRET: "client-secret-for-tests",
  LINKED_ROLE_STATE_SECRET: "s".repeat(40),
  LINKED_ROLE_REDIRECT_URI: "https://example.test/api/linked-role/callback",
};
const NOW = 1_800_000_000_000;
const TOKEN = "access-token-for-tests";

function fakeRes() {
  return {
    statusCode: 200,
    headers: {},
    body: "",
    ended: false,
    setHeader(name, value) {
      this.headers[name.toLowerCase()] = value;
    },
    end(body) {
      this.body = body || "";
      this.ended = true;
    },
  };
}

function reply(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

/** A fetch that answers from `routes` (keyed by "METHOD path") and records every call. */
function fakeFetch(routes) {
  const calls = [];
  const impl = async (url, options) => {
    const key = `${options.method} ${new URL(url).pathname}`;
    calls.push({ key, url, options });
    const answer = routes[key];
    if (!answer) throw new Error(`unexpected request ${key}`);
    return typeof answer === "function" ? answer(options) : answer;
  };
  impl.calls = calls;
  return impl;
}

const HAPPY = () => ({
  "POST /api/v10/oauth2/token": reply(200, {
    access_token: TOKEN,
    token_type: "Bearer",
    scope: "identify connections role_connections.write",
  }),
  "GET /api/v10/users/@me/connections": reply(200, [
    { type: "steam", id: "1", name: "SecretGamertag", verified: true },
    { type: "xbox", id: "2", name: "OtherTag", verified: false },
  ]),
  [`PUT /api/v10/users/@me/applications/${ENV.DISCORD_CLIENT_ID}/role-connection`]: reply(200, {}),
});

/** Run start, then hand back what a browser would bring to the callback. */
function begin(env = ENV, now = NOW) {
  const res = fakeRes();
  start.create({ env, now: () => now })({ method: "GET", url: "/api/linked-role/start", headers: {} }, res);
  const location = new URL(res.headers.location);
  const cookie = res.headers["set-cookie"].split(";")[0];
  return { res, location, cookie, state: location.searchParams.get("state") };
}

async function finish({ query, cookie, routes = HAPPY(), now = NOW, env = ENV, method = "GET" }) {
  const fetchImpl = fakeFetch(routes);
  const logs = [];
  const res = fakeRes();
  const handler = callback.create({ env, fetch: fetchImpl, now: () => now, log: (line) => logs.push(line) });
  await handler({ method, url: `/api/linked-role/callback?${query}`, headers: cookie ? { cookie } : {} }, res);
  return { res, calls: fetchImpl.calls, logs };
}

// ------------------------------------------------------------------ mapping
test("only verified, unrevoked connections count, and no account name is copied", () => {
  const out = lib.computeRoleConnection([
    { type: "steam", name: "SecretGamertag", id: "765", verified: true },
    { type: "xbox", name: "Tag", verified: false },
    { type: "playstation", name: "Tag2", verified: true, revoked: true },
    { type: "epicgames", name: "Tag3", verified: true },
    { type: "epicgames", name: "Tag4", verified: true }, // a second account is still one platform
    { type: "spotify", name: "Music", verified: true }, // not a game platform we list
    { type: "steam", verified: "true" }, // only the boolean true counts
    null,
    "steam",
  ]);
  assert.deepEqual(out, {
    platform_name: "Front Desk",
    platform_username: "2 platforms linked",
    metadata: { steam: "1", xbox: "0", playstation: "0", epicgames: "1", platforms: "2" },
  });
  assert.ok(!JSON.stringify(out).includes("Tag"));
});

test("no connections, or a reply that is not a list, links nothing", () => {
  for (const input of [[], null, undefined, { type: "steam", verified: true }, "steam"]) {
    const out = lib.computeRoleConnection(input);
    assert.equal(out.platform_username, "No platforms linked");
    assert.deepEqual(out.metadata, { steam: "0", xbox: "0", playstation: "0", epicgames: "0", platforms: "0" });
  }
  assert.equal(lib.computeRoleConnection([{ type: "xbox", verified: true }]).platform_username, "1 platform linked");
});

test("metadata.json fits Discord's limits and every value fits too", () => {
  const defs = lib.definitions;
  assert.ok(defs.records.length >= 1 && defs.records.length <= 5);
  assert.ok(defs.platform_name.length <= 50);
  const keys = new Set();
  for (const r of defs.records) {
    assert.match(r.key, /^[a-z0-9_]{1,50}$/);
    assert.ok(!keys.has(r.key));
    keys.add(r.key);
    assert.ok(r.name.length >= 1 && r.name.length <= 100);
    assert.ok(r.description.length >= 1 && r.description.length <= 200);
    assert.ok(Number.isInteger(r.type) && r.type >= 1 && r.type <= 8);
    assert.ok(Boolean(r.service) !== Boolean(r.count), "a record is a platform flag or the count");
    if (r.service) assert.equal(r.type, lib.BOOLEAN_EQUAL);
  }
  const out = lib.computeRoleConnection([]);
  assert.deepEqual(Object.keys(out.metadata).sort(), [...keys].sort());
  assert.ok(out.platform_username.length <= 100);
});

// ------------------------------------------------------------------ settings
test("off (503) when any setting is missing or unusable", async () => {
  const bad = [
    { DISCORD_CLIENT_ID: "" },
    { DISCORD_CLIENT_ID: "not-a-number" },
    { DISCORD_CLIENT_SECRET: undefined },
    { LINKED_ROLE_STATE_SECRET: "too-short" },
    { LINKED_ROLE_REDIRECT_URI: "" },
    { LINKED_ROLE_REDIRECT_URI: "http://example.test/api/linked-role/callback" },
    { LINKED_ROLE_REDIRECT_URI: "https://example.test/somewhere/else" },
    { LINKED_ROLE_REDIRECT_URI: "https://example.test/api/linked-role/callback?x=1" },
    { LINKED_ROLE_REDIRECT_URI: "not a url" },
  ];
  for (const change of bad) {
    const env = { ...ENV, ...change };
    assert.equal(lib.readConfig(env), null, JSON.stringify(change));
    const res = fakeRes();
    start.create({ env })({ method: "GET", url: "/api/linked-role/start", headers: {} }, res);
    assert.equal(res.statusCode, 503);
    assert.equal(res.headers.location, undefined);
    assert.equal(res.headers["set-cookie"], undefined);
    assert.match(res.body, /aren't set up yet/);
    const done = await finish({ query: "code=abc&state=x", env });
    assert.equal(done.res.statusCode, 503);
    assert.equal(done.calls.length, 0);
  }
  assert.ok(lib.readConfig(ENV));
});

// ------------------------------------------------------------------ start
test("start sets a signed HttpOnly cookie and redirects to Discord with the fixed redirect_uri", () => {
  const { res, location, state } = begin();
  assert.equal(res.statusCode, 302);
  assert.equal(location.origin + location.pathname, "https://discord.com/oauth2/authorize");
  assert.equal(location.searchParams.get("response_type"), "code");
  assert.equal(location.searchParams.get("client_id"), ENV.DISCORD_CLIENT_ID);
  assert.equal(location.searchParams.get("redirect_uri"), ENV.LINKED_ROLE_REDIRECT_URI);
  assert.equal(location.searchParams.get("scope"), "identify connections role_connections.write");
  assert.match(state, lib.STATE_SHAPE);
  const cookie = res.headers["set-cookie"];
  assert.ok(cookie.startsWith(`__Host-fd_linked_role=${state}.`));
  for (const attr of ["HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=600"]) {
    assert.ok(cookie.split("; ").includes(attr), attr);
  }
  assert.ok(!/Domain=/i.test(cookie));
  assert.equal(res.headers["cache-control"], "no-store");
  assert.ok(!res.headers.location.includes(ENV.DISCORD_CLIENT_SECRET));
  assert.ok(!res.headers.location.includes(ENV.LINKED_ROLE_STATE_SECRET));
});

test("start ignores everything in the request and makes a new state each time", () => {
  const res = fakeRes();
  const req = { method: "GET", url: "/api/linked-role/start?redirect_uri=https://evil.test&next=//evil.test", headers: {} };
  start.create({ env: ENV, now: () => NOW })(req, res);
  assert.ok(res.headers.location.startsWith("https://discord.com/oauth2/authorize?"));
  assert.ok(!res.headers.location.includes("evil"));
  assert.notEqual(begin().state, begin().state);
});

test("start and callback refuse other methods", async () => {
  const res = fakeRes();
  start.create({ env: ENV })({ method: "POST", url: "/api/linked-role/start", headers: {} }, res);
  assert.equal(res.statusCode, 405);
  assert.equal(res.headers.allow, "GET");
  const done = await finish({ query: "code=abc&state=x", method: "POST" });
  assert.equal(done.res.statusCode, 405);
  assert.equal(done.calls.length, 0);
});

// ------------------------------------------------------------------ callback
test("the whole flow: exchange, read connections, write booleans, clear the cookie", async () => {
  const { cookie, state } = begin();
  const { res, calls, logs } = await finish({ query: `code=the-code&state=${state}`, cookie });

  assert.equal(res.statusCode, 302);
  assert.equal(res.headers.location, "/linked-role?r=ok");
  assert.match(res.headers["set-cookie"], /^__Host-fd_linked_role=; Max-Age=0; /);
  assert.equal(res.headers["cache-control"], "no-store");
  assert.deepEqual(logs, []);
  assert.deepEqual(calls.map((c) => c.key), Object.keys(HAPPY()));

  const [token, connections, write] = calls;
  assert.equal(token.url, "https://discord.com/api/v10/oauth2/token");
  assert.equal(token.options.headers["Content-Type"], "application/x-www-form-urlencoded");
  const basic = Buffer.from(token.options.headers.Authorization.replace("Basic ", ""), "base64").toString();
  assert.equal(basic, `${ENV.DISCORD_CLIENT_ID}:${ENV.DISCORD_CLIENT_SECRET}`);
  const form = new URLSearchParams(token.options.body);
  assert.deepEqual(Object.fromEntries(form), {
    grant_type: "authorization_code",
    code: "the-code",
    redirect_uri: ENV.LINKED_ROLE_REDIRECT_URI,
  });
  assert.equal(connections.options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(write.options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.deepEqual(JSON.parse(write.options.body), {
    platform_name: "Front Desk",
    platform_username: "1 platform linked",
    metadata: { steam: "1", xbox: "0", playstation: "0", epicgames: "0", platforms: "1" },
  });
  assert.ok(!write.options.body.includes("Gamertag"));
  for (const c of calls) assert.ok(c.url.startsWith("https://discord.com/api/v10/"));
});

test("nothing about the member, the code or the token reaches the redirect or the log", async () => {
  const { cookie, state } = begin();
  const routes = HAPPY();
  routes[`PUT /api/v10/users/@me/applications/${ENV.DISCORD_CLIENT_ID}/role-connection`] = reply(500, {
    message: TOKEN,
  });
  const { res, logs } = await finish({ query: `code=the-code&state=${state}`, cookie, routes });
  assert.equal(res.headers.location, "/linked-role?r=failed");
  assert.deepEqual(logs, ["linked-role: role-connection failed (500)"]);
  const seen = JSON.stringify([res.headers, res.body, logs]);
  for (const secret of [TOKEN, "the-code", "Gamertag", ENV.DISCORD_CLIENT_SECRET, ENV.LINKED_ROLE_STATE_SECRET]) {
    assert.ok(!seen.includes(secret), secret);
  }
});

test("denying on Discord's screen ends as cancelled without calling Discord", async () => {
  const { cookie, state } = begin();
  const query = `error=access_denied&error_description=The+user+denied&state=${state}`;
  const { res, calls } = await finish({ query, cookie });
  assert.equal(res.headers.location, "/linked-role?r=cancelled");
  assert.match(res.headers["set-cookie"], /Max-Age=0/);
  assert.equal(calls.length, 0);

  const other = begin();
  const odd = await finish({ query: `error=server_error&state=${other.state}`, cookie: other.cookie });
  assert.equal(odd.res.headers.location, "/linked-role?r=failed");
});

test("no cookie, an expired cookie or a replay ends as expired without calling Discord", async () => {
  const { cookie, state } = begin();
  const query = `code=the-code&state=${state}`;

  const none = await finish({ query });
  assert.equal(none.res.headers.location, "/linked-role?r=expired");
  assert.equal(none.calls.length, 0);

  const late = await finish({ query, cookie, now: NOW + lib.STATE_TTL * 1000 });
  assert.equal(late.res.headers.location, "/linked-role?r=expired");
  assert.equal(late.calls.length, 0);

  const justInTime = await finish({ query, cookie, now: NOW + lib.STATE_TTL * 1000 - 1000 });
  assert.equal(justInTime.res.headers.location, "/linked-role?r=ok");

  // The browser obeys the Max-Age=0 from the first visit, so a replay arrives without a cookie.
  assert.match(justInTime.res.headers["set-cookie"], /Max-Age=0/);
  const replay = await finish({ query });
  assert.equal(replay.res.headers.location, "/linked-role?r=expired");
});

test("a forged, re-signed or mismatched state is refused without calling Discord", async () => {
  const { cookie, state } = begin();
  const value = cookie.split("=")[1];
  const [, expiry, signature] = value.split(".");
  const otherState = "A".repeat(43);
  const cases = [
    // attacker's own state in the query, victim's cookie
    { query: `code=c&state=${otherState}`, cookie, flag: "failed" },
    { query: "code=c", cookie, flag: "failed" },
    { query: `code=c&state=${state}&state=${state}`, cookie, flag: "failed" },
    // cookie edited to match the attacker's state, signature not valid for it
    { query: `code=c&state=${otherState}`, cookie: `__Host-fd_linked_role=${otherState}.${expiry}.${signature}`, flag: "expired" },
    // expiry pushed out without re-signing
    { query: `code=c&state=${state}`, cookie: `__Host-fd_linked_role=${state}.9999999999.${signature}`, flag: "expired" },
    // signed with another secret
    {
      query: `code=c&state=${state}`,
      cookie: `__Host-fd_linked_role=${lib.stateCookieValue("x".repeat(40), state, NOW)}`,
      flag: "expired",
    },
    { query: `code=c&state=${state}`, cookie: "__Host-fd_linked_role=garbage", flag: "expired" },
    { query: `code=c&state=${state}`, cookie: `other=${value}`, flag: "expired" },
  ];
  for (const c of cases) {
    const { res, calls } = await finish({ query: c.query, cookie: c.cookie });
    assert.equal(res.headers.location, `/linked-role?r=${c.flag}`, c.query);
    assert.equal(calls.length, 0);
    assert.match(res.headers["set-cookie"], /Max-Age=0/);
  }
});

test("a missing or malformed code is refused without calling Discord", async () => {
  for (const code of ["", "has space", "a".repeat(513), "semi;colon"]) {
    const { cookie, state } = begin();
    const { res, calls } = await finish({ query: `code=${encodeURIComponent(code)}&state=${state}`, cookie });
    assert.equal(res.headers.location, "/linked-role?r=failed");
    assert.equal(calls.length, 0);
  }
});

test("Discord errors end as failed, a rate limit as busy, and later steps do not run", async () => {
  const put = `PUT /api/v10/users/@me/applications/${ENV.DISCORD_CLIENT_ID}/role-connection`;
  const boom = () => {
    throw new Error("socket hang up");
  };
  const cases = [
    // a code Discord has already used, or one that never was
    ["POST /api/v10/oauth2/token", reply(400, { error: "invalid_grant" }), "failed", 1, "token failed (400)"],
    ["POST /api/v10/oauth2/token", reply(429, { retry_after: 3 }), "busy", 1, "token failed (429)"],
    ["POST /api/v10/oauth2/token", reply(200, { token_type: "Bearer", scope: "connections" }), "failed", 1, "token failed (-1)"],
    [
      "POST /api/v10/oauth2/token",
      reply(200, { access_token: TOKEN, token_type: "Bearer", scope: "identify" }),
      "failed",
      1,
      "scope failed (-1)",
    ],
    ["POST /api/v10/oauth2/token", boom, "failed", 1, "token failed (0)"],
    ["GET /api/v10/users/@me/connections", reply(401, {}), "failed", 2, "connections failed (401)"],
    ["GET /api/v10/users/@me/connections", reply(200, { message: "not a list" }), "failed", 2, "connections failed (-1)"],
    ["GET /api/v10/users/@me/connections", reply(429, {}), "busy", 2, "connections failed (429)"],
    [put, reply(429, {}), "busy", 3, "role-connection failed (429)"],
    [put, reply(503, {}), "failed", 3, "role-connection failed (503)"],
    [put, { ok: true, status: 200, json: async () => JSON.parse("<html>") }, "failed", 3, "role-connection failed (-1)"],
  ];
  for (const [key, answer, flag, callCount, logLine] of cases) {
    const { cookie, state } = begin();
    const routes = HAPPY();
    routes[key] = answer;
    const { res, calls, logs } = await finish({ query: `code=the-code&state=${state}`, cookie, routes });
    assert.equal(res.headers.location, `/linked-role?r=${flag}`, logLine);
    assert.equal(calls.length, callCount, logLine);
    assert.deepEqual(logs, [`linked-role: ${logLine}`]);
    assert.match(res.headers["set-cookie"], /Max-Age=0/);
  }
});

test("every redirect either function can send is Discord's authorize URL or a known flag", async () => {
  const seen = new Set();
  const { cookie, state, res } = begin();
  seen.add(res.headers.location.split("?")[0]);
  const hostile = [
    `code=c&state=${state}&r=https://evil.test&redirect=//evil.test&next=/%5Cevil.test`,
    `error=https://evil.test&state=${state}`,
    "state=https://evil.test&code=https://evil.test",
  ];
  for (const query of hostile) {
    const done = await finish({ query, cookie });
    seen.add(done.res.headers.location);
  }
  const allowed = new Set(["https://discord.com/oauth2/authorize", ...lib.FLAGS.map((f) => lib.RESULT_PAGE + f)]);
  for (const location of seen) assert.ok(allowed.has(location), location);
});

// ------------------------------------------------------------------ the result page
test("the result page knows every flag the callback can send, and takes its text from a fixed list", () => {
  const dir = path.join(__dirname, "..", "..", "..", "linked-role");
  const script = fs.readFileSync(path.join(dir, "result.js"), "utf8");
  const html = fs.readFileSync(path.join(dir, "index.html"), "utf8");
  for (const flag of lib.FLAGS) assert.match(script, new RegExp(`\\b${flag}: \\[`), flag);
  assert.ok(!/innerHTML|document\.write|location\.(href|replace|assign)|eval\(/.test(script));
  assert.match(html, /<meta name="robots" content="noindex/);
  assert.ok(!/<script(?![^>]*\bsrc=)/.test(html), "no inline script (the site's CSP forbids it)");
  assert.ok(!/\son[a-z]+=/.test(html), "no inline event handlers");

  // Run the script against a tiny fake DOM for each flag and for hostile input.
  const run = (search) => {
    const els = { "lr-title": { textContent: "T" }, "lr-text": { textContent: "X" } };
    const window = { location: { search } };
    const document = { readyState: "complete", getElementById: (id) => els[id] || null };
    new Function("window", "document", script)(window, document);
    return [els["lr-title"].textContent, els["lr-text"].textContent];
  };
  assert.equal(run("?r=ok")[0], "You're linked.");
  assert.equal(run("?r=cancelled")[0], "Cancelled.");
  for (const search of ["", "?r=", "?r=<script>", "?r=constructor", "?r=__proto__", "?x=ok", "?r=OK"]) {
    assert.deepEqual(run(search), ["T", "X"], search);
  }
});
