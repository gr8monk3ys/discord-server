"""The landing page's games list is generated from server/layout.py GAMES."""

import re
from pathlib import Path

import pytest

import config
from logic import sitegen

SITE = Path(__file__).resolve().parents[2] / "site"


def test_render_games_lists_each_game_in_order():
    html = sitegen.render_games([("🎯", "valorant", "Valorant"), ("🧱", "roblox", "Roblox")])
    assert html.index("Valorant") < html.index("Roblox")
    assert html.count("<li") == 2
    assert "🎯" not in html and "game-emoji" not in html  # names only; emoji stay in Discord


def test_render_games_escapes_html():
    html = sitegen.render_games([("<b>", "x", "Tom & Jerry <3")])
    assert "Tom &amp; Jerry &lt;3" in html
    assert "<b>" not in html


def test_replace_block_swaps_only_the_marked_region():
    page = "a\n<!-- games:start -->\nold\n<!-- games:end -->\nb"
    out = sitegen.replace_block(page, "games", "NEW")
    assert out == "a\n<!-- games:start -->\nNEW\n<!-- games:end -->\nb"


def test_replace_block_is_idempotent():
    page = "<!-- games:start --><!-- games:end -->"
    once = sitegen.replace_block(page, "games", "X")
    assert sitegen.replace_block(once, "games", "X") == once


def test_replace_block_requires_markers():
    with pytest.raises(ValueError):
        sitegen.replace_block("no markers here", "games", "X")


def test_extract_block_round_trips():
    page = sitegen.replace_block("<!-- games:start --><!-- games:end -->", "games", "BODY")
    assert sitegen.extract_block(page, "games") == "BODY"


def test_valid_invite_code():
    assert sitegen.valid_invite_code("chill-gamers")
    assert sitegen.valid_invite_code("AbC123")
    assert not sitegen.valid_invite_code("REPLACE_ME")
    assert not sitegen.valid_invite_code("")
    assert not sitegen.valid_invite_code("https://discord.gg/abc")
    assert not sitegen.valid_invite_code("a b")


# ------------------------------------------------------------ the real site


def test_site_games_match_layout():
    """Run `python -m logic.sitegen` from bot/ after editing layout.GAMES."""
    page = (SITE / "index.html").read_text(encoding="utf-8")
    games = [(g.emoji, g.channel, g.role) for g in config.GAMES]
    assert sitegen.extract_block(page, "games").strip() == sitegen.render_games(games).strip()


def test_site_invite_code_lives_only_in_config_js():
    cfg = (SITE / "config.js").read_text(encoding="utf-8")
    assert re.search(r'INVITE_CODE\s*[:=]\s*"[^"]+"', cfg)
    for name in ("index.html", "404.html", "join/index.html", "app.js", "join.js", "theme.js",
                 "style.css", "vercel.json"):
        text = (SITE / name).read_text(encoding="utf-8")
        assert "discord.gg/" not in text.replace("discord.gg/${", "")


def test_site_has_no_trackers_or_ids():
    for path in SITE.rglob("*"):
        if path.suffix not in {".html", ".js", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d{17,20}\b", text), f"snowflake-like ID in {path.name}"
        for bad in ("googletagmanager", "google-analytics", "facebook.net", "hotjar", "plausible"):
            assert bad not in text, f"{bad} in {path.name}"


def test_site_invite_code_is_placeholder_or_valid():
    cfg = (SITE / "config.js").read_text(encoding="utf-8")
    code = re.search(r'INVITE_CODE\s*:\s*"([^"]*)"', cfg).group(1)
    assert code == "REPLACE_ME" or sitegen.valid_invite_code(code)


def test_site_join_buttons_are_wired_by_app_js():
    page = (SITE / "index.html").read_text(encoding="utf-8")
    assert page.count("data-join") >= 3
    assert '<script src="config.js"' in page and '<script src="app.js"' in page
    assert page.index('src="config.js"') < page.index('src="app.js"')
    js = (SITE / "app.js").read_text(encoding="utf-8")
    assert "with_counts=true" in js and "approximate_presence_count" in js


def test_site_assets_referenced_exist():
    for name in ("index.html", "404.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        refs = re.findall(r'(?:src|href)="/?((?:img/)?[\w.-]+\.(?:png|svg|css|js))"', page)
        assert refs, name
        for src in set(refs):
            assert (SITE / src).is_file(), f"{name}: {src}"


# ------------------------------------------------------- social cards / SEO

CANONICAL = "https://discord.lscaturchio.xyz/"


def test_meta_tags_reads_property_and_name_in_any_attribute_order():
    page = (
        '<meta property="og:title" content="A &amp; B">'
        '<meta content="Big" name="twitter:card">'
        "<meta charset=\"utf-8\">"
    )
    assert sitegen.meta_tags(page) == {"og:title": "A & B", "twitter:card": "Big"}


def test_cover_box_crops_the_long_side_centred():
    # 1920x1080 into 1200x630 (wider): keep full width, trim top and bottom equally
    left, top, right, bottom = sitegen.cover_box(1920, 1080, 1200, 630)
    assert (left, right) == (0, 1920)
    assert abs((right - left) / (bottom - top) - 1200 / 630) < 0.01
    assert abs(top - (1080 - bottom)) <= 1
    # a square target from a wide source keeps the full height and trims the sides
    assert sitegen.cover_box(1920, 1080, 1080, 1080) == (420, 0, 1500, 1080)


def test_cover_box_same_ratio_is_the_whole_image():
    assert sitegen.cover_box(2400, 1260, 1200, 630) == (0, 0, 2400, 1260)


def _page():
    return (SITE / "index.html").read_text(encoding="utf-8")


def test_site_has_canonical_url():
    assert f'<link rel="canonical" href="{CANONICAL}">' in _page()


def test_site_has_open_graph_and_twitter_cards():
    meta = sitegen.meta_tags(_page())
    for key in ("og:title", "og:description", "og:image", "og:url", "og:type", "og:site_name",
                "og:image:alt", "twitter:card", "twitter:title", "twitter:description",
                "twitter:image", "twitter:image:alt", "description"):
        assert meta.get(key, "").strip(), key
    assert meta["og:url"] == CANONICAL
    assert meta["og:image"] == CANONICAL + "img/og.png"  # crawlers need an absolute URL
    assert meta["twitter:image"] == meta["og:image"]
    assert meta["twitter:card"] == "summary_large_image"
    assert (meta["og:image:width"], meta["og:image:height"]) == ("1200", "630")
    assert len(meta["og:description"]) <= 200 and len(meta["og:title"]) <= 70


def test_og_image_exists_and_is_1200x630():
    pil = pytest.importorskip("PIL.Image")
    path = SITE / "img" / "og.png"
    assert path.is_file()
    with pil.open(path) as im:
        assert im.size == (1200, 630)
    assert path.stat().st_size < 1_000_000  # unfurlers skip huge images


def test_site_json_ld_is_minimal_organization_and_website():
    import json

    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', _page(), re.S)
    assert len(blocks) == 1
    data = json.loads(blocks[0])
    types = {node["@type"]: node for node in data["@graph"]}
    assert set(types) == {"Organization", "WebSite"}
    assert types["WebSite"]["url"] == CANONICAL and types["Organization"]["url"] == CANONICAL
    assert types["Organization"]["logo"].startswith(CANONICAL + "img/")
    assert (SITE / types["Organization"]["logo"][len(CANONICAL):]).is_file()


def test_site_leads_with_what_a_new_member_does():
    """A small server listing a dozen systems reads as empty machinery: four first-week
    entries with a title each, then one short untitled list for the rest."""
    features = re.search(r'<section id="features".*?</section>', _page(), re.S).group(0)
    assert len(re.findall(r'<li><h3 class="card-title">', features)) == 4
    assert len(re.findall(r"<li\b", features)) <= 8
    for want in ("Lobby", "/lfg", "Fern, Kestrel or Otter", "Monkey SMP", "/smp link"):
        assert want in features, want


def test_site_headline_is_fixed_and_the_first_screen_says_what_this_is():
    page = _page()
    assert "<h1>Chill gamers. <em>No tryhards, no toxicity.</em></h1>" in page
    hero = re.search(r'<section class="hero".*?</section>', page, re.S).group(0)
    for want in ("co-op and PvE", "Risk of Rain 2", "Gears of War", "Minecraft", "evenings", "US Pacific",
                 "data-join"):
        assert want in hero, want


def test_site_copy_names_no_retired_game_and_no_house_style_slip():
    """Outside the generated games block (python -m logic.sitegen owns that one)."""
    page = _page()
    start, end = page.index("<!-- games:start -->"), page.index("<!-- games:end -->")
    copy = page[:start] + page[end:]
    for gone in ("Valorant", "Call of Duty", "CoD", "Fortnite", "League of Legends", "Counter-Strike",
                 "GTA", "Roblox"):
        assert gone not in copy, gone
    assert "—" not in copy  # no em dashes
    assert "18+" not in sitegen.meta_tags(page)["description"] + sitegen.meta_tags(page)["og:description"]
    meta = sitegen.meta_tags(page)
    assert meta["og:description"] == meta["twitter:description"]


def test_site_week_strip_and_faq_promise_only_what_happens():
    page = _page()
    week = re.search(r'<section id="week".*?</section>', page, re.S)
    assert week, "the weekly schedule strip"
    week = week.group(0)
    assert "<h2>A normal week</h2>" in week
    # the Friday game night is the bot's calendar entry, not a promise of a crowd
    assert "puts a 9 PM game night on the calendar" in week and "Tournament night" not in week
    faq = re.search(r'<section id="faq".*?</section>', page, re.S).group(0)
    assert 5 <= faq.count("<details>") <= 8
    for want in ("How busy is it?", "/smp link", "US Pacific"):
        assert want in faq, want
    for machinery in ("/partner", "Mythic", "/shop"):
        assert machinery not in faq, machinery
    assert '<p class="counts label" id="counts"' in page  # live counts stay, as a wall label


def test_canonical_is_one_origin_everywhere():
    """GitHub Pages is a mirror: no absolute URL on the page points at it."""
    page = _page()
    assert "github.io" not in page
    urls = set(re.findall(r'"(https://discord\.lscaturchio\.xyz[^"]*)"', page))
    assert urls and all(u.startswith(CANONICAL) for u in urls)


# ------------------------------------------------- lscaturchio.xyz design system

def _css():
    return (SITE / "style.css").read_text(encoding="utf-8")


def _vars(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


def _rule(css: str, selector: str) -> str:
    i = css.index(selector + " {")
    return css[i:css.index("}", i)]


THEME_TOKENS = {"--page", "--card", "--tint", "--ink", "--ink-muted", "--hairline", "--pen",
                "--pen-text"}


def test_both_themes_define_the_same_tokens():
    css = _css()
    paper = _vars(_rule(css, ":root"))
    media = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{(.*?)\n\}", css, re.S)
    assert media, "night theme follows prefers-color-scheme"
    system_night = _vars(_rule(media.group(1), ':root:not([data-theme="light"])'))
    forced_night = _vars(_rule(css, ':root[data-theme="dark"]'))
    assert THEME_TOKENS <= set(paper)
    assert THEME_TOKENS <= set(system_night)
    assert system_night == forced_night  # the toggle and the OS give the same night
    # the house values from lscaturchio.xyz DESIGN.md
    assert paper["--page"].lower() == "#f9f8f5" and paper["--pen"].lower() == "#184e35"
    assert forced_night["--page"].lower() == "#111317"
    assert forced_night["--pen"].lower() == "#42a979"


def test_house_type_and_motion_rules():
    css = _css()
    for family in ("Fraunces", "Instrument Sans", "IBM Plex Mono"):
        assert family in css, family
    assert "letter-spacing: 0.16em" in css  # the wall label
    assert ":focus-visible { outline: 2px solid var(--pen); outline-offset: 2px; }" in css
    reduce = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", css, re.S)
    # reduced motion stops what moves (slides, turns, presses) and keeps the colour fades;
    # no blanket 0.01ms kill, which also removes the feedback that moves nothing
    assert reduce and "transition: none" in reduce.group(1) and "transform: none" in reduce.group(1)
    assert "0.01ms" not in css
    # smooth scrolling only for readers who did not ask for less motion
    assert "@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }" in css
    assert css.count("scroll-behavior: smooth") == 1


def test_theme_toggle_is_wired_and_storage_is_guarded():
    js = (SITE / "theme.js").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in js and "data-theme" in js
    for m in re.finditer(r"localStorage", js):  # every access sits inside a try block
        before = js[:m.start()]
        assert before.rfind("try") > before.rfind("}"), "unguarded localStorage"
    for name in ("index.html", "404.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        head = page[:page.index("</head>")]
        assert re.search(r'<script src="/?theme\.js"></script>', head), name  # no defer: no flash
        assert "data-theme-toggle" in page and 'aria-label="Toggle theme"' in page, name
        assert '<meta name="color-scheme" content="light dark">' in page, name


def test_header_mirrors_lscaturchio():
    """Same bar as the main site: wordmark left (it is the way back), nav pill, controls."""
    for name in ("index.html", "404.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        header = re.search(r'<header class="top">.*?</header>', page, re.S).group(0)
        brand = re.search(r'<a class="brand-home" href="https://lscaturchio\.xyz"[^>]*>(.*?)</a>', header, re.S)
        assert brand, name
        assert re.search(r'<img class="wordmark" src="/?img/cursive\.svg" alt="Lorenzo Scaturchio"', brand.group(1)), name
        assert '<span class="brand-name">Lorenzo Scaturchio</span>' in brand.group(1), name
        # the icon names the theme you are in, as on the main site: sun first, moon stacked on it
        assert header.index('class="sun"') < header.index('class="moon"'), name
        # One Pen Rule: the header carries no filled button, the masthead keeps the only fill
        assert 'class="btn ' not in header and 'class="btn"' not in header, name
    header = re.search(r'<header class="top">.*?</header>', _page(), re.S).group(0)
    assert header.index('class="brand"') < header.index('<nav class="sections" aria-label="Primary">')         < header.index('class="controls"')
    assert re.search(r'<a class="btn-quiet" data-join', header)


def test_wordmark_is_a_plain_svg():
    svg = (SITE / "img" / "cursive.svg").read_text(encoding="utf-8")
    assert svg.lstrip().startswith("<?xml") and "<svg" in svg
    for bad in ("<script", "href=", "<style", "<foreignObject", "<image"):
        assert bad not in svg, bad


def test_footer_is_the_site_map_and_links_back():
    footer = re.search(r"<footer>.*?</footer>", _page(), re.S).group(0)
    assert '<h2 class="card-title">' in footer
    columns = re.findall(r'<nav aria-label="([^"]+)">\s*<h3 class="label">([^<]+)</h3>', footer)
    assert len(columns) >= 2 and all(label == heading for label, heading in columns)
    assert re.search(r'<a class="ink" href="https://lscaturchio\.xyz" rel="author">', footer)
    assert footer.count('class="ink"') == len(re.findall(r"<li>", footer))  # every link gets the pen
    legal = re.search(r'<div class="foot-legal">.*?</div>', footer, re.S).group(0)
    # fine print is sentence case, not a wall label: a whole line in tracked capitals is hard to read
    assert legal.count('<p class="fine">') == 2 and "Not affiliated with Discord" in legal
    assert 'class="label"' not in legal


def test_footers_do_not_promise_a_site_without_cookies():
    """The Linked Roles sign-in (/api/linked-role/*) sets one short-lived cookie, so no page may
    say the site has none; the page that starts that sign-in says so itself."""
    pages = sorted(SITE.rglob("*.html"))
    assert len(pages) >= 5
    for path in pages:
        page = path.read_text(encoding="utf-8")
        legal = re.search(r'<div class="foot-legal">.*?</div>', page, re.S).group(0)
        if path.parent.name == "linked-role":
            assert "sets one cookie" in legal, path
        else:
            assert "<p class=\"fine\">This page doesn't track you.</p>" in legal and "cookie" not in legal.lower(), path
    cookie = (SITE / "api" / "linked-role" / "_lib.js").read_text(encoding="utf-8")
    assert "Set-Cookie" in (SITE / "api" / "linked-role" / "start.js").read_text(encoding="utf-8")
    assert "HttpOnly; Secure; SameSite=Lax" in cookie and "STATE_TTL" in cookie
    # nothing else on the site sets one
    for path in SITE.rglob("*.js"):
        if path.relative_to(SITE).parts[:2] != ("api", "linked-role"):
            assert "cookie" not in path.read_text(encoding="utf-8").lower(), path


def test_fonts_are_the_main_sites_cuts():
    """next/font on lscaturchio.xyz ships Fraunces without the opsz axis; asking for it here
    (as this page once did) draws visibly thinner display type than the main site."""
    for name in ("index.html", "404.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        hrefs = re.findall(r'<link rel="stylesheet" href="(https://fonts\.googleapis\.com/[^"]+)"', page)
        assert len(hrefs) == 1, name
        href = hrefs[0]
        assert "opsz" not in href, name
        for want in ("family=Fraunces:wght@100..900", "family=Instrument+Sans:wght@400..700",
                     "family=IBM+Plex+Mono:wght@400;500;600", "display=swap"):
            assert want in href, f"{name}: {want}"
    css = _css()
    for fallback in ("Fraunces Fallback", "Instrument Sans Fallback", "IBM Plex Mono Fallback"):
        assert f'@font-face {{ font-family: "{fallback}"; src: local(' in css, fallback
    assert "@import" not in css and "url(http" not in css  # nothing remote from the stylesheet


def test_sections_are_ledgers_led_by_their_heading():
    """Every section is a ledger whose rail opens with the heading itself: no kicker line above
    it and no section numbers (the order of the page carries no information)."""
    page = _page()
    for sid in ("features", "week", "games", "rules", "faq"):
        section = re.search(rf'<section id="{sid}">.*?</section>', page, re.S).group(0)
        assert re.search(r'<div class="ledger-head">\s*<h2>[^<]+</h2>', section), sid
        assert '<div class="ledger-body">' in section, sid
    assert not re.search(r'<p class="label">\s*\d\d\b', page)
    main = re.search(r"<main.*?</main>", page, re.S).group(0)
    assert not re.search(r'<p class="label">[^<]*</p>\s*<h[12]', main)  # nothing sits on top of a heading
    assert page.count("<h1") == 1
    assert "btn-big" not in page and "btn-small" not in page  # the old button sizes are gone


def test_house_values_match_the_main_site():
    css = _css()
    assert "h1 { font-size: clamp(2.4rem, 5.2vw, 4.9rem); font-weight: 720;" in css
    assert "font-size: clamp(1.55rem, 2.4vw, 2.25rem); font-weight: 650;" in css
    assert "--sheet-shadow: 0 8px 30px rgba(0, 0, 0, 0.08);" in css
    assert css.count("box-shadow:") == 3  # the sheet, the ledge, and the disabled reset
    assert "grid-template-columns: minmax(280px, 360px) 1fr;" in css  # the ledger rail
    assert "grid-template-columns: 1.1fr 2fr;" in css  # the footer split
    assert _rule(css, ".wrap")  # 72rem page width
    assert "max-width: 72rem" in _rule(css, ".wrap")


def test_404_page():
    page = (SITE / "404.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex, follow">' in page
    assert "404" in page and "<title>" in page
    # served for missing URLs at any depth, so local links are root-absolute
    for ref in re.findall(r'(?:src|href)="([^"#]+)"', page):
        assert ref.startswith(("/", "https://")), ref


# -------------------------------------------------------------- Vercel deploy

def _vercel():
    import json

    return json.loads((SITE / "vercel.json").read_text(encoding="utf-8"))


def _headers_for(cfg, path):
    """Headers Vercel attaches to `path` (our sources are plain regexes, valid path-to-regexp)."""
    out = {}
    for rule in cfg["headers"]:
        if re.fullmatch(rule["source"], path):
            for h in rule["headers"]:
                assert h["key"] not in out, f"{h['key']} set twice for {path}"
                out[h["key"]] = h["value"]
    return out


def test_vercel_json_is_a_static_no_build_config():
    cfg = _vercel()
    assert cfg["cleanUrls"] is True
    assert cfg.get("framework") is None and cfg.get("installCommand") == ""
    assert not cfg.get("buildCommand")
    assert not (SITE / "package.json").exists()


def test_vercel_security_headers():
    h = _headers_for(_vercel(), "/")
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in h["Permissions-Policy"] and "geolocation=()" in h["Permissions-Policy"]
    csp = sitegen.parse_csp(h["Content-Security-Policy"])
    assert csp["default-src"] == ["'self'"]
    assert csp["script-src"] == ["'self'"]
    # counts from Discord, live aggregates from this repo's site-data branch, the appeal form on this origin
    assert csp["connect-src"] == ["'self'", "https://discord.com", "https://raw.githubusercontent.com"]  # 'self': the appeal form posts to /api/appeal
    assert csp["form-action"] == ["'none'"]  # forms only ever submit through fetch
    assert "https://fonts.googleapis.com" in csp["style-src"]
    assert "https://fonts.gstatic.com" in csp["font-src"]
    assert csp["frame-ancestors"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert "unsafe" not in h["Content-Security-Policy"]


def test_vercel_cache_headers():
    cfg = _vercel()
    for path in ("/img/og.png", "/img/server-icon.png"):
        cc = _headers_for(cfg, path)["Cache-Control"]
        assert int(re.search(r"max-age=(\d+)", cc).group(1)) >= 7 * 86400, path
    for path in ("/", "/404", "/index.html", "/style.css", "/config.js"):
        h = _headers_for(cfg, path)
        assert "max-age=0" in h["Cache-Control"], path
        assert "Content-Security-Policy" in h, path


def test_pages_obey_the_csp():
    """Everything the pages load is allowed by the policy; no inline code or styles."""
    csp = sitegen.parse_csp(_headers_for(_vercel(), "/")["Content-Security-Policy"])
    js = (SITE / "app.js").read_text(encoding="utf-8")
    fetched = re.findall(r'"(https://[^"]+)', js)
    assert fetched
    for url in fetched:
        assert sitegen.csp_allows(csp["connect-src"], url), url
    for name in ("index.html", "404.html", "join/index.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        assert "<style" not in page and " style=" not in page, name
        assert not re.search(r"\son[a-z]+=", page), f"inline handler in {name}"
        for attrs, body in re.findall(r"<script\b([^>]*)>(.*?)</script>", page, re.S):
            if 'type="application/ld+json"' in attrs:
                continue  # a data block, never executed
            src = re.search(r'src="([^"]+)"', attrs)
            assert src and not body.strip(), f"inline script in {name}"
            assert sitegen.csp_allows(csp["script-src"], src.group(1)), src.group(1)
        for href in re.findall(r'<link rel="stylesheet" href="([^"]+)"', page):
            assert sitegen.csp_allows(csp["style-src"], href), href
        for src in re.findall(r'<img [^>]*src="([^"]+)"', page):
            assert sitegen.csp_allows(csp["img-src"], src), src


# ------------------------------------------------------------ /join short link

def _join():
    return (SITE / "join" / "index.html").read_text(encoding="utf-8")


def test_join_page_exists_and_is_not_indexed():
    page = _join()
    assert sitegen.meta_tags(page)["robots"].startswith("noindex")
    assert f'<link rel="canonical" href="{CANONICAL}">' in page
    assert "<title>" in page and page.count("<h1") == 1
    assert _headers_for(_vercel(), "/join")["X-Robots-Tag"].startswith("noindex")
    assert "X-Robots-Tag" not in _headers_for(_vercel(), "/")


def test_join_page_has_no_hardcoded_invite():
    cfg = (SITE / "config.js").read_text(encoding="utf-8")
    code = re.search(r'INVITE_CODE\s*:\s*"([^"]*)"', cfg).group(1)
    for text in (_join(), (SITE / "join.js").read_text(encoding="utf-8")):
        assert "discord.gg/" not in text.replace("discord.gg/${", "")
        assert "discord.com/invite" not in text
        assert code == "REPLACE_ME" or code not in text
        assert "http-equiv" not in text.lower()  # no meta refresh: it would need the code


def test_join_page_loads_config_then_join_js_from_the_site_root():
    page = _join()
    scripts = re.findall(r'<script src="([^"]+)"', page)
    assert scripts == ["../theme.js", "../config.js", "../join.js"]
    # parent-relative: the site root from /join and /join/, the project folder on the mirror
    for ref in re.findall(r'(?:src|href)="([^"#]+)"', page):
        assert ref.startswith(("../", "https://")), ref
        if ref.startswith("../") and ref != "../":
            assert (SITE / ref[3:]).is_file(), ref
    assert "app.js" not in page  # no counts fetch on the way through


def test_join_page_has_a_fallback_and_a_noscript_note():
    page = _join()
    assert re.search(r'<a class="btn btn-lg" data-join href="\.\./">', page)  # home until join.js wires it
    noscript = re.search(r"<noscript>(.*?)</noscript>", page, re.S)
    assert noscript and "JavaScript" in noscript.group(1) and 'href="../"' in noscript.group(1)
    assert "<script" not in noscript.group(1) and "<meta" not in noscript.group(1)
    assert "data-theme-toggle" in page and '<meta name="color-scheme" content="light dark">' in page


def test_join_js_validates_like_app_js_and_replaces_location():
    js = (SITE / "join.js").read_text(encoding="utf-8")
    app = (SITE / "app.js").read_text(encoding="utf-8")
    check = re.search(r"var valid = .*;", app).group(0)
    assert check in js and "REPLACE_ME" in check
    assert "location.replace(inviteUrl)" in js
    assert "`https://discord.gg/${code}`" in js
    assert "if (inviteUrl) window.location.replace" in js  # never redirects on a bad code
    for bad in ("location.href", "location.assign", "innerHTML", "eval(", "fetch(", "document.write",
                "location.search", "location.hash"):  # nothing from the URL steers the redirect
        assert bad not in js, bad


def test_vercel_serves_join_with_and_without_the_slash():
    cfg = _vercel()
    assert cfg["cleanUrls"] is True and cfg["trailingSlash"] is False  # /join/ redirects to /join
    assert not cfg.get("redirects") and not cfg.get("rewrites")  # the invite is not in vercel.json
    h = _headers_for(cfg, "/join")
    assert "Content-Security-Policy" in h and "max-age=0" in h["Cache-Control"]
    assert "max-age=0" in _headers_for(cfg, "/join.js")["Cache-Control"]


def test_parse_csp_and_csp_allows():
    csp = sitegen.parse_csp("default-src 'self'; connect-src https://discord.com;; IMG-SRC 'self' data:")
    assert csp == {"default-src": ["'self'"], "connect-src": ["https://discord.com"],
                   "img-src": ["'self'", "data:"]}
    assert sitegen.csp_allows(csp["connect-src"], "https://discord.com/api/v10/invites/x")
    assert not sitegen.csp_allows(csp["connect-src"], "https://discord.com.evil.test/x")
    assert not sitegen.csp_allows(csp["connect-src"], "style.css")
    assert sitegen.csp_allows(csp["img-src"], "img/a.png")
    assert sitegen.csp_allows(csp["img-src"], "data:image/png;base64,AAAA")


def test_pages_workflow_rewrites_404_for_the_mirror():
    wf = (SITE.parent / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "site/404.html" in wf and "GITHUB_REPOSITORY" in wf
    assert "path: site" in wf


def test_ci_runs_the_site_function_tests():
    wf = (SITE.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = re.search(r"- name: Site function tests\n\s+working-directory: site/api\n\s+run: node --test\n", wf)
    assert step, "CI runs node --test from site/api"
    assert re.search(r"uses: actions/setup-node@v\d+\n\s+with:\n\s+node-version: \"\d+\"", wf)
    assert wf.index("actions/setup-node") < step.start()
    # the folder really holds tests that runner finds by itself, and they need no install
    tests = [p for p in (SITE / "api").rglob("*") if re.search(r"\.test\.(js|mjs|cjs)$", p.name)]
    assert len(tests) >= 2 and not (SITE / "package.json").exists()
    for path in tests:
        imports = re.findall(r"""(?:from\s+|require\()\s*["']([^"']+)["']""", path.read_text(encoding="utf-8"))
        assert imports and all(i.startswith(("node:", "./", "../")) for i in imports), path


# ------------------------------------------------------------ /appeal (ban appeals)

def _appeal():
    return (SITE / "appeal" / "index.html").read_text(encoding="utf-8")


def test_appeal_page_is_not_indexed_and_obeys_the_csp():
    page = _appeal()
    assert sitegen.meta_tags(page)["robots"] == "noindex, nofollow"
    assert _headers_for(_vercel(), "/appeal")["X-Robots-Tag"].startswith("noindex")
    assert _headers_for(_vercel(), "/api/appeal")["X-Robots-Tag"].startswith("noindex")
    assert "<title>" in page and page.count("<h1") == 1
    csp = sitegen.parse_csp(_headers_for(_vercel(), "/appeal")["Content-Security-Policy"])
    assert "<style" not in page and " style=" not in page
    assert not re.search(r"\son[a-z]+=", page), "inline handler"
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page, re.S)
    assert [re.search(r'src="([^"]+)"', a).group(1) for a, _ in scripts] == ["../theme.js", "../appeal/appeal.js"]
    assert all(not body.strip() for _, body in scripts)
    for href in re.findall(r'<link rel="stylesheet" href="([^"]+)"', page):
        assert sitegen.csp_allows(csp["style-src"], href), href
    # every local reference resolves the same from /appeal and /appeal/, and the file exists
    for ref in re.findall(r'(?:src|href)="([^"#]+)"', page):
        assert ref.startswith(("../", "https://")), ref
        if ref.startswith("../") and ref not in ("../", "../join"):
            assert (SITE / ref[3:]).is_file(), ref
    assert '<link rel="stylesheet" href="../style.css">' in page  # the shared look, not a copy of it


def test_appeal_form_is_labelled_and_never_submits_natively():
    page = _appeal()
    form = re.search(r"<form\b([^>]*)>(.*?)</form>", page, re.S)
    assert form and "action=" not in form.group(1) and "novalidate" in form.group(1)
    names = re.findall(r'<(?:input|textarea)\b[^>]*\bname="([^"]+)"', form.group(2))
    assert names == ["user_id", "code", "what", "why", "else"]
    for name in names:
        assert f'<label class="field-label" for="{name}">' in form.group(2), name
        assert f'id="{name}-error"' in form.group(2) and f"{name}-hint {name}-error" in form.group(2), name
    assert 'role="alert"' in page and 'id="appeal-done"' in page and "<noscript>" in page
    js = (SITE / "appeal" / "appeal.js").read_text(encoding="utf-8")
    assert "event.preventDefault()" in js and '"../api/appeal"' in js
    for bad in ("innerHTML", "eval(", "document.write", "localStorage", "document.cookie", "location.search",
                "console.", "https://"):
        assert bad not in js, bad  # same origin only, nothing stored, nothing logged


def test_appeal_limits_agree_between_page_function_and_bot():
    from logic import appeals

    page = _appeal()
    js = (SITE / "appeal" / "appeal.js").read_text(encoding="utf-8")
    fn = (SITE / "api" / "appeal.js").read_text(encoding="utf-8")
    for q in appeals.QUESTIONS:
        assert re.search(rf'id="{q.key}" name="{q.key}"[^>]*maxlength="{q.max_length}"', page), q.key
        assert ("required" in re.search(rf'<textarea[^>]*id="{q.key}"[^>]*>', page).group(0)) == q.required
        assert re.search(rf'name: "{q.key}", required: {str(q.required).lower()}, max: {q.max_length}', js), q.key
        assert f'["{q.key}", {q.max_length}, {str(q.required).lower()}]' in fn, q.key
        assert q.label in page
    assert f"const CODE_LEN = {appeals.CODE_LEN};" in fn
    assert f"const SECRET_MIN = {config.APPEAL_SECRET_MIN};" in fn
    assert f'const SITE_ORIGIN = "{config.SITE_URL}";' in fn
    assert int(re.search(r"const PAYLOAD_MAX = (\d+);", fn).group(1)) <= appeals.PAYLOAD_MAX


def test_appeal_function_has_no_dependencies_and_logs_no_secrets():
    fn = (SITE / "api" / "appeal.js").read_text(encoding="utf-8")
    assert re.findall(r'require\("([^"]+)"\)', fn) == ["node:crypto"] and "import " not in fn
    assert "module.exports = createHandler" in fn  # CommonJS: the site has no package.json
    assert "timingSafeEqual" in fn and "allowed_mentions: { parse: [] }" in fn
    logged = re.findall(r"say\((.*)\);", fn)
    assert len(logged) == 2 and "console.log" not in fn
    for line in logged:
        assert not re.search(r"secret|code|answers|webhookUrl|payload|body|message", line), line
    ignore = (SITE / ".vercelignore").read_text(encoding="utf-8").split()
    assert "api/*.test.mjs" in ignore  # every other file in api/ becomes a public function
    assert sorted(p.name for p in (SITE / "api").glob("appeal*")) == ["appeal.js", "appeal.test.mjs"]
