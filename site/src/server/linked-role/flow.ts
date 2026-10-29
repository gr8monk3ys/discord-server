// The two halves of the Linked Roles flow, as Request -> Response handlers. The route files
// in src/app/api/linked-role/ wire them to the real environment, clock and fetch; the tests
// inject fakes.
import * as lib from "./lib";

type Env = Record<string, string | undefined>;

/** GET /api/linked-role/start: where Discord sends a member who presses "Link" on a linked
 *  role (the application's Linked Roles Verification URL). Sets a signed, short-lived state
 *  cookie and sends the member to Discord's consent screen. Takes no input at all. */
export function createStart({
  env = process.env as Env,
  now = Date.now,
  randomBytes,
}: { env?: Env; now?: () => number; randomBytes?: (n: number) => Buffer } = {}) {
  return function start(req: Request): Response {
    if (req.method !== "GET" && req.method !== "HEAD") return lib.wrongMethod();
    const config = lib.readConfig(env);
    if (!config) return lib.notSetUp();

    const state = lib.newState(randomBytes);
    const query = new URLSearchParams({
      response_type: "code",
      client_id: config.clientId,
      scope: lib.SCOPES.join(" "),
      redirect_uri: config.redirectUri,
      state,
      prompt: "consent",
    });
    return lib.redirect(`${lib.AUTHORIZE_URL}?${query}`, {
      "Set-Cookie": lib.setCookieHeader(lib.stateCookieValue(config.stateSecret, state, now())),
    });
  };
}

function queryOf(req: Request) {
  const params = new URL(req.url || "/", "https://site.invalid").searchParams;
  // a repeated parameter is refused, not guessed at
  const one = (name: string) => (params.getAll(name).length === 1 ? params.get(name) : null);
  return { code: one("code"), state: one("state"), error: one("error"), hasError: params.has("error") };
}

/** GET /api/linked-role/callback: Discord's OAuth2 redirect. Checks the state against the
 *  signed cookie, exchanges the code, reads the member's connected accounts once, writes
 *  "which game platforms are verified" to Discord and ends on /linked-role with a generic flag.
 *
 *  The access token lives in one local variable for the length of this request. It is not
 *  stored, logged or revoked: Discord revokes the whole authorization along with a token, and
 *  whether the linked role survives that is not documented, so the token is simply dropped (it
 *  expires by itself). The state cookie is cleared on every outcome, so a replayed callback URL
 *  finds no cookie; Discord also refuses a code the second time. */
export function createCallback({
  env = process.env as Env,
  fetch: fetchImpl = globalThis.fetch as unknown as lib.FetchLike,
  now = Date.now,
  log = console.error,
}: { env?: Env; fetch?: lib.FetchLike; now?: () => number; log?: (line: string) => void } = {}) {
  return async function callback(req: Request): Promise<Response> {
    if (req.method !== "GET") return lib.wrongMethod();
    const config = lib.readConfig(env);
    if (!config) return lib.notSetUp();

    const cookie = lib.cookieFrom(req.headers.get("cookie"));
    const clear = { "Set-Cookie": lib.clearCookieHeader() }; // single use, whatever happens next
    const { code, state, error, hasError } = queryOf(req);

    const expected = lib.readStateCookie(config.stateSecret, cookie, now());
    if (!expected) return lib.finish("expired", clear); // no cookie, tampered, too old, or a replay
    if (!state || !lib.sameString(state, expected)) return lib.finish("failed", clear);
    if (hasError) return lib.finish(error === "access_denied" ? "cancelled" : "failed", clear);
    if (!code || !lib.CODE_SHAPE.test(code)) return lib.finish("failed", clear);

    try {
      const token = await lib.exchangeCode(fetchImpl, config, code);
      const connections = await lib.fetchConnections(fetchImpl, token);
      await lib.writeRoleConnection(fetchImpl, config, token, lib.computeRoleConnection(connections));
    } catch (err) {
      lib.logFailure(log, err);
      return lib.finish(err instanceof lib.DiscordError && err.status === 429 ? "busy" : "failed", clear);
    }
    return lib.finish("ok", clear);
  };
}
