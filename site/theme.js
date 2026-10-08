// Theme: paper (light) or night (dark). Loaded without defer in <head> so a
// remembered choice applies before first paint. With no stored choice the page
// follows prefers-color-scheme through CSS alone; clicking the toggle stores an
// explicit "light" or "dark". Storage can throw (private mode, blocked site
// data), so every access is guarded and the page works without it.
(function () {
  "use strict";

  var KEY = "theme";
  var root = document.documentElement;

  function stored() {
    try {
      var v = window.localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : null;
    } catch (e) {
      return null;
    }
  }

  function systemDark() {
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  function current() {
    var t = root.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return systemDark() ? "dark" : "light";
  }

  var initial = stored();
  if (initial) root.setAttribute("data-theme", initial);

  function sync(btn) {
    var dark = current() === "dark";
    btn.setAttribute("aria-pressed", dark ? "true" : "false");
    btn.setAttribute("title", dark ? "Switch to paper (light)" : "Switch to night (dark)");
  }

  function wire() {
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.hidden = false;
      sync(btn);
      btn.addEventListener("click", function () {
        var next = current() === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        try { window.localStorage.setItem(KEY, next); } catch (e) { /* not remembered */ }
        Array.prototype.forEach.call(buttons, sync);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
