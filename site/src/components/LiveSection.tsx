"use client";

import { useEffect, useRef, useState } from "react";
import { arm, loadStats, onOpenLive, type Loaded } from "@/lib/live";
import { dayRows, fmt, hasContent, houseRows, updatedLine } from "@/lib/stats";

function jump(px: number) {
  try {
    window.scrollBy({ top: px, left: 0, behavior: "instant" });
  } catch {
    window.scrollBy(0, px);
  }
}

/** Show the section without moving anything the visitor is looking at. It opens just above
 *  the next section, so: below the fold it simply appears; above the viewport (or tucked under
 *  the sticky header, where a #games link lands) it appears and the scroll position is
 *  corrected by its height; and while that spot is on screen it waits until the visitor has
 *  scrolled past it. Returns a cleanup function. */
function reveal(section: HTMLElement, force: boolean): () => void {
  const next = section.nextElementSibling;
  const header = document.querySelector(".top");
  const attempt = (forced: boolean) => {
    if (!section.hidden) return true;
    const viewport = window.innerHeight || document.documentElement.clientHeight;
    const top = next ? next.getBoundingClientRect().top : viewport;
    if (forced || top >= viewport) {
      section.hidden = false;
      arm(section);
      return true;
    }
    const edge = header ? Math.max(header.getBoundingClientRect().bottom, 0) : 0;
    if (next && top <= edge + 1) {
      section.hidden = false;
      const moved = next.getBoundingClientRect().top - top; // 0 where the browser anchors scroll itself
      if (moved) jump(moved);
      return true;
    }
    return false;
  };
  if (attempt(force)) return () => {};
  const later = () => {
    if (!attempt(false)) return;
    stop();
  };
  const stop = () => {
    window.removeEventListener("scroll", later);
    window.removeEventListener("resize", later);
  };
  window.addEventListener("scroll", later, { passive: true });
  window.addEventListener("resize", later);
  return stop;
}

/** "This week": the aggregates the bot publishes (no names, no IDs). Server-rendered hidden
 *  and empty; it opens only when fresh data with something in it arrives. If the fetch fails
 *  or the data is stale it stays hidden and the page is unchanged.
 *
 *  The section's `hidden` attribute is a constant in the markup and is lifted here on the DOM
 *  node itself, never through React state: the reveal has to measure the page in the same
 *  moment it opens the section, and React never puts a constant prop back. */
export function LiveSection() {
  const ref = useRef<HTMLElement>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  useEffect(() => {
    let live = true;
    loadStats().then((data) => {
      if (live && data && hasContent(data.stats)) setLoaded(data);
    });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    const section = ref.current;
    if (!section || !loaded) return;
    const asked = window.location.hash === "#live";
    const stop = reveal(section, asked);
    if (asked) section.scrollIntoView();
    const off = onOpenLive(() => {
      section.hidden = false;
    });
    return () => {
      stop();
      off();
    };
  }, [loaded]);

  const stats = loaded?.stats;
  const rows = loaded ? dayRows(loaded.stats, loaded.now) : [];
  const houses = stats ? houseRows(stats.houses) : [];
  const games = stats ? stats.games : [];
  const tally = stats ? stats.tally : [];

  return (
    <section id="live" hidden ref={ref}>
      <div className="wrap">
        <div className="ledger">
          <div className="ledger-head">
            <h2>This week</h2>
            <p className="blurb" id="live-updated">{stats ? updatedLine(stats) : null}</p>
            <p className="counts label tally" id="live-tally" hidden={!tally.length}>
              {tally.map(([n, label]) => (
                <span key={label}>
                  <strong>{fmt(n)}</strong>
                  {" " + label}
                </span>
              ))}
            </p>
          </div>
          <div className="ledger-body live-body">
            <div className="group" id="live-events" hidden={!rows.length}>
              <div className="group-head">
                <h3>Coming up</h3>
                <span className="label" id="live-events-count">{(rows.length < 10 ? "0" : "") + rows.length}</span>
              </div>
              <ol className="days" id="live-events-list">
                {rows.map((row, i) => (
                  <li key={i}>
                    <span className="label">{row.iso ? <time dateTime={row.iso}>{row.label}</time> : row.label}</span>
                    <div>
                      <h4 className="card-title">{row.title}</h4>
                      <p>{row.detail}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
            <div className="group" id="live-houses" hidden={!houses.length}>
              <div className="group-head">
                <h3>House standings</h3>
                <span className="label">Points per member</span>
              </div>
              <ol className="houses" id="live-houses-list">
                {houses.map((h) => (
                  <li key={h.name} className={h.lead ? "lead" : undefined}>
                    <span className="card-title">{h.name}</span>
                    <span className="label">{fmt(h.points)}</span>
                    <span className="bar" aria-hidden="true">
                      {/* Only ever rendered in the browser, so React sets the width through the
                          CSSOM, which the CSP's style-src allows; no style attribute is sent. */}
                      <span className="bar-fill" style={{ width: `${h.width}%` }}></span>
                    </span>
                  </li>
                ))}
              </ol>
            </div>
            <div className="group" id="live-games" hidden={!games.length}>
              <div className="group-head">
                <h3>Most played this week</h3>
                <span className="label">Hours</span>
              </div>
              <ul className="games" id="live-games-list">
                {games.map((g) => (
                  <li key={g.name} className="game">
                    <span>{g.name}</span>
                    <span className="game-hours">{fmt(g.hours) + " h"}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
