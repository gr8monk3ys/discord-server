import type { ReactNode } from "react";
import { INVITE_URL } from "@/lib/site";

/** A link to the Discord invite, rendered on the server so it works without JavaScript.
 *  Without a usable invite code it is drawn disabled instead of pointing nowhere. */
export function JoinLink({ className, id, children }: { className: string; id?: string; children: ReactNode }) {
  if (!INVITE_URL) {
    return (
      <a className={`${className} is-disabled`} id={id} data-join="" aria-disabled="true" title="Invite link not set yet">
        {children}
      </a>
    );
  }
  return (
    <a className={className} id={id} data-join="" href={INVITE_URL} rel="noopener">
      {children}
    </a>
  );
}
