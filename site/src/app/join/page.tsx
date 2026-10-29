import Link from "next/link";
import type { Metadata } from "next";
import { Footer } from "@/components/Footer";
import { Header, SkipLink } from "@/components/Header";
import { CANONICAL, INVITE_URL } from "@/lib/site";

// /join is the short link to the server. With a valid invite code, next.config.ts redirects
// it straight to Discord (307), so this page is only ever seen when the code isn't set up.
export const metadata: Metadata = {
  title: { absolute: "Join Chill Gamers" },
  description: "The short link to the Chill Gamers Discord server. You are being sent to the invite.",
  robots: { index: false, follow: true },
  alternates: { canonical: CANONICAL },
};

export default function JoinPage() {
  return (
    <>
      <SkipLink />
      <Header />

      <main id="main" className="lost">
        <div className="wrap">
          <div className="narrow">
            <h1>Opening Discord</h1>
            <p className="lede" id="join-note">
              {INVITE_URL
                ? "Discord should open in a moment. If nothing happens, use the button."
                : "The invite link isn't set up yet. Check back soon."}
            </p>
            <p className="lede">
              {INVITE_URL ? (
                <a className="btn btn-lg" data-join="" href={INVITE_URL} rel="noopener">
                  Join the server
                </a>
              ) : (
                <Link className="btn btn-lg" data-join="" href="/">
                  Back to the server page
                </Link>
              )}
            </p>
            <hr />
            <nav className="recover" aria-label="Find your way">
              <Link className="label" href="/">
                ← See the server page first
              </Link>
              <Link className="label" href="/#faq">
                Read the FAQ
              </Link>
            </nav>
          </div>
        </div>
      </main>

      <Footer />
    </>
  );
}
