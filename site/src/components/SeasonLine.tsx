"use client";

import { useSyncExternalStore } from "react";
import { seasonLine } from "@/lib/stats";

const noop = () => () => {};

/** The Monkey SMP season line. Server-rendered as the date, which is true without
 *  JavaScript; in the browser it counts down to the opening, then says it is open. The
 *  server snapshot is null, so hydration matches the server HTML and the count follows. */
export function SeasonLine({ start, children }: { start: string; children: string }) {
  const line = useSyncExternalStore(
    noop,
    () => seasonLine(start, children, Date.now()),
    () => null,
  );
  return (
    <p className="label season" id="season" data-start={start}>
      {line ?? children}
    </p>
  );
}
