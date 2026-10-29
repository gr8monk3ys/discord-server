// Tests for the Linked Roles flow (src/server/linked-role/) and its result page. A fake fetch
// and a fixed clock: nothing here touches the network or the real clock.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import LinkedRolePage, { metadata } from "@/app/linked-role/page";
import * as startRoute from "@/app/api/linked-role/start/route";
import * as callbackRoute from "@/app/api/linked-role/callback/route";
import { RESULTS } from "@/lib/linked-role-results";
import { createCallback, createStart } from "./flow";
import * as lib from "./lib";

const ENV = {
  // Made up, and built at run time: the bot's site tests refuse ID-like numbers in site/.
  DISCORD_CLIENT_ID: "1".repeat(18),
  DISCORD_CLIENT_SECRET: "client-secret-for-tests",
  LINKED_ROLE_STATE_SECRET: "s".repeat(40),
  LINKED_ROLE_REDIRECT_URI: "https://example.test/api/linked-role/callback",
};
type Env = Record<string, string | undefined>;
const NOW = 1_800_000_000_000;
const TOKEN = "access-token-for-tests";
const ORIGIN = "https://example.test";

type Answer = { ok: boolean; status: number; json: () => Promise<unknown> } | ((options: RequestInit) => never);
type Call = { key: string; url: string; options: RequestInit & { headers: Record<string, string> } };

function reply(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

/** A fetch that answers from `routes` (keyed by "METHOD path") and records every call. */
function fakeFetch(routes: Record<string, Answer>) {
  const calls: Call[] = [];
  const impl = async (url: string, options: RequestInit & { method: string }) => {
    const key = `${options.method} ${new URL(url).pathname}`;
    calls.push({ key, url, options: options as Call["options"] });
    const answer = routes[key];
    if (!answer) throw new Error(`unexpected request ${key}`);
    return typeof answer === "function" ? answer(options) : answer;
  };
  return { impl, calls };
}

const PUT = `PUT /api/v10/users/@me/applications/${ENV.DISCORD_CLIENT_ID}/role-connection`;
const HAPPY = (): Record<string, Answer> => ({
  "POST /api/v10/oauth2/token": reply(200, {
    access_token: TOKEN,
    token_type: "Bearer",
    scope: "identify connections role_connections.write",
  }),
  "GET /api/v10/users/@me/connections": reply(200, [
    { type: "steam", id: "1", name: "SecretGamertag", verified: true },
    { type: "xbox", id: "2", name: "OtherTag", verified: false },
  ]),
  [PUT]: reply(200, {}),
});

type Res = { status: number; headers: Record<string, string>; body: string };
async function read(response: Response): Promise<Res> {
  return { status: response.status, headers: Object.fromEntries(response.headers), body: await response.text() };
}

function startReq(method = "GET", search = "") {
  return new Request(`${ORIGIN}/api/linked-role/start${search}`, { method });
}

/** Run start, then hand back what a browser would bring to the callback. */
async function begin(env: Env = ENV, now = NOW) {
  const res = await read(createStart({ env, now: () => now })(startReq()));
  const location = new URL(res.headers.location);
  const cookie = res.headers["set-cookie"].split(";")[0];
  return { res, location, cookie, state: location.searchParams.get("state") as string };
}

async function finish({
  query,
  cookie,
  routes = HAPPY(),
  now = NOW,
  env = ENV,
  method = "GET",
}: { query: string; cookie?: string; routes?: Record<string, Answer>; now?: number; env?: Env; method?: string }) {
  const { impl, calls } = fakeFetch(routes);
  const logs: string[] = [];
  const handler = createCallback({ env, fetch: impl as unknown as lib.FetchLike, now: () => now, log: (line) => logs.push(line) });
  const req = new Request(`${ORIGIN}/api/linked-role/callback?${query}`, {
    method,
    headers: cookie ? { cookie } : {},
  });
  return { res: await read(await handler(req)), calls, logs };
}

describe("linked roles: mapping", () => {
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
    expect(out).toEqual({
      platform_name: "Front Desk",
      platform_username: "2 platforms linked",
      metadata: { steam: "1", xbox: "0", playstation: "0", epicgames: "1", platforms: "2" },
    });
    expect(JSON.stringify(out)).not.toContain("Tag");
  });

  test("no connections, or a reply that is not a list, links nothing", () => {
    for (const input of [[], null, undefined, { type: "steam", verified: true }, "steam"]) {
      const out = lib.computeRoleConnection(input);
      expect(out.platform_username).toBe("No platforms linked");
      expect(out.metadata).toEqual({ steam: "0", xbox: "0", playstation: "0", epicgames: "0", platforms: "0" });
    }
    expect(lib.computeRoleConnection([{ type: "xbox", verified: true }]).platform_username).toBe("1 platform linked");
  });

  test("metadata.json fits Discord's limits and every value fits too", () => {
    const defs = lib.definitions as {
      platform_name: string;
      records: { key: string; name: string; description: string; type: number; service?: string; count?: boolean }[];
    };
    expect(defs.records.length).toBeGreaterThanOrEqual(1);
    expect(defs.records.length).toBeLessThanOrEqual(5);
    expect(defs.platform_name.length).toBeLessThanOrEqual(50);
    const keys = new Set<string>();
    for (const r of defs.records) {
      expect(r.key).toMatch(/^[a-z0-9_]{1,50}$/);
      expect(keys.has(r.key)).toBe(false);
      keys.add(r.key);
      expect(r.name.length >= 1 && r.name.length <= 100).toBe(true);
      expect(r.description.length >= 1 && r.description.length <= 200).toBe(true);
      expect(Number.isInteger(r.type) && r.type >= 1 && r.type <= 8).toBe(true);
      expect(Boolean(r.service) !== Boolean(r.count), "a record is a platform flag or the count").toBe(true);
      if (r.service) expect(r.type).toBe(lib.BOOLEAN_EQUAL);
    }
    const out = lib.computeRoleConnection([]);
    expect(Object.keys(out.metadata).sort()).toEqual([...keys].sort());
    expect(out.platform_username.length).toBeLessThanOrEqual(100);
  });
});

describe("linked roles: settings and start", () => {
  test("off (503) when any setting is missing or unusable", async () => {
    const bad: Env[] = [
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
      expect(lib.readConfig(env), JSON.stringify(change)).toBeNull();
      const res = await read(createStart({ env })(startReq()));
      expect(res.status).toBe(503);
      expect(res.headers.location).toBeUndefined();
      expect(res.headers["set-cookie"]).toBeUndefined();
      expect(res.body).toMatch(/aren't set up yet/);
      const done = await finish({ query: "code=abc&state=x", env });
      expect(done.res.status).toBe(503);
      expect(done.calls).toHaveLength(0);
    }
    expect(lib.readConfig(ENV)).toBeTruthy();
  });

  test("start sets a signed HttpOnly cookie and redirects to Discord with the fixed redirect_uri", async () => {
    const { res, location, state } = await begin();
    expect(res.status).toBe(302);
    expect(location.origin + location.pathname).toBe("https://discord.com/oauth2/authorize");
    expect(location.searchParams.get("response_type")).toBe("code");
    expect(location.searchParams.get("client_id")).toBe(ENV.DISCORD_CLIENT_ID);
    expect(location.searchParams.get("redirect_uri")).toBe(ENV.LINKED_ROLE_REDIRECT_URI);
    expect(location.searchParams.get("scope")).toBe("identify connections role_connections.write");
    expect(state).toMatch(lib.STATE_SHAPE);
    const cookie = res.headers["set-cookie"];
    expect(cookie.startsWith(`__Host-fd_linked_role=${state}.`)).toBe(true);
    for (const attr of ["HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=600"]) {
      expect(cookie.split("; "), attr).toContain(attr);
    }
    expect(cookie).not.toMatch(/Domain=/i);
    expect(res.headers["cache-control"]).toBe("no-store");
    expect(res.headers.location).not.toContain(ENV.DISCORD_CLIENT_SECRET);
    expect(res.headers.location).not.toContain(ENV.LINKED_ROLE_STATE_SECRET);
  });

  test("start ignores everything in the request and makes a new state each time", async () => {
    const req = startReq("GET", "?redirect_uri=https://evil.test&next=//evil.test");
    const res = await read(createStart({ env: ENV, now: () => NOW })(req));
    expect(res.headers.location.startsWith("https://discord.com/oauth2/authorize?")).toBe(true);
    expect(res.headers.location).not.toContain("evil");
    expect((await begin()).state).not.toBe((await begin()).state);
  });

  test("start and callback refuse other methods, and the routes answer every method", async () => {
    const res = await read(createStart({ env: ENV })(startReq("POST")));
    expect(res.status).toBe(405);
    expect(res.headers.allow).toBe("GET");
    const done = await finish({ query: "code=abc&state=x", method: "POST" });
    expect(done.res.status).toBe(405);
    expect(done.calls).toHaveLength(0);
    for (const name of ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"] as const) {
      expect(typeof startRoute[name]).toBe("function");
      expect(typeof callbackRoute[name]).toBe("function");
    }
    expect((await startRoute.DELETE(startReq("DELETE"))).status).toBe(405);
    expect(startRoute.dynamic).toBe("force-dynamic");
    expect(callbackRoute.dynamic).toBe("force-dynamic");
  });
});

describe("linked roles: callback", () => {
  test("the whole flow: exchange, read connections, write booleans, clear the cookie", async () => {
    const { cookie, state } = await begin();
    const { res, calls, logs } = await finish({ query: `code=the-code&state=${state}`, cookie });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/linked-role?r=ok");
    expect(res.headers["set-cookie"]).toMatch(/^__Host-fd_linked_role=; Max-Age=0; /);
    expect(res.headers["cache-control"]).toBe("no-store");
    expect(logs).toEqual([]);
    expect(calls.map((c) => c.key)).toEqual(Object.keys(HAPPY()));

    const [token, connections, write] = calls;
    expect(token.url).toBe("https://discord.com/api/v10/oauth2/token");
    expect(token.options.headers["Content-Type"]).toBe("application/x-www-form-urlencoded");
    const basic = Buffer.from(token.options.headers.Authorization.replace("Basic ", ""), "base64").toString();
    expect(basic).toBe(`${ENV.DISCORD_CLIENT_ID}:${ENV.DISCORD_CLIENT_SECRET}`);
    expect(Object.fromEntries(new URLSearchParams(String(token.options.body)))).toEqual({
      grant_type: "authorization_code",
      code: "the-code",
      redirect_uri: ENV.LINKED_ROLE_REDIRECT_URI,
    });
    expect(connections.options.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(write.options.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(JSON.parse(String(write.options.body))).toEqual({
      platform_name: "Front Desk",
      platform_username: "1 platform linked",
      metadata: { steam: "1", xbox: "0", playstation: "0", epicgames: "0", platforms: "1" },
    });
    expect(String(write.options.body)).not.toContain("Gamertag");
    for (const c of calls) expect(c.url.startsWith("https://discord.com/api/v10/")).toBe(true);
  });

  test("nothing about the member, the code or the token reaches the redirect or the log", async () => {
    const { cookie, state } = await begin();
    const routes = HAPPY();
    routes[PUT] = reply(500, { message: TOKEN });
    const { res, logs } = await finish({ query: `code=the-code&state=${state}`, cookie, routes });
    expect(res.headers.location).toBe("/linked-role?r=failed");
    expect(logs).toEqual(["linked-role: role-connection failed (500)"]);
    const seen = JSON.stringify([res.headers, res.body, logs]);
    for (const secret of [TOKEN, "the-code", "Gamertag", ENV.DISCORD_CLIENT_SECRET, ENV.LINKED_ROLE_STATE_SECRET]) {
      expect(seen, secret).not.toContain(secret);
    }
  });

  test("denying on Discord's screen ends as cancelled without calling Discord", async () => {
    const { cookie, state } = await begin();
    const query = `error=access_denied&error_description=The+user+denied&state=${state}`;
    const { res, calls } = await finish({ query, cookie });
    expect(res.headers.location).toBe("/linked-role?r=cancelled");
    expect(res.headers["set-cookie"]).toMatch(/Max-Age=0/);
    expect(calls).toHaveLength(0);

    const other = await begin();
    const odd = await finish({ query: `error=server_error&state=${other.state}`, cookie: other.cookie });
    expect(odd.res.headers.location).toBe("/linked-role?r=failed");
  });

  test("no cookie, an expired cookie or a replay ends as expired without calling Discord", async () => {
    const { cookie, state } = await begin();
    const query = `code=the-code&state=${state}`;

    const none = await finish({ query });
    expect(none.res.headers.location).toBe("/linked-role?r=expired");
    expect(none.calls).toHaveLength(0);

    const late = await finish({ query, cookie, now: NOW + lib.STATE_TTL * 1000 });
    expect(late.res.headers.location).toBe("/linked-role?r=expired");
    expect(late.calls).toHaveLength(0);

    const justInTime = await finish({ query, cookie, now: NOW + lib.STATE_TTL * 1000 - 1000 });
    expect(justInTime.res.headers.location).toBe("/linked-role?r=ok");

    // The browser obeys the Max-Age=0 from the first visit, so a replay arrives without a cookie.
    expect(justInTime.res.headers["set-cookie"]).toMatch(/Max-Age=0/);
    const replay = await finish({ query });
    expect(replay.res.headers.location).toBe("/linked-role?r=expired");
  });

  test("a forged, re-signed or mismatched state is refused without calling Discord", async () => {
    const { cookie, state } = await begin();
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
      expect(res.headers.location, c.query).toBe(`/linked-role?r=${c.flag}`);
      expect(calls).toHaveLength(0);
      expect(res.headers["set-cookie"]).toMatch(/Max-Age=0/);
    }
  });

  test("a missing or malformed code is refused without calling Discord", async () => {
    for (const code of ["", "has space", "a".repeat(513), "semi;colon"]) {
      const { cookie, state } = await begin();
      const { res, calls } = await finish({ query: `code=${encodeURIComponent(code)}&state=${state}`, cookie });
      expect(res.headers.location).toBe("/linked-role?r=failed");
      expect(calls).toHaveLength(0);
    }
  });

  test("Discord errors end as failed, a rate limit as busy, and later steps do not run", async () => {
    const boom = () => {
      throw new Error("socket hang up");
    };
    const cases: [string, Answer, string, number, string][] = [
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
      [PUT, reply(429, {}), "busy", 3, "role-connection failed (429)"],
      [PUT, reply(503, {}), "failed", 3, "role-connection failed (503)"],
      [PUT, { ok: true, status: 200, json: async () => JSON.parse("<html>") }, "failed", 3, "role-connection failed (-1)"],
    ];
    for (const [key, answer, flag, callCount, logLine] of cases) {
      const { cookie, state } = await begin();
      const routes = HAPPY();
      routes[key] = answer;
      const { res, calls, logs } = await finish({ query: `code=the-code&state=${state}`, cookie, routes });
      expect(res.headers.location, logLine).toBe(`/linked-role?r=${flag}`);
      expect(calls.length, logLine).toBe(callCount);
      expect(logs).toEqual([`linked-role: ${logLine}`]);
      expect(res.headers["set-cookie"]).toMatch(/Max-Age=0/);
    }
  });

  test("every redirect either handler can send is Discord's authorize URL or a known flag", async () => {
    const seen = new Set<string>();
    const { cookie, state, res } = await begin();
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
    for (const location of seen) expect(allowed.has(location), location).toBe(true);
  });
});

describe("linked roles: the result page", () => {
  const render = async (r?: string | string[]) =>
    renderToStaticMarkup(await LinkedRolePage({ searchParams: Promise.resolve(r === undefined ? {} : { r }) }));
  const textOf = (html: string, id: string) => {
    const m = html.match(new RegExp(`id="${id}"[^>]*>([^<]*)<`));
    return m ? m[1].replace(/&#x27;/g, "'") : null;
  };

  test("knows every flag the callback can send, and takes its text from a fixed list", async () => {
    for (const flag of lib.FLAGS) {
      const html = await render(flag);
      expect(textOf(html, "lr-title"), flag).toBe(RESULTS[flag][0]);
      expect(textOf(html, "lr-text"), flag).toBe(RESULTS[flag][1]);
    }
    expect(textOf(await render("ok"), "lr-title")).toBe("You're linked.");
    expect(textOf(await render("cancelled"), "lr-title")).toBe("Cancelled.");
  });

  test("anything else shows the default text, and nothing from the address is written", async () => {
    const hostile = ["", "<script>", "constructor", "__proto__", "OK", "toString", "\"><img src=x>"];
    for (const r of [undefined, ...hostile, ["ok", "ok"]]) {
      const html = await render(r);
      expect(textOf(html, "lr-title"), String(r)).toBe(RESULTS.default[0]);
      expect(textOf(html, "lr-text"), String(r)).toBe(RESULTS.default[1]);
      expect(html).not.toContain("<script>");
      expect(html).not.toContain("<img src=x>");
    }
    expect(metadata.robots).toEqual({ index: false, follow: false });
    expect(metadata.referrer).toBe("no-referrer");
  });
});
