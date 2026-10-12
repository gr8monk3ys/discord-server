// Tests for api/appeal.js: run with `node --test site/api/` (Node 20+). No network:
// the handler gets a fake request, a fake response and a fake fetch.
// Kept out of the Vercel deploy by .vercelignore, so it never becomes a function.

import assert from "node:assert/strict";
import { Readable } from "node:stream";
import { test } from "node:test";

import handlerModule from "./appeal.js";

const { createHandler, readConfig, appealCode, normalizeCode, cleanAnswer, validate, buildPayload, inertString,
  BODY_MAX, PAYLOAD_MAX, SITE_ORIGIN } = handlerModule;

// The shared vector: bot/tests/test_appeals.py asserts the same literal for the same inputs.
const SECRET = "test-secret-for-appeal-vectors!!";
const USER = "123456789012345678";
const CODE = "TD7YUL66OYCHIZTL";
// Assembled from parts so secret scanners don't mistake the fixture for a live webhook.
const WEBHOOK = ["https://discord.com/api", "webhooks", "1".padEnd(18, "0"), "fixture_token-".padEnd(48, "x")].join("/");
const ENV = { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK };
const GOOD = { user_id: USER, code: "td7y-ul66-oych-iztl", what: "I spammed the chat.", why: "I read the rules.", else: "" };

function fakeRes() {
  return {
    statusCode: 0, headers: {}, text: null,
    setHeader(k, v) { this.headers[k.toLowerCase()] = v; },
    end(text) { this.text = text; },
    get json() { return JSON.parse(this.text); },
  };
}

function fakeReq(body, { method = "POST", headers = {}, raw } = {}) {
  const text = raw !== undefined ? raw : JSON.stringify(body);
  const req = Readable.from(text === "" ? [] : [Buffer.from(text)]);
  req.method = method;
  req.headers = { origin: SITE_ORIGIN, "content-type": "application/json", "content-length": String(Buffer.byteLength(text)), ...headers };
  return req;
}

function setup({ env = ENV, reply = { ok: true, status: 204 }, fail = null } = {}) {
  const calls = [];
  const logs = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (fail) throw fail;
    return reply;
  };
  const handler = createHandler({ config: readConfig(env), fetchImpl, log: (line) => logs.push(line) });
  return { handler, calls, logs };
}

async function post(ctx, body, options) {
  const res = fakeRes();
  await ctx.handler(fakeReq(body, options), res);
  return res;
}

test("the code matches the Python implementation's shared vector", () => {
  assert.equal(appealCode(SECRET, USER), CODE);
  assert.equal(appealCode(SECRET, "987654321098765432"), "TQRXMXY43NDFCQDZ");
  assert.notEqual(appealCode(SECRET + "x", USER), CODE);
});

test("codes are normalised and anything else is refused", () => {
  assert.equal(normalizeCode(" td7y-ul66 oych-iztl "), CODE);
  for (const bad of ["", "TD7YUL66OYCHIZT", CODE + "A", "TD7YUL66OYCHIZT1", "TD7YUL66OYCHIZT!", null, 5, "A".repeat(80)]) {
    assert.equal(normalizeCode(bad), null, String(bad));
  }
});

test("a valid appeal is forwarded once, compactly, with no mentions allowed", async () => {
  const ctx = setup();
  const res = await post(ctx, GOOD);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.json, { ok: true, message: "Received." });
  assert.equal(res.headers["cache-control"], "no-store");
  assert.equal(ctx.calls.length, 1);
  const { url, init } = ctx.calls[0];
  assert.equal(url, WEBHOOK);
  assert.equal(init.method, "POST");
  assert.equal(init.redirect, "error");
  const sent = JSON.parse(init.body);
  assert.deepEqual(sent.allowed_mentions, { parse: [] });
  assert.equal(sent.content, undefined);
  assert.equal(sent.embeds.length, 1);
  assert.deepEqual(JSON.parse(sent.embeds[0].description),
    { v: 1, u: USER, c: CODE, a: ["I spammed the chat.", "I read the rules.", ""] });
  assert.deepEqual(ctx.logs, []);
});

test("a wrong code gets the same answer as a right one and nothing is forwarded", async () => {
  const good = await post(setup(), GOOD);
  for (const body of [{ ...GOOD, code: "AAAAAAAAAAAAAAAA" }, { ...GOOD, user_id: "123456789012345679" }]) {
    const ctx = setup();
    const res = await post(ctx, body);
    assert.equal(res.statusCode, 200);
    assert.equal(res.text, good.text);
    assert.deepEqual(res.headers, good.headers);
    assert.equal(ctx.calls.length, 0);
    assert.deepEqual(ctx.logs, []);
  }
});

test("off with a friendly 503 unless both settings are present and sane", async () => {
  const off = [
    {}, { APPEAL_SECRET: SECRET }, { APPEAL_WEBHOOK_URL: WEBHOOK },
    { APPEAL_SECRET: "short", APPEAL_WEBHOOK_URL: WEBHOOK },
    { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK.replace("https:", "http:") },
    { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK.replace("discord.com", "discord.com.evil.example") },
    { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK.replace("discord.com", "evil.example") },
    { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: WEBHOOK + "?wait=true" },
    { APPEAL_SECRET: SECRET, APPEAL_WEBHOOK_URL: "https://discord.com/api/users/@me" },
  ];
  for (const env of off) {
    assert.equal(readConfig(env), null);
    const ctx = setup({ env });
    const res = await post(ctx, GOOD);
    assert.equal(res.statusCode, 503);
    assert.equal(res.json.ok, false);
    assert.match(res.json.message, /aren't open/);
    assert.equal(ctx.calls.length, 0);
  }
});

test("POST only", async () => {
  for (const method of ["GET", "PUT", "DELETE", "OPTIONS", "HEAD"]) {
    const ctx = setup();
    const res = await post(ctx, GOOD, { method });
    assert.equal(res.statusCode, 405);
    assert.equal(res.headers.allow, "POST");
    assert.equal(ctx.calls.length, 0);
  }
});

test("only this site's own origin may post", async () => {
  const bad = [{ origin: "https://evil.example" }, { origin: undefined }, { origin: "null" },
    { origin: SITE_ORIGIN + ".evil.example" }, { origin: SITE_ORIGIN.replace("https", "http") },
    { "sec-fetch-site": "cross-site" }, { "sec-fetch-site": "same-site" }];
  for (const headers of bad) {
    const ctx = setup();
    const res = await post(ctx, GOOD, { headers });
    assert.equal(res.statusCode, 403, JSON.stringify(headers));
    assert.equal(ctx.calls.length, 0);
  }
  assert.equal((await post(setup(), GOOD, { headers: { "sec-fetch-site": "same-origin" } })).statusCode, 200);
  const custom = setup({ env: { ...ENV, APPEAL_ORIGIN: "https://staging.example/" } });
  assert.equal((await post(custom, GOOD)).statusCode, 403);
  assert.equal((await post(custom, GOOD, { headers: { origin: "https://staging.example" } })).statusCode, 200);
});

test("JSON only", async () => {
  for (const type of ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", undefined, "application/jsonp"]) {
    const ctx = setup();
    const res = await post(ctx, GOOD, { headers: { "content-type": type } });
    assert.equal(res.statusCode, 415, String(type));
    assert.equal(ctx.calls.length, 0);
  }
  assert.equal((await post(setup(), GOOD, { headers: { "content-type": "application/json; charset=utf-8" } })).statusCode, 200);
  for (const raw of ["", "{", "[1,2]x", "null", "7", '"text"']) {
    const res = await post(setup(), null, { raw });
    assert.equal(res.statusCode, 400, raw);
  }
});

test("the body is capped by header, by stream and after parsing", async () => {
  const huge = { ...GOOD, what: "a".repeat(BODY_MAX) };
  let ctx = setup();
  assert.equal((await post(ctx, huge)).statusCode, 413);
  ctx = setup();
  assert.equal((await post(ctx, huge, { headers: { "content-length": "10" } })).statusCode, 413); // a lying header
  ctx = setup();
  const res = fakeRes();
  const req = { method: "POST", headers: { origin: SITE_ORIGIN, "content-type": "application/json" }, body: huge };
  await ctx.handler(req, res); // Vercel's helpers already parsed it
  assert.equal(res.statusCode, 413);
  assert.equal(ctx.calls.length, 0);
});

test("a body already parsed by the platform works, and a throwing body getter is a 400", async () => {
  const ctx = setup();
  const res = fakeRes();
  await ctx.handler({ method: "POST", headers: { origin: SITE_ORIGIN, "content-type": "application/json" }, body: GOOD }, res);
  assert.equal(res.statusCode, 200);
  assert.equal(ctx.calls.length, 1);
  const bad = fakeRes();
  const req = { method: "POST", headers: { origin: SITE_ORIGIN, "content-type": "application/json" } };
  Object.defineProperty(req, "body", { get() { throw new Error("Invalid JSON"); } });
  await ctx.handler(req, bad);
  assert.equal(bad.statusCode, 400);
});

test("fields are validated strictly and never echoed", async () => {
  const cases = [
    [{ ...GOOD, user_id: "12345" }, { user_id: "format" }],
    [{ ...GOOD, user_id: "0123456789012345678" }, { user_id: "format" }],
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
    assert.equal(res.statusCode, 400, JSON.stringify(fields));
    assert.deepEqual(res.json.fields, fields);
    assert.equal(ctx.calls.length, 0);
    for (const value of Object.values(body)) {
      if (typeof value === "string" && value.length > 8) assert.ok(!res.text.includes(value));
    }
  }
  assert.equal(validate([]).fields.form, "shape");
  assert.equal(validate(null).fields.form, "shape");
  const emoji = "\u{1F600}".repeat(600); // 600 characters, 1200 UTF-16 units: allowed
  assert.deepEqual(validate({ ...GOOD, what: emoji }).answers[0], emoji);
});

test("answers are cleaned: control and invisible characters, blank-line runs, odd spacing", () => {
  assert.equal(cleanAnswer("  a\tb\u0000c\u200bd\u202ee  "), "a bcde");
  assert.equal(cleanAnswer("one\r\n\r\n\r\n\r\ntwo \n three"), "one\n\ntwo\nthree");
  assert.equal(cleanAnswer("\u0007\u200d"), "");
});

test("hostile text is inert in the payload and survives the round trip", async () => {
  const nasty = "@everyone <@1> <@&2> [x](javascript:alert(1)) **b** `c` ~~d~~ ||e|| # h :tada: a/b &amp; \"q\" \\ back\nline";
  const payload = buildPayload(USER, CODE, [nasty, "ok", ""]);
  assert.equal(JSON.parse(payload).a[0], nasty);
  const literal = inertString(nasty);
  for (const ch of "<>@[]()`*_~|#:/&") assert.ok(!literal.includes(ch), ch);
  assert.equal(JSON.parse(literal), nasty);
  const ctx = setup();
  const res = await post(ctx, { ...GOOD, what: nasty });
  assert.equal(res.statusCode, 200);
  const description = JSON.parse(ctx.calls[0].init.body).embeds[0].description;
  assert.ok(!description.includes("@everyone") && !description.includes("](") && !description.includes("**"));
  assert.equal(JSON.parse(description).a[0], nasty);
});

test("a payload too big for one embed is refused, not truncated", async () => {
  const ctx = setup();
  const res = await post(ctx, { ...GOOD, what: "*".repeat(600), why: "_".repeat(600) });
  assert.equal(res.statusCode, 400);
  assert.deepEqual(res.json.fields, { form: "too_long" });
  assert.equal(ctx.calls.length, 0);
  assert.ok(buildPayload(USER, CODE, ["a".repeat(600), "b".repeat(600), "c".repeat(300)]).length < PAYLOAD_MAX);
});

test("a malformed request is refused the same way with a right code and with a wrong one", async () => {
  // Nothing about the refusal may depend on the code: otherwise each of these would be a
  // way to test a code (or a user ID) without ever sending a real appeal.
  const WRONG = "AAAAAAAAAAAAAAAA";
  const malformed = [
    { what: "*".repeat(600), why: "_".repeat(600) }, // fits the field limits, too long once escaped
    { what: "x".repeat(601) },
    { what: "" },
    { why: "see https://example.com" },
    { else: 5 },
    { extra: "field" },
  ];
  for (const change of malformed) {
    const answers = [];
    for (const code of [GOOD.code, WRONG]) {
      for (const failing of [false, true]) { // nor on whether Discord would have taken it
        const ctx = setup(failing ? { reply: { ok: false, status: 500 } } : {});
        const res = await post(ctx, { ...GOOD, ...change, code });
        assert.equal(res.statusCode, 400, JSON.stringify(change));
        assert.equal(ctx.calls.length, 0);
        assert.deepEqual(ctx.logs, []);
        answers.push(JSON.stringify([res.statusCode, res.headers, res.text]));
      }
    }
    assert.equal(new Set(answers).size, 1, JSON.stringify(change));
  }
  // the same holds for a body that is too large or not JSON at all
  for (const options of [{ raw: "{not json" }, { raw: JSON.stringify({ ...GOOD, what: "x".repeat(BODY_MAX) }) }]) {
    const right = await post(setup(), GOOD, options);
    const wrong = await post(setup(), { ...GOOD, code: WRONG }, options.raw.startsWith("{not")
      ? options : { raw: JSON.stringify({ ...GOOD, code: WRONG, what: "x".repeat(BODY_MAX) }) });
    assert.deepEqual([right.statusCode, right.text], [wrong.statusCode, wrong.text]);
    assert.ok(right.statusCode === 400 || right.statusCode === 413);
  }
});

test("a well-formed appeal with a wrong code is 'received' even while Discord is failing", async () => {
  // The 502 below takes the real code to trigger, so it tells its sender nothing they did
  // not already hold; without the code the answer never changes.
  for (const options of [{ reply: { ok: false, status: 500 } }, { fail: new Error("down") }, {}]) {
    const ctx = setup(options);
    const res = await post(ctx, { ...GOOD, code: "AAAAAAAAAAAAAAAA" });
    assert.equal(res.statusCode, 200);
    assert.deepEqual(res.json, { ok: true, message: "Received." });
    assert.equal(ctx.calls.length, 0);
    const other = await post(setup(options), { ...GOOD, user_id: "123456789012345679" });
    assert.equal(other.text, res.text);
  }
});

test("a failed delivery is a 502 and the log never holds the secret, code, answers or URL", async () => {
  const body = { ...GOOD, what: "a very identifiable answer", why: "another identifiable answer" };
  const failures = [setup({ reply: { ok: false, status: 429 } }), setup({ reply: { ok: false, status: 404 } }),
    setup({ fail: Object.assign(new Error(`connect failed ${WEBHOOK}`), { name: "TimeoutError" }) })];
  for (const ctx of failures) {
    const res = await post(ctx, body);
    assert.equal(res.statusCode, 502);
    assert.equal(res.json.error, "delivery");
    assert.equal(ctx.logs.length, 1);
    for (const secret of [SECRET, CODE, WEBHOOK, "fixture_token", body.what, body.why, USER]) {
      assert.ok(!ctx.logs[0].includes(secret), ctx.logs[0]);
      assert.ok(!res.text.includes(secret));
    }
  }
});

test("the default export is the handler Vercel calls, off when unconfigured", async () => {
  assert.equal(typeof handlerModule, "function");
  if (!process.env.APPEAL_SECRET) {
    const res = fakeRes();
    await handlerModule(fakeReq(GOOD), res);
    assert.equal(res.statusCode, 503);
  }
});
