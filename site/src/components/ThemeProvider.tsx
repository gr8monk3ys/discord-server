"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ReactNode } from "react";

// Paper (light) or night (dark). With no stored choice the page follows
// prefers-color-scheme; the toggle stores an explicit "light" or "dark" under the same
// "theme" key the old theme.js used, so a choice made before the port still applies.
// next-themes sets data-theme before first paint with a small inline script that carries
// the request's CSP nonce. field-notebook.css sets color-scheme itself.
export function ThemeProvider({ nonce, children }: { nonce?: string; children: ReactNode }) {
  return (
    <NextThemesProvider
      attribute="data-theme"
      defaultTheme="system"
      enableSystem
      enableColorScheme={false}
      storageKey="theme"
      themes={["light", "dark"]}
      nonce={nonce}
    >
      {children}
    </NextThemesProvider>
  );
}
