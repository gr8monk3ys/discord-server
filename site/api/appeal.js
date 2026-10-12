// POST /api/appeal: the ban-appeal form's only backend. A Vercel Node function with
// no dependencies (CommonJS: the site has no package.json, so .js files are CommonJS).
//
// A banned member sends their Discord user ID, the appeal code Front Desk DMed them
// and three short answers. The code is base32(HMAC-SHA256(APPEAL_SECRET, userId))[:16]
// (bot/logic/appeals.py computes the same), so only someone holding a real code gets
// anything forwarded: the request is turned into a small JSON payload and posted to
// a Discord webhook in a private staff channel, where the bot verifies it again.
//
// Rules this file keeps:
// - Off (503) unless APPEAL_SECRET and APPEAL_WEBHOOK_URL are both set and sane.
// - POST with a JSON body only, from this site's own origin, at most 8 KB.
// - A wrong code gets the same 200 as a right one, so the endpoint can't be used to
//   test user IDs or codes. Every refusal for a malformed request is decided before the
//   code is compared, so those are the same for both too. The one answer only a right
//   code can get is 502 when Discord did not take the appeal (see the handler for why
//   that gives nothing away).
// - Never logs the secret, the code, the answers or the webhook URL.

"use strict";

const crypto = require("node:crypto");

const SITE_ORIGIN = "https://discord.lscaturchio.xyz";
const BODY_MAX = 8 * 1024; // bytes
const PAYLOAD_MAX = 4000; // characters; a Discord embed description holds 4096
const SECRET_MIN = 16;
const CODE_LEN = 16;
const FORWARD_TIMEOUT_MS = 5000;
// key, most characters, required. Mirrored in appeal/appeal.js and bot/logic/appeals.py.
const FIELDS = [["what", 600, true], ["why", 600, true], ["else", 300, false]];
const WEBHOOK_RE = /^https:\/\/discord\.com\/api\/webhooks\/[0-9]{15,22}\/[A-Za-z0-9_-]{20,200}$/;
const ORIGIN_RE = /^https:\/\/[a-z0-9.-]+(?::[0-9]{1,5})?$/;
const USER_ID_RE = /^[1-9][0-9]{14,19}$/;
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

const MESSAGES = {
  unavailable: "Appeals aren't open on this site right now. Please try again another day.",
  method: "This address only accepts the appeal form.",
  origin: "Please send the form from the appeal page itself.",
  type: "The form must be sent as JSON.",
  size: "That is too much text. Please shorten your answers.",
  invalid: "Some fields need another look.",
  delivery: "Your appeal could not be passed on just now. Nothing was lost on your side: please try again in a few minutes.",
  received: "Received.",
};

function base32(buf) {
  let bits = "";
  for (const byte of buf) bits += byte.toString(2).padStart(8, "0");
  let out = "";
  for (let i = 0; i + 5 <= bits.length; i += 5) out += B32[parseInt(bits.slice(i, i + 5), 2)];
  return out;
}

function appealCode(secret, userId) {
  const digest = crypto.createHmac("sha256", secret).update(String(userId), "utf8").digest();
  return base32(digest).slice(0, CODE_LEN);
}

// Upper-case with spaces and dashes removed, or null if it can't be a code.
function normalizeCode(text) {
  if (typeof text !== "string" || text.length > 64) return null;
  const code = text.replace(/[\s-]/g, "").toUpperCase();
  return /^[A-Z2-7]{16}$/.test(code) ? code : null;
}

function codeMatches(secret, userId, code) {
  const want = Buffer.from(appealCode(secret, userId), "ascii");
  const got = Buffer.from(String(code), "ascii");
  return want.length === got.length && crypto.timingSafeEqual(want, got);
}

// Read once at cold start. null means the feature is off.
function readConfig(env) {
  const secret = String(env.APPEAL_SECRET || "").trim();
  const webhookUrl = String(env.APPEAL_WEBHOOK_URL || "").trim();
  if (secret.length < SECRET_MIN || !WEBHOOK_RE.test(webhookUrl)) return null;
  const custom = String(env.APPEAL_ORIGIN || "").trim().replace(/\/+$/, "");
  return { secret, webhookUrl, origin: ORIGIN_RE.test(custom) ? custom : SITE_ORIGIN };
}

// Control and invisible format characters out, paragraphs kept, no runs of blank lines.
function cleanAnswer(text) {
  return String(text)
    .replace(/\r\n?/g, "\n")
    .replace(/[^\S\n]+/g, " ")
    .replace(/[\p{Cc}\p{Cf}\p{Cs}\p{Co}]/gu, (ch) => (ch === "\n" ? ch : ""))
    .replace(/ ?\n ?/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// { userId, code, answers } or { fields: { name: problem } }. Never echoes a value.
function validate(body) {
  const fields = {};
  if (body === null || typeof body !== "object" || Array.isArray(body)) return { fields: { form: "shape" } };
  const allowed = new Set(["user_id", "code", ...FIELDS.map((f) => f[0])]);
  if (Object.keys(body).some((k) => !allowed.has(k))) return { fields: { form: "shape" } };

  const userId = typeof body.user_id === "string" ? body.user_id.trim() : "";
  if (!USER_ID_RE.test(userId)) fields.user_id = "format";
  const code = normalizeCode(body.code);
  if (code === null) fields.code = "format";

  const answers = [];
  for (const [key, max, required] of FIELDS) {
    const raw = body[key] === undefined ? "" : body[key];
    if (typeof raw !== "string") { fields[key] = "format"; continue; }
    if (Array.from(raw).length > max) { fields[key] = "too_long"; continue; }
    const text = cleanAnswer(raw);
    if (required && !text) { fields[key] = "required"; continue; }
    if (/:\/\/|\bwww\./i.test(text)) { fields[key] = "links"; continue; }
    answers.push(text);
  }
  return Object.keys(fields).length ? { fields } : { userId, code, answers };
}

// A JSON string literal whose markdown, mention and link characters are written as \u
// escapes: still plain JSON for the bot, and inert text if a person sees it in Discord
// before the bot deletes the webhook message.
function inertString(text) {
  return JSON.stringify(text).replace(/[<>@[\]()`*_~|#:/&]/g, (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"));
}

function buildPayload(userId, code, answers) {
  return `{"v":1,"u":"${userId}","c":"${code}","a":[${answers.map(inertString).join(",")}]}`;
}

function header(req, name) {
  const value = req.headers ? req.headers[name] : undefined;
  return Array.isArray(value) ? value[0] : value;
}

// The parsed JSON body, or { status } to refuse. Vercel's Node helpers expose the body
// as req.body (a getter that throws on bad JSON); elsewhere the stream is read here.
async function readBody(req) {
  const declared = Number(header(req, "content-length"));
  if (Number.isFinite(declared) && declared > BODY_MAX) return { status: 413 };
  let body;
  try {
    body = req.body;
  } catch {
    return { status: 400 };
  }
  if (body === undefined) {
    const chunks = [];
    let size = 0;
    try {
      for await (const chunk of req) {
        size += chunk.length;
        if (size > BODY_MAX) return { status: 413 };
        chunks.push(Buffer.from(chunk));
      }
    } catch {
      return { status: 400 };
    }
    body = Buffer.concat(chunks);
  }
  if (Buffer.isBuffer(body)) body = body.toString("utf8");
  if (typeof body === "string") {
    if (Buffer.byteLength(body, "utf8") > BODY_MAX) return { status: 413 };
    try {
      body = JSON.parse(body);
    } catch {
      return { status: 400 };
    }
  }
  if (body === null || typeof body !== "object") return { status: 400 };
  try {
    if (Buffer.byteLength(JSON.stringify(body), "utf8") > BODY_MAX) return { status: 413 };
  } catch {
    return { status: 400 };
  }
  return { body };
}

function send(res, status, error, extra) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("X-Content-Type-Options", "nosniff");
  const ok = status === 200;
  res.end(JSON.stringify({ ok, ...(ok ? {} : { error }), message: MESSAGES[ok ? "received" : error], ...extra }));
}

// config: readConfig(...) or null. fetchImpl and log are injected by the tests.
function createHandler({ config, fetchImpl, log }) {
  const say = log || ((line) => console.warn(line));
  return async function handler(req, res) {
    if (req.method !== "POST") {
      res.setHeader("Allow", "POST");
      return send(res, 405, "method");
    }
    if (!config) return send(res, 503, "unavailable");
    const fetchSite = header(req, "sec-fetch-site");
    if (header(req, "origin") !== config.origin || (fetchSite !== undefined && fetchSite !== "same-origin")) {
      return send(res, 403, "origin");
    }
    if (!/^application\/json\s*(;|$)/i.test(String(header(req, "content-type") || ""))) return send(res, 415, "type");

    const read = await readBody(req);
    if (read.status === 413) return send(res, 413, "size");
    if (read.status) return send(res, 400, "invalid", { fields: { form: "shape" } });
    const input = validate(read.body);
    if (input.fields) return send(res, 400, "invalid", { fields: input.fields });

    // Every check on the request's shape and size comes before the code is looked at, the
    // escaped length included: a malformed request gets the same refusal with a right code
    // as with a wrong one.
    const payload = buildPayload(input.userId, input.code, input.answers);
    if (payload.length > PAYLOAD_MAX) return send(res, 400, "invalid", { fields: { form: "too_long" } });

    // Same answer whether or not the code is right: no way to probe IDs or codes.
    if (!codeMatches(config.secret, input.userId, input.code)) return send(res, 200);

    // From here on the sender holds the real code. A failed delivery says so (502) instead of
    // "Received": a banned member must not believe an appeal arrived that was lost. It tells
    // nobody anything new. The only fact it gives away is "this code is this ID's code", which
    // takes the real code to trigger; the code is 80 bits of an HMAC, so it cannot be searched
    // for, and whoever holds it was sent it by the bot and knows it is right. It says nothing
    // about whether the ID is banned: the code depends on the ID alone.
    try {
      const reply = await (fetchImpl || fetch)(config.webhookUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: "Appeal form",
          embeds: [{ description: payload }],
          allowed_mentions: { parse: [] },
        }),
        redirect: "error",
        signal: AbortSignal.timeout(FORWARD_TIMEOUT_MS),
      });
      if (!reply.ok) {
        say(`appeal: webhook answered ${reply.status}`);
        return send(res, 502, "delivery");
      }
    } catch (err) {
      say(`appeal: webhook request failed (${err && err.name ? err.name : "error"})`);
      return send(res, 502, "delivery");
    }
    return send(res, 200);
  };
}

module.exports = createHandler({ config: readConfig(process.env) });
Object.assign(module.exports, {
  createHandler, readConfig, appealCode, normalizeCode, cleanAnswer, validate, buildPayload, inertString,
  BODY_MAX, PAYLOAD_MAX, SITE_ORIGIN, FIELDS,
});
