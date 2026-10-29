import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import type { ReactNode } from "react";
import { ThemeProvider } from "@/components/ThemeProvider";
import { SITE_ORIGIN } from "@/lib/site";
import { fontVariables } from "./fonts";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_ORIGIN),
  title: "Chill Gamers",
  referrer: "strict-origin-when-cross-origin",
  icons: { icon: [{ url: "/img/favicon.png", type: "image/png" }] },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#184e35" },
    { media: "(prefers-color-scheme: dark)", color: "#111317" },
  ],
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Set by src/proxy.ts for this request; the theme script must carry it to run.
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    // next-themes sets data-theme on <html> before React hydrates
    <html lang="en" className={fontVariables} suppressHydrationWarning>
      <body>
        <ThemeProvider nonce={nonce}>{children}</ThemeProvider>
      </body>
    </html>
  );
}
