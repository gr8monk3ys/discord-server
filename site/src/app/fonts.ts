// The three faces, self-hosted by next/font at build time: the browser never talks to
// Google, and the CSP's font-src can stay 'self'. The CSS variables feed --display, --body
// and --mono in field-notebook.css.
import { Fraunces, IBM_Plex_Mono, Instrument_Sans } from "next/font/google";

export const fraunces = Fraunces({ subsets: ["latin"], display: "swap", variable: "--font-fraunces" });
export const instrumentSans = Instrument_Sans({ subsets: ["latin"], display: "swap", variable: "--font-instrument-sans" });
export const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
  variable: "--font-plex-mono",
});

export const fontVariables = `${fraunces.variable} ${instrumentSans.variable} ${plexMono.variable}`;
