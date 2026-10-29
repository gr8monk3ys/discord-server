import Link from "next/link";
import type { Metadata } from "next";
import { Footer } from "@/components/Footer";
import { Header, SkipLink } from "@/components/Header";
import { RESULTS, resultFor } from "@/lib/linked-role-results";
import { CANONICAL } from "@/lib/site";

export const metadata: Metadata = {
  title: { absolute: "Linked roles · Chill Gamers" },
  description: "Where the Chill Gamers linked-role check ends.",
  robots: { index: false, follow: false },
  referrer: "no-referrer",
  alternates: { canonical: CANONICAL },
};

/** Where the linked-role check ends. The only input is the ?r= flag, and it is only ever used
 *  as a key into a fixed list (unknown flags show the default text), so nothing from the
 *  address bar is written into the page. Rendered on the server: works without JavaScript. */
export default async function LinkedRolePage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const [title, text] = resultFor((await searchParams).r) ?? RESULTS.default;
  return (
    <>
      <SkipLink />
      <Header />

      <main id="main" className="lost">
        <div className="wrap">
          <div className="narrow">
            <h1 id="lr-title">{title}</h1>
            <p className="lede" id="lr-text">{text}</p>
            <p className="lede">
              Only yes or no per platform is saved, and Discord is the one saving it. Your account names are not kept, and
              nothing is stored on this site.
            </p>
            <hr />
            <nav className="recover" aria-label="Where next">
              <Link className="label" href="/">
                ← Back to the server page
              </Link>
              <Link className="label" href="/#faq">
                Read the FAQ
              </Link>
            </nav>
          </div>
        </div>
      </main>

      <Footer fine="Signing in sets one cookie, gone in 10 minutes. Nothing tracks you." />
    </>
  );
}
