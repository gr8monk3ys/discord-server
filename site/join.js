// /join: the short link. Reads the invite from config.js (the only place it
// lives), points the fallback button at it and sends the visitor straight on.
// location.replace keeps /join out of the history, so Back leaves the site
// instead of bouncing forward again.
(function () {
  "use strict";

  var cfg = window.SITE_CONFIG || {};
  var code = String(cfg.INVITE_CODE || "").trim();
  var valid = /^[A-Za-z0-9-]{2,32}$/.test(code) && code !== "REPLACE_ME";
  var inviteUrl = valid ? `https://discord.gg/${code}` : null;

  var wire = function () {
    document.querySelectorAll("[data-join]").forEach(function (el) {
      if (inviteUrl) {
        el.setAttribute("href", inviteUrl);
        el.setAttribute("rel", "noopener");
      } else {
        el.textContent = "Back to the server page";
      }
    });
    if (!inviteUrl) {
      var note = document.getElementById("join-note");
      if (note) note.textContent = "The invite link isn't set up yet. Check back soon.";
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }

  if (inviteUrl) window.location.replace(inviteUrl);
})();
