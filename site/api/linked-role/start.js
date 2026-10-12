// GET /api/linked-role/start: where Discord sends a member who presses "Link" on a linked
// role (the application's Linked Roles Verification URL). Sets a signed, short-lived state
// cookie and sends the member to Discord's consent screen. Takes no input at all.
"use strict";

const lib = require("./_lib.js");

function create({ env = process.env, now = Date.now, randomBytes } = {}) {
  return function start(req, res) {
    if (req.method !== "GET" && req.method !== "HEAD") return lib.wrongMethod(res);
    const config = lib.readConfig(env);
    if (!config) return lib.notSetUp(res);

    const state = lib.newState(randomBytes);
    const query = new URLSearchParams({
      response_type: "code",
      client_id: config.clientId,
      scope: lib.SCOPES.join(" "),
      redirect_uri: config.redirectUri,
      state,
      prompt: "consent",
    });
    res.setHeader("Set-Cookie", lib.setCookieHeader(lib.stateCookieValue(config.stateSecret, state, now())));
    return lib.redirect(res, `${lib.AUTHORIZE_URL}?${query}`);
  };
}

module.exports = create();
module.exports.create = create;
