import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Counts } from "@/components/Counts";
import { Footer, Out, PRIVACY_URL, SMP_URL } from "@/components/Footer";
import { Header, SkipLink } from "@/components/Header";
import { JoinLink } from "@/components/JoinLink";
import { LiveSection } from "@/components/LiveSection";
import { NowLine } from "@/components/NowLine";
import { PageMotion } from "@/components/PageMotion";
import { GameList, RuleList } from "@/components/Rules";
import { SeasonLine } from "@/components/SeasonLine";
import { DESCRIPTION, JSON_LD, OG_ALT, SEASON_START, SEASON_TEXT, SHARE, TITLE } from "@/lib/home";
import { CANONICAL, SITE_ORIGIN } from "@/lib/site";

export const metadata: Metadata = {
  title: { absolute: TITLE },
  description: DESCRIPTION,
  authors: [{ name: "Lorenzo Scaturchio" }],
  alternates: { canonical: CANONICAL },
  openGraph: {
    type: "website",
    siteName: "Chill Gamers",
    url: CANONICAL,
    title: TITLE,
    description: SHARE,
    images: [{ url: `${SITE_ORIGIN}/img/og.png`, type: "image/png", width: 1200, height: 630, alt: OG_ALT }],
    locale: "en_US",
  },
  twitter: {
    card: "summary_large_image",
    title: TITLE,
    description: SHARE,
    images: [{ url: `${SITE_ORIGIN}/img/og.png`, alt: OG_ALT }],
  },
};

function Ledger({ head, children }: { head: ReactNode; children: ReactNode }) {
  return (
    <div className="wrap">
      <div className="ledger">
        <div className="ledger-head">{head}</div>
        <div className="ledger-body">{children}</div>
      </div>
    </div>
  );
}

const FAQ: [string, ReactNode][] = [
  [
    "Do I have to be good at games?",
    "You don't. Most of what we play is co-op against the game, so a bad run is everyone's bad run. Nobody tracks your K/D.",
  ],
  [
    "How busy is it?",
    "It's a small server. Most of us are in US Pacific and play in the evening: some nights a few people are in voice, some nights nobody is. When the bot has fresh numbers, this page shows them under This week.",
  ],
  [
    "What happens when I join?",
    "Discord asks you to apply, and a mod approves it. Then you pick your games and land in the main channels. Say hi in general, and look at who is in voice.",
  ],
  [
    "Is voice chat required?",
    "It isn't. Most of the talking happens in voice, so the text channels are on the quiet side. You're welcome to join a room muted and just listen.",
  ],
  [
    "How do I get on the Minecraft server?",
    <>
      Monkey SMP takes a pass. Season dates and how to get one are on{" "}
      <a className="ink" href={SMP_URL} rel="noopener">its page</a>. In the Discord it all sits in the Monkey SMP category: news goes in
      #monkey-smp, the commands are in #smp-commands, and the SMP Ping role tells you when three or more people are on.
    </>,
  ],
  [
    "Why does Discord call it Lorenzo's Server?",
    "That's the server's name on Discord. Chill Gamers is what we call it here. It's the same place.",
  ],
  [
    "What does the bot keep about me?",
    <>
      Counts, not content. Front Desk notes how many messages you send each day, never what they say, plus your time in
      voice and the game Discord shows you playing. That drives levels, coins and house points.{" "}
      <code>/privacy tracking:off</code> stops it and deletes that history. The full list is in the{" "}
      <a className="ink" href={PRIVACY_URL} rel="noopener">privacy policy</a>.
    </>,
  ],
  [
    "My game isn't listed. Can I still join?",
    "Yes. Ask in general who else plays it. If a few people do, it can get a channel and a role.",
  ],
];

export default function Home() {
  return (
    <>
      <script
        type="application/ld+json"
        // The only raw HTML on the site: constant data, with "<" escaped so it can't close the tag.
        dangerouslySetInnerHTML={{ __html: JSON.stringify(JSON_LD).replace(/</g, "\\u003c") }}
      />
      <SkipLink />
      <Header home />

      <main id="main">
        <section className="hero" id="top">
          <div className="hero-grid">
            <div className="hero-copy">
              <h1>
                Chill gamers. <em>No tryhards, no toxicity.</em>
              </h1>
              <p className="lede">
                A small Discord server for co-op and PvE games: Risk of Rain 2, Gears of War, Overwatch, Marvel Rivals and a
                few more, plus Monkey SMP, the owner&apos;s modded Minecraft server.
              </p>
              <p className="lede">
                People get on in the evenings, US Pacific time. Some nights a few of us are in voice and some nights it&apos;s
                quiet. Nobody minds if you&apos;re new to a game or bad at it. Join (a mod approves new members), pick your
                games, and hop into the Lobby when you see someone there.
              </p>
              <div className="cta">
                <JoinLink className="btn btn-lg" id="join">
                  <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M20.3 4.4A19.6 19.6 0 0 0 15.4 3l-.6 1.3a18 18 0 0 0-5.5 0L8.6 3a19.6 19.6 0 0 0-4.9 1.5C.6 9.1-.3 13.6.1 18.1a19.8 19.8 0 0 0 6 3l1.3-2a12.7 12.7 0 0 1-2-1l.5-.4a14 14 0 0 0 12.1 0l.5.4-2 1 1.3 2a19.7 19.7 0 0 0 6-3c.5-5.2-.9-9.7-3.5-13.7ZM8 15.3c-1.2 0-2.2-1.1-2.2-2.4S6.8 10.5 8 10.5s2.2 1.1 2.2 2.4-1 2.4-2.2 2.4Zm8 0c-1.2 0-2.2-1.1-2.2-2.4s1-2.4 2.2-2.4 2.2 1.1 2.2 2.4-1 2.4-2.2 2.4Z" /></svg>
                  Join the server
                </JoinLink>
                <Counts />
              </div>
            </div>
            <figure className="plate" aria-hidden="true">
              <div className="plate-frame">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img className="icon" src="/img/server-icon.png" alt="" width="256" height="256" />
              </div>
            </figure>
            <NowLine />
          </div>
        </section>

        <section id="games">
          <Ledger
            head={
              <>
                <h2>What we play</h2>
                <p className="blurb">
                  Each of these has a channel and a role. Pick yours when you join and you only get pings for those. Playing
                  something else? Ask in general.
                </p>
              </>
            }
          >
            <GameList />
          </Ledger>
        </section>

        <section id="features">
          <Ledger
            head={
              <>
                <h2>Your first week</h2>
                <p className="blurb">Four things a new member ends up doing. None of them is required.</p>
              </>
            }
          >
            <div className="group">
              <ul className="rows">
                <li>
                  <h3 className="card-title">Hop in voice</h3>
                  <p>
                    The Lobby is the open voice room. If someone is in it, join them: that is how most games here start. For a
                    room of your own, join New Squad and Front Desk makes one named after what you&apos;re playing.
                  </p>
                </li>
                <li>
                  <h3 className="card-title">Find people for a game</h3>
                  <p>
                    Post with <code>/lfg</code>: what you&apos;re playing, when, and how many you need. Members who picked that
                    game get the ping. On a server this size, asking in general works too.
                  </p>
                </li>
                <li>
                  <h3 className="card-title">Get sorted into a house</h3>
                  <p>
                    Front Desk, the server&apos;s bot, puts you in Fern, Kestrel or Otter. You don&apos;t choose. Voice time,
                    chatting, squads and the daily word all score for your house, and Monday&apos;s post says which one won the
                    week.
                  </p>
                </li>
                <li>
                  <h3 className="card-title">Play on Monkey SMP</h3>
                  <p>
                    Monkey SMP is the owner&apos;s modded Minecraft server. It takes its own pass.{" "}
                    <a className="ink" href="#smp">How to get in</a>
                  </p>
                </li>
              </ul>
            </div>
            <p className="aside">
              Smaller things, if you want them: levels for chat and voice time (<code>/rank</code>), coins and a{" "}
              <code>/shop</code> for name colours, a daily word puzzle (<code>/word guess</code>), and a monthly bracket once
              four people sign up.
            </p>
          </Ledger>
        </section>

        <section id="week">
          <Ledger
            head={
              <>
                <h2>A normal week</h2>
                <p className="blurb">
                  Times are US Pacific. Evenings are when people play. The other rows are what Front Desk, the bot, posts each
                  week.
                </p>
              </>
            }
          >
            <ol className="days">
              <li>
                <span className="label">Evenings</span>
                <div>
                  <h3 className="card-title">Voice</h3>
                  <p>
                    Whoever&apos;s around joins the Lobby and picks a game. Nothing is scheduled, so some nights are busier than
                    others.
                  </p>
                </div>
              </li>
              <li>
                <span className="label">Mon</span>
                <div>
                  <h3 className="card-title">House result</h3>
                  <p>Front Desk adds up last week&apos;s house points and posts the winner.</p>
                </div>
              </li>
              <li>
                <span className="label">Thu</span>
                <div>
                  <h3 className="card-title">Free games</h3>
                  <p>A list of PC games that are free to keep that week, when there are new ones.</p>
                </div>
              </li>
              <li>
                <span className="label">Fri</span>
                <div>
                  <h3 className="card-title">Game night</h3>
                  <p>
                    At noon Front Desk puts a 9 PM game night on the calendar for whatever was played most that week, unless a
                    member has already set one up with <code>/gamenight</code>. Come if you&apos;re free.
                  </p>
                </div>
              </li>
            </ol>
          </Ledger>
        </section>

        <section id="smp">
          <Ledger
            head={
              <>
                <h2>Monkey SMP</h2>
                <p className="blurb">
                  The owner&apos;s modded Minecraft server, run alongside the Discord. The Discord is free; the server takes a
                  pass.
                </p>
                <SeasonLine start={SEASON_START}>{SEASON_TEXT}</SeasonLine>
              </>
            }
          >
            <p className="smp-note">
              Java Edition, with factions of up to four, land you claim that nobody may grief, PvP everywhere else and voice
              chat by distance. Prices, rules and how to get in are on its own site.
            </p>
            <ul className="links">
              <li>
                <a className="ink" href={SMP_URL} rel="noopener">
                  monkeysmp.lscaturchio.xyz <Out />
                </a>
              </li>
            </ul>
          </Ledger>
        </section>

        <LiveSection />

        <section id="rules">
          <Ledger
            head={
              <>
                <h2>House rules</h2>
                <p className="blurb">
                  The same rules are pinned in #rules on Discord. Monkey SMP has its own, on its site.
                </p>
              </>
            }
          >
            <RuleList />
          </Ledger>
        </section>

        <section id="faq">
          <Ledger head={<h2>Questions</h2>}>
            <div className="faq">
              {FAQ.map(([question, answer]) => (
                <details key={question}>
                  <summary>{question}</summary>
                  <p>{answer}</p>
                </details>
              ))}
            </div>
          </Ledger>
        </section>

        <section className="final">
          <div className="wrap">
            <div className="final-row">
              <h2>Come play a round</h2>
              <JoinLink className="btn btn-lg">Join the server</JoinLink>
            </div>
          </div>
        </section>
      </main>

      <Footer full />
      <PageMotion />
    </>
  );
}
