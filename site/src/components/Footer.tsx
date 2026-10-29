import Link from "next/link";
import { SECTIONS } from "@/components/Header";

export const PRIVACY_URL = "https://github.com/gr8monk3ys/discord-server/blob/front-desk-bot/PRIVACY.md";
export const SMP_URL = "https://monkeysmp.lscaturchio.xyz";

/** The arrow after an outbound link (U+FE0E keeps it text, never an emoji). */
export function Out() {
  return <span aria-hidden="true">{"↗︎"}</span>;
}

/** The full footer is the home page's; the other pages carry only the legal lines. */
export function Footer({ full = false, fine = "This page doesn't track you." }: { full?: boolean; fine?: string }) {
  return (
    <footer>
      <div className="wrap foot">
        {full && (
          <div className="foot-grid">
            <div className="foot-about">
              <h2 className="card-title">Chill Gamers</h2>
              <p>
                A small co-op and PvE Discord server run by Lorenzo Scaturchio. Front Desk, the server&apos;s own bot, does
                the housekeeping, and its code is public.
              </p>
            </div>
            <div className="foot-cols">
              <nav aria-label="On this page">
                <h3 className="label">On this page</h3>
                <ul>
                  {SECTIONS.map(([id, label]) => (
                    <li key={id}>
                      <a className="ink" href={`#${id}`}>{label}</a>
                    </li>
                  ))}
                </ul>
              </nav>
              <nav aria-label="Elsewhere">
                <h3 className="label">Elsewhere</h3>
                <ul>
                  <li><a className="ink" href="https://lscaturchio.xyz" rel="author">lscaturchio.xyz <Out /></a></li>
                  <li><a className="ink" href="https://lscaturchio.xyz/blog" rel="author">Writing <Out /></a></li>
                  <li><a className="ink" href="https://lscaturchio.xyz/projects" rel="author">Projects <Out /></a></li>
                </ul>
              </nav>
              <nav aria-label="Help">
                <h3 className="label">Help</h3>
                <ul>
                  <li><Link className="ink" href="/appeal">Appeal a ban</Link></li>
                  <li><a className="ink" href={PRIVACY_URL} rel="noopener">Privacy <Out /></a></li>
                  <li><a className="ink" href={SMP_URL} rel="noopener">Monkey SMP <Out /></a></li>
                </ul>
              </nav>
              <nav aria-label="Source">
                <h3 className="label">Source</h3>
                <ul>
                  <li><a className="ink" href="https://github.com/gr8monk3ys/discord-server" rel="noopener">GitHub <Out /></a></li>
                </ul>
              </nav>
            </div>
          </div>
        )}
        <div className="foot-legal">
          {full ? (
            <p className="fine">
              © <span id="year">{new Date().getFullYear()}</span> Chill Gamers. Not affiliated with Discord.
            </p>
          ) : (
            <p className="fine">Chill Gamers. Not affiliated with Discord.</p>
          )}
          <p className="fine">{fine}</p>
        </div>
      </div>
    </footer>
  );
}
