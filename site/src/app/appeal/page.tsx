import type { Metadata } from "next";
import { AppealForm } from "@/components/AppealForm";
import { Footer } from "@/components/Footer";
import { Header, SkipLink } from "@/components/Header";
import { CANONICAL } from "@/lib/site";

export const metadata: Metadata = {
  title: { absolute: "Appeal a ban · Chill Gamers" },
  description: "Banned from the Chill Gamers Discord server? Ask the staff team to take another look.",
  robots: { index: false, follow: false },
  alternates: { canonical: CANONICAL },
};

export default function AppealPage() {
  return (
    <>
      <SkipLink />
      <Header />

      <main id="main" className="appeal">
        <div className="wrap">
          <div className="narrow">
            <h1>Appeal a ban</h1>
            <p className="lede">
              If you were banned from the server, this form puts your side in front of the staff team. A person reads every
              appeal.
            </p>

            <section className="appeal-notes" aria-labelledby="before-title">
              <h2 className="card-title" id="before-title">Before you start</h2>
              <ul className="appeal-list">
                <li>
                  You need your <strong>Discord user ID</strong> and your <strong>appeal code</strong>. Front Desk, the
                  server&apos;s bot, sent both in a direct message when you were banned. Without a code this form can&apos;t
                  reach anyone.
                </li>
                <li>
                  That message also gives the date appeals open for you, usually a week after the ban. Appeals sent earlier
                  are discarded.
                </li>
                <li>One appeal at a time. If it is denied you can send another after the waiting period in that message.</li>
                <li>Plain words work best. Links aren&apos;t accepted.</li>
              </ul>
            </section>

            <AppealForm />

            <noscript>
              <p className="lede">This form needs JavaScript to send. Turn it on for this site and reload the page.</p>
            </noscript>

            <hr />

            <section className="appeal-notes" aria-labelledby="next-title">
              <h2 className="card-title" id="next-title">What happens next</h2>
              <ul className="appeal-list">
                <li>The staff team reads your answers and decides. That usually takes a few days and can take up to 30.</li>
                <li>
                  We often can&apos;t message you: Discord blocks the bot from messaging someone who is banned. If it can,
                  Front Desk tells you the outcome. If you hear nothing after a week or two, try the{" "}
                  <a className="ink" href="/join">invite link</a> again. If it lets you in, the ban was lifted.
                </li>
              </ul>
            </section>

            <section className="appeal-notes" aria-labelledby="stored-title">
              <h2 className="card-title" id="stored-title">What is stored</h2>
              <ul className="appeal-list">
                <li>
                  Your user ID, your three answers and the time you sent them go to a private staff channel on the Discord
                  server and are kept by the server&apos;s bot together with the decision, who made it and when.
                </li>
                <li>
                  The answers are erased from the bot&apos;s records 180 days after the decision. The record that an appeal
                  was made stays, so the waiting period can be applied.
                </li>
                <li>
                  This page keeps nothing: it sets no cookies, has no trackers, and the form is not saved here. Our host
                  records requests, including IP addresses, in its usual server logs.
                </li>
              </ul>
            </section>
          </div>
        </div>
      </main>

      <Footer />
    </>
  );
}
