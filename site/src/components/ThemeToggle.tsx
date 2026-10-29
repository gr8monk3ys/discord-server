"use client";

import { useTheme } from "next-themes";
import { useSyncExternalStore } from "react";

const noop = () => () => {};

// Hidden until it can work (no JavaScript, no button), then a two-state switch. The sun and
// moon swap in CSS from data-theme, so the icon is right even before this has run.
export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  // false while server rendering and hydrating, true once running in the browser
  const mounted = useSyncExternalStore(noop, () => true, () => false);
  const dark = mounted && resolvedTheme === "dark";
  return (
    <button
      className="theme-toggle"
      type="button"
      data-theme-toggle=""
      aria-label="Toggle theme"
      hidden={!mounted}
      aria-pressed={mounted ? dark : undefined}
      title={mounted ? (dark ? "Switch to paper (light)" : "Switch to night (dark)") : undefined}
      onClick={() => setTheme(dark ? "light" : "dark")}
    >
      <svg className="sun" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4" /><path d="M12 2v2" /><path d="M12 20v2" /><path d="m4.93 4.93 1.41 1.41" /><path d="m17.66 17.66 1.41 1.41" /><path d="M2 12h2" /><path d="M20 12h2" /><path d="m6.34 17.66-1.41 1.41" /><path d="m19.07 4.93-1.41 1.41" /></svg>
      <svg className="moon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401" /></svg>
    </button>
  );
}
