// /linked-role: shows how the linked-role check ended. The only input is the ?r= flag, and it
// is only ever used as a key into the fixed list below (unknown flags leave the page as it is),
// so nothing from the address bar is written into the page.
(function () {
  "use strict";

  var RESULTS = {
    ok: [
      "You're linked.",
      "Discord now knows which game platforms are verified on your account. Go back to Discord: a role you qualify for is yours. If one stays locked, add that platform under User Settings, Connections, then press Link again.",
    ],
    cancelled: ["Cancelled.", "You said no on Discord's screen, so nothing was read and nothing changed."],
    expired: [
      "That took too long.",
      "The sign-in window lasts 10 minutes and works once. Start again from the Linked Roles menu in Discord.",
    ],
    busy: ["Discord is busy.", "Discord asked us to slow down. Give it a minute, then press Link again."],
    failed: [
      "Something went wrong.",
      "Nothing was saved. Start again from the Linked Roles menu in Discord; if it keeps failing, tell a mod.",
    ],
  };

  var flag = null;
  try {
    flag = new URLSearchParams(window.location.search).get("r");
  } catch (e) {
    return;
  }
  if (typeof flag !== "string" || !Object.prototype.hasOwnProperty.call(RESULTS, flag)) return;

  var show = function () {
    var title = document.getElementById("lr-title");
    var text = document.getElementById("lr-text");
    if (!title || !text) return;
    title.textContent = RESULTS[flag][0];
    text.textContent = RESULTS[flag][1];
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", show);
  } else {
    show();
  }
})();
