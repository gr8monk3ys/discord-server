// What /linked-role says for each ?r= flag the Linked Roles callback can send
// (src/server/linked-role/lib.ts FLAGS), plus the default for a visit without one.

export const RESULTS = {
  default: [
    "Linked roles start in Discord.",
    "Open the server, click its name, choose Linked Roles and press Link on a role. Front Desk then checks which game platforms are verified on your Discord account.",
  ],
  ok: [
    "You're linked.",
    "Discord now knows which game platforms are verified on your account. Go back to Discord: a role you qualify for is yours. If one stays locked, add that platform under User Settings, Connections, then press Link again.",
  ],
  cancelled: ["Cancelled.", "You said no on Discord's screen, so nothing was read and nothing changed."],
  expired: [
    "That took too long.",
    "The sign-in window lasts 10 minutes and works once. Start again from the Linked Roles menu in Discord.",
  ],
  busy: ["Discord is busy.", "Discord asked us to slow down. Give it a minute, then press Link again."],
  failed: [
    "Something went wrong.",
    "Nothing was saved. Start again from the Linked Roles menu in Discord; if it keeps failing, tell a mod.",
  ],
} as const satisfies Record<string, readonly [string, string]>;

const FLAGS = ["ok", "cancelled", "expired", "busy", "failed"] as const;

/** The [title, text] for one flag, or null for anything else (missing, repeated, unknown). */
export function resultFor(flag: unknown): readonly [string, string] | null {
  if (typeof flag !== "string") return null;
  return (FLAGS as readonly string[]).includes(flag) ? RESULTS[flag as (typeof FLAGS)[number]] : null;
}
