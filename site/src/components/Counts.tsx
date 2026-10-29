"use client";

import { useEffect, useState } from "react";
import { getJson, loadStats } from "@/lib/live";
import { INVITE_CODE, INVITE_URL } from "@/lib/site";
import { fmt } from "@/lib/stats";

type Shown = { members: number; online: number | null };

/** Member and online counts under the hero's Join button. Discord's public invite endpoint
 *  first (it sends CORS headers, so the browser can read it); if that is unreachable, the
 *  bot's published member count stands in. Hidden until one of them answers. */
export function Counts() {
  const [shown, setShown] = useState<Shown | null>(null);

  useEffect(() => {
    let live = true;
    const show = (members: unknown, online: unknown) => {
      if (live && typeof members === "number") setShown({ members, online: typeof online === "number" ? online : null });
    };
    const fromStats = () =>
      loadStats().then((loaded) => {
        if (loaded && loaded.stats.members !== null) show(loaded.stats.members, loaded.stats.online);
      });
    if (!INVITE_URL || typeof fetch !== "function") {
      fromStats();
    } else {
      getJson(`https://discord.com/api/v10/invites/${encodeURIComponent(INVITE_CODE)}?with_counts=true`).then((raw) => {
        const data = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : null;
        const members = data ? data.approximate_member_count : null;
        const online = data ? data.approximate_presence_count : null;
        if (typeof members !== "number" || typeof online !== "number") fromStats(); // offline, blocked or expired invite
        else show(members, online);
      });
    }
    return () => {
      live = false;
    };
  }, []);

  const hasOnline = shown !== null && shown.online !== null;
  return (
    <p className="counts label" id="counts" hidden={!shown} aria-live="polite">
      <span id="count-online-wrap" hidden={!!shown && !hasOnline}>
        <span className="dot" aria-hidden="true"></span>
        <strong id="count-online">{hasOnline ? fmt(shown.online as number) : "0"}</strong> online
      </span>
      <span>
        <span className="sep" id="count-sep" aria-hidden="true" hidden={!!shown && !hasOnline}>
          ·
        </span>
        <strong id="count-members">{shown ? fmt(shown.members) : "0"}</strong> members
      </span>
    </p>
  );
}
