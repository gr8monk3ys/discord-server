// Site-wide constants. The invite code and the live-data URL live only in
// src/data/site-config.json (bot/logic/textfilter.py reads the code from there too).
import siteConfig from "../data/site-config.json";
import security from "../data/security.json";

export const SITE_ORIGIN = "https://discord.lscaturchio.xyz";
export const CANONICAL = `${SITE_ORIGIN}/`;

export const INVITE_RE = /^[A-Za-z0-9-]{2,32}$/;

/** A bare invite code (not a URL), and not the REPLACE_ME placeholder. */
export function validInviteCode(code: unknown): code is string {
  return typeof code === "string" && INVITE_RE.test(code) && code !== "REPLACE_ME";
}

export const INVITE_CODE = String(siteConfig.inviteCode ?? "").trim();
export const INVITE_URL: string | null = validInviteCode(INVITE_CODE) ? `https://discord.gg/${INVITE_CODE}` : null;
export const STATS_URL = typeof siteConfig.statsUrl === "string" ? siteConfig.statsUrl.trim() : "";

/** The page Content-Security-Policy for one response. In development React needs eval for
 *  its error overlays and the dev server injects styles, so both are allowed there only. */
export function contentSecurityPolicy(nonce: string, dev = false): string {
  const parts: string[] = [];
  for (const [directive, sources] of Object.entries(security.csp)) {
    let list = sources.map((s) => s.replace("{nonce}", nonce));
    if (dev && directive === "script-src") list = [...list, "'unsafe-eval'"];
    if (dev && directive === "style-src") list = list.filter((s) => !s.startsWith("'nonce-")).concat("'unsafe-inline'");
    parts.push([directive, ...list].join(" "));
  }
  return parts.join("; ");
}

export const SECURITY_HEADERS: Record<string, string> = security.headers;
export const ASSET_CSP: string = security.assetCsp;
