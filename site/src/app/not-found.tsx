import Link from "next/link";
import type { Metadata } from "next";
import { Footer } from "@/components/Footer";
import { Header, SkipLink } from "@/components/Header";

export const metadata: Metadata = {
  title: { absolute: "Page not found (404) · Chill Gamers" },
  description: "Error 404. The page was moved or the link has a typo.",
  robots: { index: false, follow: true },
};

export default function NotFound() {
  return (
    <>
      <SkipLink />
      <Header />

      <main id="main" className="lost">
        <div className="wrap">
          <div className="narrow">
            <h1>Nothing at this address</h1>
            <p className="lede">The page was moved or the link has a typo. The server page has everything.</p>
            <hr />
            <nav className="recover" aria-label="Find your way back">
              <Link className="label" href="/">
                ← Back to the server page
              </Link>
              <Link className="label" href="/#faq">
                Read the FAQ
              </Link>
              <a className="label" href="https://lscaturchio.xyz" rel="author">
                lscaturchio.xyz <span aria-hidden="true">↗</span>
              </a>
            </nav>
          </div>
        </div>
      </main>

      <Footer />
    </>
  );
}
