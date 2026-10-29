import Link from "next/link";
import { JoinLink } from "@/components/JoinLink";
import { ThemeToggle } from "@/components/ThemeToggle";

export const SECTIONS: [string, string][] = [
  ["games", "Games"],
  ["features", "First week"],
  ["week", "The week"],
  ["smp", "Monkey SMP"],
  ["rules", "Rules"],
  ["faq", "FAQ"],
];

export function SkipLink() {
  return <a className="skip" href="#main">Skip to content</a>;
}

/** The sheet at the top of every page. The home page gets the section links and a Join
 *  link; every other page gets a quiet way back to it. */
export function Header({ home = false }: { home?: boolean }) {
  return (
    <header className="top">
      <div className="wrap top-row">
        <div className="brand">
          <a className="brand-home" href="https://lscaturchio.xyz" rel="author">
            {/* A plain <img>: a fixed SVG wordmark, nothing for next/image to optimise. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img className="wordmark" src="/img/cursive.svg" alt="Lorenzo Scaturchio" width="818" height="198" />
            <span className="brand-name">Lorenzo Scaturchio</span>
          </a>
        </div>
        {home && (
          <nav className="sections" aria-label="Primary">
            <ul>
              {SECTIONS.map(([id, label]) => (
                <li key={id}>
                  <a href={`#${id}`}>{label}</a>
                </li>
              ))}
            </ul>
          </nav>
        )}
        <div className="controls">
          <ThemeToggle />
          {home ? (
            <JoinLink className="btn-quiet">Join</JoinLink>
          ) : (
            <Link className="btn-quiet" href="/">
              Server page
            </Link>
          )}
        </div>
      </div>
    </header>
  );
}
