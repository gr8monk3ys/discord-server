// Landing page behaviour: wire every Join link to the invite in config.js, show
// live member/online counts from Discord's public invite endpoint (it sends CORS
// headers, so the browser can read it), rule the page in as it scrolls, and fill
// the "Live" section from the
// aggregates the bot publishes (config.js STATS_URL). Anything that fails just
// leaves its part hidden; the page never depends on this script.
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

  // Scroll motion: the pen rules the page (style.css, "motion"). Only what is still
  // below the fold is armed, so nothing on screen ever blinks out, and each piece is
  // inked once, when it arrives. Skipped when the reader asked for less motion or the
  // browser has no IntersectionObserver; then the page is simply there.
  var ink = function () {
    var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (calm || !("IntersectionObserver" in window)) return;
    var settle = function (el) {
      el.classList.add("inked");
      // Back to the plain page once the ink is dry, so hover and theme changes are untouched.
      window.setTimeout(function () { el.classList.remove("armed", "inked"); }, 2400);
    };
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        seen.unobserve(entry.target);
        settle(entry.target);
      });
    }, { rootMargin: "0px 0px -12% 0px" });
    var fold = window.innerHeight;
    document.documentElement.classList.add("inks");
    document.querySelectorAll(
      "main .ledger, .final-row, .ledger-body > .days, .ledger-body > .rules, .faq, #games .games"
    ).forEach(function (el) {
      if (el.closest("[hidden]") || el.getBoundingClientRect().top < fold) return;
      el.classList.add("armed");
      seen.observe(el);
    });
    // Paper gets the whole page, whatever has been scrolled past or not.
    window.addEventListener("beforeprint", function () {
      document.querySelectorAll(".armed").forEach(function (el) {
        seen.unobserve(el);
        el.classList.remove("armed", "inked");
      });
    });
  };
  ink();

  if (!window.fetch) return;

  var TIMEOUT_MS = 6000;
  var HOUR_MS = 3600 * 1000;
  var STALE_MS = 48 * HOUR_MS; // older than this: the live section stays hidden
  var ONLINE_FRESH_MS = 2 * HOUR_MS; // an online count older than this is not worth showing
  var STARTED_GRACE_MS = 3 * HOUR_MS; // an event that started this recently still shows, as "Now"
  var MAX_COUNT = 10000000;

  var byId = function (id) { return document.getElementById(id); };

  var fmt = function (n) {
    try { return new Intl.NumberFormat().format(n); } catch (e) { return String(n); }
  };

  // JSON from `url`, or null on any failure (offline, blocked, timeout, not JSON).
  var getJson = function (url) {
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, TIMEOUT_MS) : null;
    return fetch(url, { credentials: "omit", signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (data) {
        if (timer) clearTimeout(timer);
        return data;
      });
  };

  // ------------------------------------------------------------ live data
  // The published file is checked again here, field by field: only whole
  // numbers in range and short plain strings get through, and a string that
  // looks like an ID or a mention is dropped even though the bot and the
  // publisher already refuse those. Everything is rendered with textContent.
  var count = function (x) {
    return typeof x === "number" && isFinite(x) && Math.floor(x) === x && x >= 0 && x <= MAX_COUNT ? x : null;
  };

  var text = function (x, max) {
    if (typeof x !== "string") return null;
    var s = x.trim();
    if (!s || s.length > max || /\d{15,}/.test(s) || /[@<>]/.test(s)) return null;
    return s;
  };

  var when = function (x) {
    if (typeof x !== "string" || x.length > 40) return null;
    var t = Date.parse(x);
    return isFinite(t) ? t : null;
  };

  var list = function (x, max) { return Array.isArray(x) ? x.slice(0, max) : []; };
  var obj = function (x) { return x && typeof x === "object" && !Array.isArray(x) ? x : {}; };

  var KINDS = { gamenight: "Game night", tournament: "Tournament", event: "Server event" };

  var readStats = function (raw, now) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || raw.v !== 1) return null;
    var generated = when(raw.generated_at);
    if (generated === null) return null;
    var age = now - generated;
    if (!(age < STALE_MS) || age < -HOUR_MS) return null; // stale, or a clock that makes no sense

    var out = { generated: generated, age: Math.max(age, 0), events: [], houses: [], games: [], tally: [] };

    var members = obj(raw.members);
    out.members = count(members.total);
    out.online = age < ONLINE_FRESH_MS ? count(members.online) : null;

    list(raw.events, 8).forEach(function (e) {
      e = obj(e);
      var title = text(e.title, 80);
      var start = when(e.start);
      if (!title || start === null || !KINDS.hasOwnProperty(e.kind) || start < now - STARTED_GRACE_MS) return;
      out.events.push({ title: title, start: start, kind: e.kind, game: text(e.game, 40) });
    });
    out.events.sort(function (a, b) { return a.start - b.start; });

    var seasonal = obj(raw.seasonal);
    var ends = when(seasonal.ends);
    var name = text(seasonal.name, 40);
    if (name && ends !== null && ends > now) {
      out.seasonal = { name: name, ends: ends };
    }

    list(raw.houses, 8).forEach(function (h) {
      h = obj(h);
      var houseName = text(h.name, 40);
      var points = count(h.points);
      if (houseName && points !== null) out.houses.push({ name: houseName, points: points });
    });
    out.houses.sort(function (a, b) { return b.points - a.points; });
    if (out.houses.length < 2) out.houses = [];

    list(raw.games, 5).forEach(function (g) {
      g = obj(g);
      var gameName = text(g.name, 40);
      var hours = count(g.hours);
      if (gameName && hours) out.games.push({ name: gameName, hours: hours });
    });

    var week = obj(raw.week);
    var hall = obj(raw.hall);
    var mm = obj(raw.matchmaking);
    [
      [count(week.messages), "messages this week"],
      [count(week.voice_hours), "hours in voice"],
      [count(mm.matches_week), "squads matched"],
      [count(hall.starred), "in the hall of fame"],
      [count(hall.badges), "badges earned"],
    ].forEach(function (row) {
      if (row[0]) out.tally.push(row);
    });
    return out;
  };

  var el = function (tag, className, content) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
  };

  var dateLabel = function (t, now) {
    if (t <= now) return "Now";
    var d = new Date(t);
    var today = new Date(now);
    var days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) -
      new Date(today.getFullYear(), today.getMonth(), today.getDate())) / (24 * HOUR_MS));
    if (days === 0) return "Today";
    if (days === 1) return "Tomorrow";
    try {
      return new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" }).format(d);
    } catch (e) {
      return d.toDateString();
    }
  };

  var timeLabel = function (t) {
    var d = new Date(t);
    try {
      return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(d);
    } catch (e) {
      return d.toLocaleTimeString();
    }
  };

  var shortDate = function (t) {
    try {
      return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(t));
    } catch (e) {
      return new Date(t).toDateString();
    }
  };

  var ago = function (ms) {
    var minutes = Math.round(ms / 60000);
    if (minutes < 2) return "Updated just now";
    if (minutes < 90) return "Updated " + minutes + " minutes ago";
    var hours = Math.round(minutes / 60);
    return "Updated " + hours + " hours ago";
  };

  // A row of the "Coming up" ledger: the day as the wall label, then title and detail.
  var dayRow = function (label, iso, title, detail) {
    var li = el("li");
    var head = el("span", "label");
    if (iso) {
      var time = el("time", null, label);
      time.setAttribute("datetime", iso);
      head.appendChild(time);
    } else {
      head.textContent = label;
    }
    var body = el("div");
    body.appendChild(el("h4", "card-title", title));
    body.appendChild(el("p", null, detail));
    li.appendChild(head);
    li.appendChild(body);
    return li;
  };

  // Fills the live section. True when there is something to show.
  var render = function (stats, now) {
    var events = byId("live-events-list");
    var houses = byId("live-houses-list");
    var games = byId("live-games-list");
    var updated = byId("live-updated");
    var tally = byId("live-tally");
    if (!events || !houses || !games || !updated || !tally) return false;

    var rows = 0;
    if (stats.seasonal) {
      var s = stats.seasonal;
      events.appendChild(dayRow("Now", null, s.name,
        "Seasonal event · ends " + shortDate(s.ends)));
      rows += 1;
    }
    stats.events.forEach(function (e) {
      var detail = (e.start <= now ? "Started " : "") + timeLabel(e.start) + " · " + KINDS[e.kind];
      if (e.game && e.title.indexOf(e.game) === -1) detail += " · " + e.game;
      events.appendChild(dayRow(dateLabel(e.start, now), new Date(e.start).toISOString(), e.title, detail));
      rows += 1;
    });
    if (rows) {
      byId("live-events-count").textContent = (rows < 10 ? "0" : "") + rows;
      byId("live-events").hidden = false;
    }

    if (stats.houses.length) {
      var top = stats.houses[0].points;
      stats.houses.forEach(function (h) {
        var li = el("li");
        li.appendChild(el("span", "card-title", h.name));
        li.appendChild(el("span", "label", fmt(h.points)));
        var bar = el("span", "bar");
        var fill = el("span", "bar-fill");
        bar.setAttribute("aria-hidden", "true");
        // CSSOM, not a style attribute: allowed by the page's style-src 'self'.
        fill.style.width = (top > 0 ? Math.max(2, Math.round((h.points / top) * 100)) : 0) + "%";
        bar.appendChild(fill);
        li.appendChild(bar);
        houses.appendChild(li);
      });
      byId("live-houses").hidden = false;
    }

    if (stats.games.length) {
      stats.games.forEach(function (g) {
        var li = el("li", "game");
        li.appendChild(el("span", null, g.name));
        li.appendChild(el("span", "game-hours", fmt(g.hours) + " h"));
        games.appendChild(li);
      });
      byId("live-games").hidden = false;
    }

    if (!rows && !stats.houses.length && !stats.games.length) return false;

    stats.tally.forEach(function (row) {
      var item = el("span");
      item.appendChild(el("strong", null, fmt(row[0])));
      item.appendChild(document.createTextNode(" " + row[1]));
      tally.appendChild(item);
    });
    if (stats.tally.length) tally.hidden = false;

    updated.textContent = ago(stats.age) + (stats.events.length ? ". Times are in your time zone." : ".");
    return true;
  };

  var jump = function (px) {
    try {
      window.scrollBy({ top: px, left: 0, behavior: "instant" });
    } catch (e) {
      window.scrollBy(0, px);
    }
  };

  // Show the section without moving anything the visitor is looking at. It opens
  // just above the next section, so: below the fold it simply appears; above the
  // viewport (or tucked under the sticky header, where a #games link lands) it
  // appears and the scroll position is corrected by its height; and while that
  // spot is on screen it waits until the visitor has scrolled past it.
  var reveal = function (section, force) {
    var next = section.nextElementSibling;
    var header = document.querySelector(".top");
    var attempt = function (forced) {
      if (!section.hidden) return true;
      var viewport = window.innerHeight || document.documentElement.clientHeight;
      var top = next ? next.getBoundingClientRect().top : viewport;
      if (forced || top >= viewport) {
        section.hidden = false;
        return true;
      }
      var edge = header ? Math.max(header.getBoundingClientRect().bottom, 0) : 0;
      if (top <= edge + 1) {
        section.hidden = false;
        var moved = next.getBoundingClientRect().top - top; // 0 where the browser anchors scroll itself
        if (moved) jump(moved);
        return true;
      }
      return false;
    };
    if (attempt(force)) return;
    var later = function () {
      if (!attempt(false)) return;
      window.removeEventListener("scroll", later);
      window.removeEventListener("resize", later);
    };
    window.addEventListener("scroll", later, { passive: true });
    window.addEventListener("resize", later);
  };

  // The masthead line: the next thing on, or the seasonal event that is running.
  var ticker = function (stats, now, section) {
    var box = byId("now");
    var label = byId("now-text");
    if (!box || !label) return;
    var next = stats.events[0];
    var line = null;
    if (next) {
      line = (next.start <= now ? "On now · " : "Next up · ") + next.title +
        (next.start <= now ? "" : " · " + dateLabel(next.start, now) + ", " + timeLabel(next.start));
    } else if (stats.seasonal) {
      line = "On now · " + stats.seasonal.name + " · ends " + shortDate(stats.seasonal.ends);
    }
    if (!line) return;
    label.textContent = line;
    box.hidden = false;
    var link = box.querySelector("a");
    // The link's target may still be waiting for a quiet moment: open it on demand.
    if (link) link.addEventListener("click", function () { section.hidden = false; });
  };

  var statsUrl = typeof cfg.STATS_URL === "string" ? cfg.STATS_URL.trim() : "";
  var statsReady = !statsUrl ? Promise.resolve(null) : getJson(statsUrl).then(function (raw) {
    try {
      var now = Date.now();
      var stats = readStats(raw, now);
      var section = byId("live");
      if (!stats || !section) return stats;
      if (render(stats, now)) {
        var asked = window.location.hash === "#live";
        reveal(section, asked);
        if (asked) section.scrollIntoView();
        ticker(stats, now, section);
      }
      return stats;
    } catch (e) {
      return null; // a surprise in the data: the page stays as it was
    }
  });

  // ------------------------------------------------------------ member counts
  var showCounts = function (members, online) {
    var box = byId("counts");
    var total = byId("count-members");
    if (!box || !total || typeof members !== "number") return;
    total.textContent = fmt(members);
    var hasOnline = typeof online === "number";
    if (hasOnline) byId("count-online").textContent = fmt(online);
    byId("count-online-wrap").hidden = !hasOnline;
    byId("count-sep").hidden = !hasOnline;
    box.hidden = false;
  };

  // Discord's own numbers first; if the invite endpoint is unreachable, the
  // bot's published member count stands in.
  var fromStats = function () {
    statsReady.then(function (stats) {
      if (stats && stats.members !== null) showCounts(stats.members, stats.online);
    });
  };

  if (!inviteUrl) {
    fromStats();
    return;
  }

  getJson("https://discord.com/api/v10/invites/" + encodeURIComponent(code) + "?with_counts=true")
    .then(function (data) {
      var members = data ? data.approximate_member_count : null;
      var online = data ? data.approximate_presence_count : null;
      if (typeof members !== "number" || typeof online !== "number") {
        fromStats(); // offline, blocked or expired invite
        return;
      }
      showCounts(members, online);
    });
})();
