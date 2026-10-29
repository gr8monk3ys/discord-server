"use client";

import { useEffect } from "react";
import { arm, setArm } from "@/lib/live";

// Selectors the pen rules in as they arrive (field-notebook.css, "motion").
export const INKED =
  "main .ledger, .final-row, .ledger-body .rows, .ledger-body .days, .ledger-body > .rules, .faq, .games, .houses";

/** Scroll motion: the pen rules the page. Only what is still below the fold is armed, so
 *  nothing on screen ever blinks out, and each piece is inked once, when it arrives. Skipped
 *  when the reader asked for less motion or the browser has no IntersectionObserver; then
 *  the page is simply there. Returns a cleanup function. */
function ink(): () => void {
  const calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (calm || !("IntersectionObserver" in window)) return () => {};
  const timers = new Set<number>();
  const settle = (el: Element) => {
    el.classList.add("inked");
    // Back to the plain page once the ink is dry, so hover and theme changes are untouched.
    const t = window.setTimeout(() => {
      timers.delete(t);
      el.classList.remove("armed", "inked");
    }, 2400);
    timers.add(t);
  };
  const seen = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        seen.unobserve(entry.target);
        settle(entry.target);
      }
    },
    { rootMargin: "0px" },
  );
  document.documentElement.classList.add("inks");
  setArm((scope) => {
    const fold = window.innerHeight;
    scope.querySelectorAll(INKED).forEach((el) => {
      if (el.closest("[hidden]") || el.getBoundingClientRect().top < fold) return;
      el.classList.add("armed");
      seen.observe(el);
    });
  });
  // The live section is armed again when it opens, since it starts out hidden.
  arm(document);
  // Paper gets the whole page, whatever has been scrolled past or not.
  const print = () => {
    document.querySelectorAll(".armed").forEach((el) => {
      seen.unobserve(el);
      el.classList.remove("armed", "inked");
    });
  };
  window.addEventListener("beforeprint", print);
  return () => {
    window.removeEventListener("beforeprint", print);
    seen.disconnect();
    timers.forEach((t) => window.clearTimeout(t));
    setArm(() => {});
    document.documentElement.classList.remove("inks");
    document.querySelectorAll(".armed, .inked").forEach((el) => el.classList.remove("armed", "inked"));
  };
}

/** Where you are: the header link for the section crossing the reading line (a band just
 *  above the middle of the screen) carries the pen underline and aria-current="location".
 *  This is state, not decoration, so it runs with reduced motion too (without the slide). */
function spy(): () => void {
  if (!("IntersectionObserver" in window)) return () => {};
  const links = new Map<string, Element>();
  document.querySelectorAll(".sections a[href^='#']").forEach((a) => {
    links.set((a.getAttribute("href") || "").slice(1), a);
  });
  let current: string | null = null;
  const mark = (id: string | null) => {
    if (id === current) return;
    if (current) links.get(current)?.removeAttribute("aria-current");
    current = id && links.has(id) ? id : null;
    if (current) links.get(current)?.setAttribute("aria-current", "location");
  };
  const crossing = new Set<Element>();
  const line = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) crossing.add(entry.target);
        else crossing.delete(entry.target);
      }
      let last: Element | null = null;
      crossing.forEach((s) => {
        if (!last || last.compareDocumentPosition(s) & Node.DOCUMENT_POSITION_FOLLOWING) last = s;
      });
      mark(last ? (last as Element).id : null);
    },
    { rootMargin: "-38% 0px -58% 0px" },
  );
  document.querySelectorAll("main > section").forEach((s) => line.observe(s));
  return () => {
    line.disconnect();
    mark(null);
  };
}

/** The home page's motion and scroll-spy. Renders nothing; without JavaScript the page is
 *  simply there, fully drawn. */
export function PageMotion() {
  useEffect(() => {
    const stopInk = ink();
    const stopSpy = spy();
    return () => {
      stopInk();
      stopSpy();
    };
  }, []);
  return null;
}
