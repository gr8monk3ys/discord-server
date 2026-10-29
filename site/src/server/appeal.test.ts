// Tests for the ban-appeal handler (src/server/appeal.ts) and its route. No network: the
// handler gets a real Request and a fake fetch.
import { describe, expect, test } from "vitest";
import * as route from "@/app/api/appeal/route";
import {
  appealCode,
  BODY_MAX,
  buildPayload,
  cleanAnswer,
  createHandler,
  inertString,
  normalizeCode,
  PAYLOAD_MAX,
  readConfig,
  SITE_ORIGIN,
  validate,
} from "./appeal";

// The shared vector: bot/tests/test_appeals.py asserts the same literal for the same inputs.
const SECRET = "test-secret-for-appeal-vectors!!";
// Made up, and built at run time: the bot's site tests refuse ID-like numbers in site/.
const USER = "123456789" + "012345678";
const CODE = "TD7YUL66OYCHIZTL";
// Assembled from parts so secret scanners don't mistake the fixture for a live webhook.
const WEBHOOK = ["https://discord.com/api", "webhooks", "1".padEnd(18, "0"), "fixture_token-".padEnd(48, "x")].join("/");
const ENV = { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK };
const GOOD = { user_id: USER, code: "td7y-ul66-oych-iztl", what: "I spammed the chat.", why: "I read the rules.", else: "" };
const URL_ = `${SITE_ORIGIN}/api/appeal`;

type Reply = { ok: boolean; status: number };
type Options = { method?: string; headers?: Record<string, string | undefined>; raw?: string };

function fakeReq(body: unknown, { method = "POST", headers = {}, raw }: Options = {}): Request {
  const text = raw !== undefined ? raw : JSON.stringify(body);
  const all: Record<string, string | undefined> = {
    origin: SITE_ORIGIN,
    "content-type": "application/json",
    "content-length": String(Buffer.byteLength(text)),
    ...headers,
  };
  const h = new Headers();
  for (const [k, v] of Object.entries(all)) if (v !== undefined) h.set(k, v);
  const hasBody = method !== "GET" && method !== "HEAD";
  return new Request(URL_, { method, headers: h, body: hasBody && text !== "" ? text : undefined });
}

/** A request whose body arrives as a stream, with whatever Content-Length it claims. */
function streamReq(chunks: Uint8Array[], { headers = {}, error }: { headers?: Record<string, string>; error?: Error } = {}) {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(c);
      if (error) controller.error(error);
      else controller.close();
    },
  });
  return new Request(URL_, {
    method: "POST",
    headers: { origin: SITE_ORIGIN, "content-type": "application/json", ...headers },
    body: stream,
    duplex: "half",
  } as RequestInit);
}

function setup({ env = ENV as Record<string, string>, reply = { ok: true, status: 204 } as Reply, fail = null as Error | null } = {}) {
  const calls: { url: string; init: RequestInit }[] = [];
  const logs: string[] = [];
  const fetchImpl = async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (fail) throw fail;
    return reply;
  };
  const handler = createHandler({ config: readConfig(env), fetchImpl, log: (line) => logs.push(line) });
  return { handler, calls, logs };
}

type Res = { status: number; headers: Record<string, string>; text: string; json: Record<string, unknown> & { fields?: unknown } };

async function read(response: Response): Promise<Res> {
  const text = await response.text();
  let json = {};
  try {
    json = JSON.parse(text);
  } catch {
    /* not JSON */
  }
  return { status: response.status, headers: Object.fromEntries(response.headers), text, json };
}

async function post(ctx: ReturnType<typeof setup>, body: unknown, options?: Options): Promise<Res> {
  return read(await ctx.handler(fakeReq(body, options)));
}

const sentBody = (ctx: ReturnType<typeof setup>, i = 0) => JSON.parse(String(ctx.calls[i].init.body));

describe("appeal", () => {
  test("the code matches the Python implementation's shared vector", () => {
    expect(appealCode(SECRET, USER)).toBe(CODE);
    expect(appealCode(SECRET, "987654321" + "098765432")).toBe("TQRXMXY43NDFCQDZ");
    expect(appealCode(SECRET + "x", USER)).not.toBe(CODE);
  });

  test("codes are normalised and anything else is refused", () => {
    expect(normalizeCode(" td7y-ul66 oych-iztl ")).toBe(CODE);
    for (const bad of ["", "TD7YUL66OYCHIZT", CODE + "A", "TD7YUL66OYCHIZT1", "TD7YUL66OYCHIZT!", null, 5, "A".repeat(80)]) {
      expect(normalizeCode(bad), String(bad)).toBeNull();
    }
  });

  test("a valid appeal is forwarded once, compactly, with no mentions allowed", async () => {
    const ctx = setup();
    const res = await post(ctx, GOOD);
    expect(res.status).toBe(200);
    expect(res.json).toEqual({ ok: true, message: "Received." });
    expect(res.headers["cache-control"]).toBe("no-store");
    expect(ctx.calls).toHaveLength(1);
    const { url, init } = ctx.calls[0];
    expect(url).toBe(WEBHOOK);
    expect(init.method).toBe("POST");
    expect(init.redirect).toBe("error");
    const sent = sentBody(ctx);
    expect(sent.allowed_mentions).toEqual({ parse: [] });
    expect(sent.content).toBeUndefined();
    expect(sent.embeds).toHaveLength(1);
    expect(JSON.parse(sent.embeds[0].description)).toEqual({
      v: 1,
      u: USER,
      c: CODE,
      a: ["I spammed the chat.", "I read the rules.", ""],
    });
    expect(ctx.logs).toEqual([]);
  });

  test("a wrong code gets the same answer as a right one and nothing is forwarded", async () => {
    const good = await post(setup(), GOOD);
    for (const body of [{ ...GOOD, code: "AAAAAAAAAAAAAAAA" }, { ...GOOD, user_id: "123456789" + "012345679" }]) {
      const ctx = setup();
      const res = await post(ctx, body);
      expect(res.status).toBe(200);
      expect(res.text).toBe(good.text);
      expect(res.headers).toEqual(good.headers);
      expect(ctx.calls).toHaveLength(0);
      expect(ctx.logs).toEqual([]);
    }
  });

  test("off with a friendly 503 unless both settings are present and sane", async () => {
    const off: Record<string, string>[] = [
      {},
      { APPEAL_SECRET: SECRET },
      { APPEAL_WEBHOOK_URL: WEBHOOK },
      { APPEAL_SECRET: "short", APPEAL_WEBHOOK_URL: WEBHOOK },
      { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK.replace("https:", "http:") },
      { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK.replace("discord.com", "discord.com.evil.example") },
      { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK.replace("discord.com", "evil.example") },
      { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK + "?wait=true" },
      { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: "https://discord.com/api/users/@me" },
    ];
    for (const env of off) {
      expect(readConfig(env)).toBeNull();
      const ctx = setup({ env });
      const res = await post(ctx, GOOD);
      expect(res.status).toBe(503);
      expect(res.json.ok).toBe(false);
      expect(res.json.message).toMatch(/aren't open/);
      expect(ctx.calls).toHaveLength(0);
    }
  });

  test("POST only", async () => {
    for (const method of ["GET", "PUT", "DELETE", "OPTIONS", "HEAD"]) {
      const ctx = setup();
      const res = await post(ctx, GOOD, { method });
      expect(res.status).toBe(405);
      expect(res.headers.allow).toBe("POST");
      expect(ctx.calls).toHaveLength(0);
    }
  });

  test("only this site's own origin may post", async () => {
    const bad: Record<string, string | undefined>[] = [
      { origin: "https://evil.example" },
      { origin: undefined },
      { origin: "null" },
      { origin: SITE_ORIGIN + ".evil.example" },
      { origin: SITE_ORIGIN.replace("https", "http") },
      { "sec-fetch-site": "cross-site" },
      { "sec-fetch-site": "same-site" },
    ];
    for (const headers of bad) {
      const ctx = setup();
      const res = await post(ctx, GOOD, { headers });
      expect(res.status, JSON.stringify(headers)).toBe(403);
      expect(ctx.calls).toHaveLength(0);
    }
    expect((await post(setup(), GOOD, { headers: { "sec-fetch-site": "same-origin" } })).status).toBe(200);
    const custom = setup({ env: { ...ENV, APPEAL_ORIGIN: "https://staging.example/" } });
    expect((await post(custom, GOOD)).status).toBe(403);
    expect((await post(custom, GOOD, { headers: { origin: "https://staging.example" } })).status).toBe(200);
  });

  test("JSON only", async () => {
    for (const type of ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", undefined, "application/jsonp"]) {
      const ctx = setup();
      const req = fakeReq(GOOD, { headers: { "content-type": type } });
      if (type === undefined) req.headers.delete("content-type"); // a string body sets text/plain by itself
      const res = await read(await ctx.handler(req));
      expect(res.status, String(type)).toBe(415);
      expect(ctx.calls).toHaveLength(0);
    }
    expect((await post(setup(), GOOD, { headers: { "content-type": "application/json; charset=utf-8" } })).status).toBe(200);
    for (const raw of ["", "{", "[1,2]x", "null", "7", '"text"']) {
      const res = await post(setup(), null, { raw });
      expect(res.status, raw).toBe(400);
    }
  });

  test("the body is capped by header and by stream", async () => {
    const huge = { ...GOOD, what: "a".repeat(BODY_MAX) };
    let ctx = setup();
    expect((await post(ctx, huge)).status).toBe(413);
    // a lying Content-Length
    ctx = setup();
    const big = new TextEncoder().encode(JSON.stringify(huge));
    expect((await read(await ctx.handler(streamReq([big], { headers: { "content-length": "10" } })))).status).toBe(413);
    // no Content-Length at all, the body trickling in: refused once it passes the cap
    ctx = setup();
    const pieces = Array.from({ length: 12 }, () => new TextEncoder().encode("a".repeat(1024)));
    expect((await read(await ctx.handler(streamReq(pieces)))).status).toBe(413);
    expect(ctx.calls).toHaveLength(0);
  });

  test("a streamed body works, and a stream that breaks off is a 400", async () => {
    const ctx = setup();
    const text = new TextEncoder().encode(JSON.stringify(GOOD));
    const res = await read(await ctx.handler(streamReq([text.slice(0, 10), text.slice(10)])));
    expect(res.status).toBe(200);
    expect(ctx.calls).toHaveLength(1);
    const broken = await read(await ctx.handler(streamReq([text.slice(0, 10)], { error: new Error("connection reset") })));
    expect(broken.status).toBe(400);
    expect(ctx.calls).toHaveLength(1);
  });

  test("fields are validated strictly and never echoed", async () => {
    const cases: [Record<string, unknown>, Record<string, string>][] = [
      [{ ...GOOD, user_id: "12345" }, { user_id: "format" }],
      [{ ...GOOD, user_id: "0" + USER }, { user_id: "format" }],
      [{ ...GOOD, user_id: 123456789012345 }, { user_id: "format" }],
      [{ ...GOOD, user_id: USER + " OR 1=1" }, { user_id: "format" }],
      [{ ...GOOD, code: "nope" }, { code: "format" }],
      [{ ...GOOD, code: ["TD7YUL66OYCHIZTL"] }, { code: "format" }],
      [{ ...GOOD, what: "   \n " }, { what: "required" }],
      [{ ...GOOD, why: undefined }, { why: "required" }],
      [{ ...GOOD, what: "x".repeat(601) }, { what: "too_long" }],
      [{ ...GOOD, else: "y".repeat(301) }, { else: "too_long" }],
      [{ ...GOOD, why: { $ne: "" } }, { why: "format" }],
      [{ ...GOOD, what: "see https://evil.example/x" }, { what: "links" }],
      [{ ...GOOD, else: "WWW.evil.example" }, { else: "links" }],
      [{ ...GOOD, extra: "field" }, { form: "shape" }],
      [{ ...GOOD, __proto__x: 1 }, { form: "shape" }],
      [{ user_id: "1", code: "2", what: "", why: "" }, { user_id: "format", code: "format", what: "required", why: "required" }],
    ];
    for (const [body, fields] of cases) {
      const ctx = setup();
      const res = await post(ctx, body);
      expect(res.status, JSON.stringify(fields)).toBe(400);
      expect(res.json.fields).toEqual(fields);
      expect(ctx.calls).toHaveLength(0);
      for (const value of Object.values(body)) {
        if (typeof value === "string" && value.length > 8) expect(res.text.includes(value)).toBe(false);
      }
    }
    expect((validate([]) as { fields: Record<string, string> }).fields.form).toBe("shape");
    expect((validate(null) as { fields: Record<string, string> }).fields.form).toBe("shape");
    const emoji = "\u{1F600}".repeat(600); // 600 characters, 1200 UTF-16 units: allowed
    expect((validate({ ...GOOD, what: emoji }) as { answers: string[] }).answers[0]).toBe(emoji);
  });

  test("answers are cleaned: control and invisible characters, blank-line runs, odd spacing", () => {
    expect(cleanAnswer("  a\tb\u0000c​d‮e  ")).toBe("a bcde");
    expect(cleanAnswer("one\r\n\r\n\r\n\r\ntwo \n three")).toBe("one\n\ntwo\nthree");
    expect(cleanAnswer("\u0007‍")).toBe("");
  });

  test("hostile text is inert in the payload and survives the round trip", async () => {
    const nasty = '@everyone <@1> <@&2> [x](javascript:alert(1)) **b** `c` ~~d~~ ||e|| # h :tada: a/b &amp; "q" \\ back\nline';
    const payload = buildPayload(USER, CODE, [nasty, "ok", ""]);
    expect(JSON.parse(payload).a[0]).toBe(nasty);
    const literal = inertString(nasty);
    for (const ch of "<>@[]()`*_~|#:/&") expect(literal.includes(ch), ch).toBe(false);
    expect(JSON.parse(literal)).toBe(nasty);
    const ctx = setup();
    const res = await post(ctx, { ...GOOD, what: nasty });
    expect(res.status).toBe(200);
    const description: string = sentBody(ctx).embeds[0].description;
    expect(description.includes("@everyone") || description.includes("](") || description.includes("**")).toBe(false);
    expect(JSON.parse(description).a[0]).toBe(nasty);
  });

  test("a payload too big for one embed is refused, not truncated", async () => {
    const ctx = setup();
    const res = await post(ctx, { ...GOOD, what: "*".repeat(600), why: "_".repeat(600) });
    expect(res.status).toBe(400);
    expect(res.json.fields).toEqual({ form: "too_long" });
    expect(ctx.calls).toHaveLength(0);
    expect(buildPayload(USER, CODE, ["a".repeat(600), "b".repeat(600), "c".repeat(300)]).length).toBeLessThan(PAYLOAD_MAX);
  });

  test("a malformed request is refused the same way with a right code and with a wrong one", async () => {
    // Nothing about the refusal may depend on the code: otherwise each of these would be a
    // way to test a code (or a user ID) without ever sending a real appeal.
    const WRONG = "AAAAAAAAAAAAAAAA";
    const malformed: Record<string, unknown>[] = [
      { what: "*".repeat(600), why: "_".repeat(600) }, // fits the field limits, too long once escaped
      { what: "x".repeat(601) },
      { what: "" },
      { why: "see https://example.com" },
      { else: 5 },
      { extra: "field" },
    ];
    for (const change of malformed) {
      const answers = new Set<string>();
      for (const code of [GOOD.code, WRONG]) {
        for (const failing of [false, true]) {
          // nor on whether Discord would have taken it
          const ctx = setup(failing ? { reply: { ok: false, status: 500 } } : {});
          const res = await post(ctx, { ...GOOD, ...change, code });
          expect(res.status, JSON.stringify(change)).toBe(400);
          expect(ctx.calls).toHaveLength(0);
          expect(ctx.logs).toEqual([]);
          answers.add(JSON.stringify([res.status, res.headers, res.text]));
        }
      }
      expect(answers.size, JSON.stringify(change)).toBe(1);
    }
    // the same holds for a body that is too large or not JSON at all
    for (const raw of ["{not json", null]) {
      const right = await post(setup(), GOOD, { raw: raw ?? JSON.stringify({ ...GOOD, what: "x".repeat(BODY_MAX) }) });
      const wrong = await post(setup(), GOOD, { raw: raw ?? JSON.stringify({ ...GOOD, code: WRONG, what: "x".repeat(BODY_MAX) }) });
      expect([right.status, right.text]).toEqual([wrong.status, wrong.text]);
      expect([400, 413]).toContain(right.status);
    }
  });

  test("a well-formed appeal with a wrong code is 'received' even while Discord is failing", async () => {
    // The 502 below takes the real code to trigger, so it tells its sender nothing they did
    // not already hold; without the code the answer never changes.
    for (const options of [{ reply: { ok: false, status: 500 } }, { fail: new Error("down") }, {}]) {
      const ctx = setup(options);
      const res = await post(ctx, { ...GOOD, code: "AAAAAAAAAAAAAAAA" });
      expect(res.status).toBe(200);
      expect(res.json).toEqual({ ok: true, message: "Received." });
      expect(ctx.calls).toHaveLength(0);
      const other = await post(setup(options), { ...GOOD, user_id: "123456789" + "012345679" });
      expect(other.text).toBe(res.text);
    }
  });

  test("a failed delivery is a 502 and the log never holds the secret, code, answers or URL", async () => {
    const body = { ...GOOD, what: "a very identifiable answer", why: "another identifiable answer" };
    const failures = [
      setup({ reply: { ok: false, status: 429 } }),
      setup({ reply: { ok: false, status: 404 } }),
      setup({ fail: Object.assign(new Error(`connect failed ${WEBHOOK}`), { name: "TimeoutError" }) }),
    ];
    for (const ctx of failures) {
      const res = await post(ctx, body);
      expect(res.status).toBe(502);
      expect(res.json.error).toBe("delivery");
      expect(ctx.logs).toHaveLength(1);
      for (const secret of [SECRET, CODE, WEBHOOK, "fixture_token", body.what, body.why, USER]) {
        expect(ctx.logs[0].includes(secret), ctx.logs[0]).toBe(false);
        expect(res.text.includes(secret)).toBe(false);
      }
    }
  });

  test("the route handler answers every method and is off when unconfigured", async () => {
    for (const name of ["POST", "GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"] as const) {
      expect(typeof route[name], name).toBe("function");
    }
    if (!process.env.APPEAL_SECRET) {
      const res = await route.POST(fakeReq(GOOD));
      expect(res.status).toBe(503);
    }
    expect(route.dynamic).toBe("force-dynamic");
  });
});
