// POST /api/appeal: the ban-appeal form's only backend, served by the route handler in
// src/app/api/appeal/route.ts. No dependencies beyond node:crypto.
//
// A banned member sends their Discord user ID, the appeal code Front Desk DMed them
// and three short answers. The code is base32(HMAC-SHA256(APPEAL_SECRET, userId))[:16]
// (bot/logic/appeals.py computes the same), so only someone holding a real code gets
// anything forwarded: the request is turned into a small JSON payload and posted to
// a Discord webhook in a private staff channel, where the bot verifies it again.
//
// Rules this file keeps:
// - Off (503) unless APPEAL_SECRET and APPEAL_WEBHOOK_URL are both set and sane. They come
//   from the Vercel project's environment variables, never from a file in the repo.
// - POST with a JSON body only, from this site's own origin, at most 8 KB.
// - A wrong code gets the same 200 as a right one, so the endpoint can't be used to
//   test user IDs or codes. Every refusal for a malformed request is decided before the
//   code is compared, so those are the same for both too. The one answer only a right
//   code can get is 502 when Discord did not take the appeal (see the handler for why
//   that gives nothing away).
// - Never logs the secret, the code, the answers or the webhook URL.

import crypto from "node:crypto";

export const SITE_ORIGIN = "https://discord.lscaturchio.xyz";
export const BODY_MAX = 8 * 1024; // bytes
export const PAYLOAD_MAX = 4000; // characters; a Discord embed description holds 4096
export const SECRET_MIN = 16;
export const CODE_LEN = 16;
const FORWARD_TIMEOUT_MS = 5000;
// key, most characters, required. Mirrored in src/lib/appeal-form.ts and bot/logic/appeals.py.
export const FIELDS: [string, number, boolean][] = [["what", 600, true], ["why", 600, true], ["else", 300, false]];
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
} as const;
type ErrorKey = Exclude<keyof typeof MESSAGES, "received">;

export type AppealConfig = { secret: string; webhookUrl: string; origin: string };
type Env = Record<string, string | undefined>;
type FetchLike = (url: string, init: RequestInit) => Promise<{ ok: boolean; status: number }>;

function base32(buf: Uint8Array): string {
  let bits = "";
  for (const byte of buf) bits += byte.toString(2).padStart(8, "0");
  let out = "";
  for (let i = 0; i + 5 <= bits.length; i += 5) out += B32[parseInt(bits.slice(i, i + 5), 2)];
  return out;
}

export function appealCode(secret: string, userId: string): string {
  const digest = crypto.createHmac("sha256", secret).update(String(userId), "utf8").digest();
  return base32(digest).slice(0, CODE_LEN);
}

/** Upper-case with spaces and dashes removed, or null if it can't be a code. */
export function normalizeCode(text: unknown): string | null {
  if (typeof text !== "string" || text.length > 64) return null;
  const code = text.replace(/[\s-]/g, "").toUpperCase();
  return /^[A-Z2-7]{16}$/.test(code) ? code : null;
}

function codeMatches(secret: string, userId: string, code: string): boolean {
  const want = Buffer.from(appealCode(secret, userId), "ascii");
  const got = Buffer.from(String(code), "ascii");
  return want.length === got.length && crypto.timingSafeEqual(want, got);
}

/** null means the feature is off. */
export function readConfig(env: Env): AppealConfig | null {
  const secret = String(env.APPEAL_SECRET || "").trim();
  const webhookUrl = String(env.APPEAL_WEBHOOK_URL || "").trim();
  if (secret.length < SECRET_MIN || !WEBHOOK_RE.test(webhookUrl)) return null;
  const custom = String(env.APPEAL_ORIGIN || "").trim().replace(/\/+$/, "");
  return { secret, webhookUrl, origin: ORIGIN_RE.test(custom) ? custom : SITE_ORIGIN };
}

/** Control and invisible format characters out, paragraphs kept, no runs of blank lines. */
export function cleanAnswer(text: string): string {
  return String(text)
    .replace(/\r\n?/g, "\n")
    .replace(/[^\S\n]+/g, " ")
    .replace(/[\p{Cc}\p{Cf}\p{Cs}\p{Co}]/gu, (ch) => (ch === "\n" ? ch : ""))
    .replace(/ ?\n ?/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

type Valid = { userId: string; code: string; answers: string[]; fields?: undefined };
type Invalid = { fields: Record<string, string> };

/** { userId, code, answers } or { fields: { name: problem } }. Never echoes a value. */
export function validate(body: unknown): Valid | Invalid {
  const fields: Record<string, string> = {};
  if (body === null || typeof body !== "object" || Array.isArray(body)) return { fields: { form: "shape" } };
  const b = body as Record<string, unknown>;
  const allowed = new Set(["user_id", "code", ...FIELDS.map((f) => f[0])]);
  if (Object.keys(b).some((k) => !allowed.has(k))) return { fields: { form: "shape" } };

  const userId = typeof b.user_id === "string" ? b.user_id.trim() : "";
  if (!USER_ID_RE.test(userId)) fields.user_id = "format";
  const code = normalizeCode(b.code);
  if (code === null) fields.code = "format";

  const answers: string[] = [];
  for (const [key, max, required] of FIELDS) {
    const raw = b[key] === undefined ? "" : b[key];
    if (typeof raw !== "string") { fields[key] = "format"; continue; }
    if (Array.from(raw).length > max) { fields[key] = "too_long"; continue; }
    const text = cleanAnswer(raw);
    if (required && !text) { fields[key] = "required"; continue; }
    if (/:\/\/|\bwww\./i.test(text)) { fields[key] = "links"; continue; }
    answers.push(text);
  }
  return Object.keys(fields).length ? { fields } : { userId, code: code as string, answers };
}

/** A JSON string literal whose markdown, mention and link characters are written as \u
 *  escapes: still plain JSON for the bot, and inert text if a person sees it in Discord
 *  before the bot deletes the webhook message. */
export function inertString(text: string): string {
  return JSON.stringify(text).replace(/[<>@[\]()`*_~|#:/&]/g, (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"));
}

export function buildPayload(userId: string, code: string, answers: string[]): string {
  return `{"v":1,"u":"${userId}","c":"${code}","a":[${answers.map(inertString).join(",")}]}`;
}

/** The parsed JSON body, or { status } to refuse. The stream is read here with the cap
 *  applied as it arrives, so a lying Content-Length gains nothing. */
async function readBody(req: Request): Promise<{ body: object; status?: undefined } | { status: 400 | 413 }> {
  const declared = Number(req.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > BODY_MAX) return { status: 413 };
  const chunks: Uint8Array[] = [];
  let size = 0;
  if (req.body) {
    const reader = req.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > BODY_MAX) {
          reader.cancel().catch(() => {});
          return { status: 413 };
        }
        chunks.push(value);
      }
    } catch {
      return { status: 400 };
    }
  }
  let body: unknown;
  try {
    body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return { status: 400 };
  }
  if (body === null || typeof body !== "object") return { status: 400 };
  return { body };
}

function send(status: number, error?: ErrorKey, extra?: object, headers?: Record<string, string>): Response {
  const ok = status === 200;
  const text = JSON.stringify({ ok, ...(ok ? {} : { error }), message: MESSAGES[ok ? "received" : (error as ErrorKey)], ...extra });
  return new Response(text, {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...headers,
    },
  });
}

type HandlerOptions = { config: AppealConfig | null; fetchImpl?: FetchLike; log?: (line: string) => void };

/** config: readConfig(...) or null. fetchImpl and log are injected by the tests. */
export function createHandler({ config, fetchImpl, log }: HandlerOptions) {
  const say = log || ((line: string) => console.warn(line));
  return async function handler(req: Request): Promise<Response> {
    if (req.method !== "POST") return send(405, "method", undefined, { Allow: "POST" });
    if (!config) return send(503, "unavailable");
    const fetchSite = req.headers.get("sec-fetch-site");
    if (req.headers.get("origin") !== config.origin || (fetchSite !== null && fetchSite !== "same-origin")) {
      return send(403, "origin");
    }
    if (!/^application\/json\s*(;|$)/i.test(req.headers.get("content-type") || "")) return send(415, "type");

    const read = await readBody(req);
    if (read.status === 413) return send(413, "size");
    if (read.status) return send(400, "invalid", { fields: { form: "shape" } });
    const input = validate(read.body);
    if (input.fields) return send(400, "invalid", { fields: input.fields });

    // Every check on the request's shape and size comes before the code is looked at, the
    // escaped length included: a malformed request gets the same refusal with a right code
    // as with a wrong one.
    const payload = buildPayload(input.userId, input.code, input.answers);
    if (payload.length > PAYLOAD_MAX) return send(400, "invalid", { fields: { form: "too_long" } });

    // Same answer whether or not the code is right: no way to probe IDs or codes.
    if (!codeMatches(config.secret, input.userId, input.code)) return send(200);

    // From here on the sender holds the real code. A failed delivery says so (502) instead of
    // "Received": a banned member must not believe an appeal arrived that was lost. It tells
    // nobody anything new. The only fact it gives away is "this code is this ID's code", which
    // takes the real code to trigger; the code is 80 bits of an HMAC, so it cannot be searched
    // for, and whoever holds it was sent it by the bot and knows it is right. It says nothing
    // about whether the ID is banned: the code depends on the ID alone.
    try {
      const reply = await (fetchImpl || (fetch as unknown as FetchLike))(config.webhookUrl, {
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
        return send(502, "delivery");
      }
    } catch (err) {
      say(`appeal: webhook request failed (${err && (err as Error).name ? (err as Error).name : "error"})`);
      return send(502, "delivery");
    }
    return send(200);
  };
}
