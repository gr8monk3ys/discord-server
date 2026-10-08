// Landing page behaviour: wire every Join link to the invite in config.js and
// show live member/online counts from Discord's public invite endpoint (it
// sends CORS headers, so the browser can read it). Anything that fails just
// leaves the counts hidden; the page never depends on this script.
(function () {
  "use strict";

  var cfg = window.SITE_CONFIG || {};
  var code = String(cfg.INVITE_CODE || "").trim();
  var valid = /^[A-Za-z0-9-]{2,32}$/.test(code) && code !== "REPLACE_ME";
  var inviteUrl = valid ? `https://discord.gg/${code}` : null;

  document.querySelectorAll("[data-join]").forEach(function (el) {
    if (inviteUrl) {
      el.setAttribute("href", inviteUrl);
      el.setAttribute("rel", "noopener");
      el.removeAttribute("aria-disabled");
      el.classList.remove("is-disabled");
    } else {
      el.removeAttribute("href");
      el.setAttribute("aria-disabled", "true");
      el.classList.add("is-disabled");
      el.title = "Invite link not set yet";
    }
  });

  var year = document.getElementById("year");
  if (year) year.textContent = String(new Date().getFullYear());

  if (!inviteUrl || !window.fetch) return;

  var fmt = function (n) {
    try { return new Intl.NumberFormat().format(n); } catch (e) { return String(n); }
  };

  var ctrl = window.AbortController ? new AbortController() : null;
  var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 6000) : null;

  fetch(
    "https://discord.com/api/v10/invites/" + encodeURIComponent(code) + "?with_counts=true",
    { credentials: "omit", signal: ctrl ? ctrl.signal : undefined }
  )
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) {
      if (timer) clearTimeout(timer);
      if (!data) return;
      var members = data.approximate_member_count;
      var online = data.approximate_presence_count;
      if (typeof members !== "number" || typeof online !== "number") return;
      document.getElementById("count-members").textContent = fmt(members);
      document.getElementById("count-online").textContent = fmt(online);
      var box = document.getElementById("counts");
      box.hidden = false;
    })
    .catch(function () { /* offline, blocked or expired invite: keep counts hidden */ });
})();
