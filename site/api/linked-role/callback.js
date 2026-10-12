// GET /api/linked-role/callback: Discord's OAuth2 redirect. Checks the state against the signed
// cookie, exchanges the code, reads the member's connected accounts once, writes "which game
// platforms are verified" to Discord and ends on /linked-role with a generic flag.
//
// The access token lives in one local variable for the length of this request. It is not
// stored, logged or revoked: Discord revokes the whole authorization along with a token, and
// whether the linked role survives that is not documented, so the token is simply dropped (it
// expires by itself). The state cookie is cleared on every outcome, so a replayed callback URL
// finds no cookie; Discord also refuses a code the second time.
"use strict";

const lib = require("./_lib.js");

function first(value) {
  return typeof value === "string" ? value : null; // a repeated parameter is refused, not guessed at
}

function queryOf(req) {
  const params = new URL(req.url || "/", "https://site.invalid").searchParams;
  const one = (name) => (params.getAll(name).length === 1 ? first(params.get(name)) : null);
  return { code: one("code"), state: one("state"), error: one("error"), hasError: params.has("error") };
}

function create({ env = process.env, fetch: fetchImpl = globalThis.fetch, now = Date.now, log = console.error } = {}) {
  return async function callback(req, res) {
    if (req.method !== "GET") return lib.wrongMethod(res);
    const config = lib.readConfig(env);
    if (!config) return lib.notSetUp(res);

    const cookie = lib.cookieFrom(req.headers && req.headers.cookie);
    res.setHeader("Set-Cookie", lib.clearCookieHeader()); // single use, whatever happens next
    const { code, state, error, hasError } = queryOf(req);

    const expected = lib.readStateCookie(config.stateSecret, cookie, now());
    if (!expected) return lib.finish(res, "expired"); // no cookie, tampered, too old, or a replay
    if (!state || !lib.sameString(state, expected)) return lib.finish(res, "failed");
    if (hasError) return lib.finish(res, error === "access_denied" ? "cancelled" : "failed");
    if (!code || !lib.CODE_SHAPE.test(code)) return lib.finish(res, "failed");

    try {
      const token = await lib.exchangeCode(fetchImpl, config, code);
      const connections = await lib.fetchConnections(fetchImpl, token);
      await lib.writeRoleConnection(fetchImpl, config, token, lib.computeRoleConnection(connections));
    } catch (err) {
      lib.logFailure(log, err);
      return lib.finish(res, err instanceof lib.DiscordError && err.status === 429 ? "busy" : "failed");
    }
    return lib.finish(res, "ok");
  };
}

module.exports = create();
module.exports.create = create;
