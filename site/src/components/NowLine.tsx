"use client";

import { useEffect, useState } from "react";
import { loadStats, requestOpenLive } from "@/lib/live";
import { hasContent, tickerLine } from "@/lib/stats";

/** The masthead line: the next thing on, or the seasonal event that is running. It sits in
 *  the masthead's bottom margin, so showing it moves nothing. Hidden without JavaScript or
 *  without fresh data. */
export function NowLine() {
  const [line, setLine] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    loadStats().then((loaded) => {
      if (!live || !loaded || !hasContent(loaded.stats)) return;
      setLine(tickerLine(loaded.stats, loaded.now));
    });
    return () => {
      live = false;
    };
  }, []);
  return (
    <p className="now label" id="now" hidden={!line}>
      {/* The live section may still be waiting for a quiet moment to open: open it on demand. */}
      <a href="#live" onClick={requestOpenLive}>
        <span className="dot" aria-hidden="true"></span>
        <span id="now-text">{line}</span>
      </a>
    </p>
  );
}
