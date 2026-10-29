"""The landing page (site/, a Next.js app): its games and rules lists are generated from
server/layout.py, and its sources keep the house rules (one origin, no trackers, the nonce CSP,
the lscaturchio.xyz design system). The rendered markup and metadata are tested by Vitest in
site/src/app/pages.test.tsx; the route handlers in site/src/server/*.test.ts."""

import json
import re
from pathlib import Path

import pytest

import config
from logic import sitegen

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
SRC = SITE / "src"
GENERATED = SRC / "data" / "generated"
CANONICAL = "https://discord.lscaturchio.xyz/"

# Folders that hold installs and build output, never the site's own text.
_SKIP_DIRS = {"node_modules", ".next", "test-results", "playwright-report", "out"}


def site_files(suffixes=(".ts", ".tsx", ".mjs", ".cjs", ".css", ".json", ".svg", ".md")):
    """The site's own files (not installs, build output or the lockfile)."""
    for path in sorted(SITE.rglob("*")):
        rel = path.relative_to(SITE)
        if not path.is_file() or _SKIP_DIRS & set(rel.parts) or path.name == "bun.lock":
            continue
        if path.suffix in suffixes:
            yield path


def read(rel: str) -> str:
    return (SITE / rel).read_text(encoding="utf-8")


# ------------------------------------------------------------ generators


def test_games_data_lists_each_game_in_order_by_name_only():
    out = sitegen.games_data([("🎯", "valorant", "Valorant"), ("🧱", "roblox", "Roblox")])
    assert out == ["Valorant", "Roblox"]  # names only; emoji stay in Discord


def test_games_data_keeps_text_as_text():
    # React escapes text when it renders, so the data keeps the name exactly as written
    assert sitegen.games_data([("<b>", "x", "Tom & Jerry <3")]) == ["Tom & Jerry <3"]


def test_rules_data_takes_the_first_sentence_as_the_lead():
    out = sitegen.rules_data("`01`  18+ only. If you're under 18, leave.\n`02`  No cheats, no exploits.")
    assert out == [
        {"lead": ["18+ only."], "body": ["If you're under 18, leave."]},
        {"lead": ["No cheats, no exploits."], "body": []},
    ]


def test_rules_data_drops_discord_markup():
    out = sitegen.rules_data("`05`  Clips in {#clips}. Ping {@LFG}, use `/report`, hide ||spoilers||. <b>")
    assert out == [{"lead": ["Clips in #clips."], "body": ["Ping @LFG, use ", {"code": "/report"}, ", hide spoilers. <b>"]}]
    flat = json.dumps(out)
    assert "{#" not in flat and "{@" not in flat and "||" not in flat
    # "<b>" stays a plain string: the page renders segments as text, never as HTML


def test_rules_data_skips_lines_that_are_not_rules():
    assert sitegen.rules_data("Intro line\n\n`01`  Be kind.") == [{"lead": ["Be kind."], "body": []}]


def test_dump_json_is_stable_text():
    text = sitegen.dump_json(["Risk of Rain 2", "Café"])
    assert text == '[\n  "Risk of Rain 2",\n  "Café"\n]\n'
    assert sitegen.dump_json(json.loads(text)) == text


def test_valid_invite_code():
    assert sitegen.valid_invite_code("chill-gamers")
    assert sitegen.valid_invite_code("AbC123")
    assert not sitegen.valid_invite_code("REPLACE_ME")
    assert not sitegen.valid_invite_code("")
    assert not sitegen.valid_invite_code("https://discord.gg/abc")
    assert not sitegen.valid_invite_code("a b")


def test_parse_csp_and_csp_allows():
    csp = sitegen.parse_csp("default-src 'self'; connect-src https://discord.com;; IMG-SRC 'self' data:")
    assert csp == {"default-src": ["'self'"], "connect-src": ["https://discord.com"],
                   "img-src": ["'self'", "data:"]}
    assert sitegen.csp_allows(csp["connect-src"], "https://discord.com/api/v10/invites/x")
    assert not sitegen.csp_allows(csp["connect-src"], "https://discord.com.evil.test/x")
    assert not sitegen.csp_allows(csp["connect-src"], "style.css")
    assert sitegen.csp_allows(csp["img-src"], "img/a.png")
    assert sitegen.csp_allows(csp["img-src"], "data:image/png;base64,AAAA")


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


# ------------------------------------------------------------ the real site


def test_site_games_match_layout():
    """Run `python -m logic.sitegen` from bot/ after editing layout.GAMES."""
    games = [(g.emoji, g.channel, g.role) for g in config.GAMES]
    want = sitegen.dump_json(sitegen.games_data(games))
    assert (GENERATED / "games.json").read_bytes().decode("utf-8") == want


def test_site_rules_match_the_rules_embed():
    """Run `python -m logic.sitegen` from bot/ after editing the rules in layout.POSTS."""
    description = config.layout.POSTS["rules"]["description"]
    want = sitegen.dump_json(sitegen.rules_data(description))
    assert (GENERATED / "rules.json").read_bytes().decode("utf-8") == want
    assert len(json.loads(want)) == len(description.splitlines())


def test_the_page_renders_the_generated_lists():
    page = read("src/app/page.tsx")
    rules = read("src/components/Rules.tsx")
    assert 'from "@/data/generated/games.json"' in page + rules
    assert 'from "@/data/generated/rules.json"' in page + rules


def test_site_invite_code_lives_only_in_the_site_config():
    code = json.loads(read("src/data/site-config.json"))["inviteCode"]
    assert code == "REPLACE_ME" or sitegen.valid_invite_code(code)
    for path in site_files((".ts", ".tsx", ".mjs", ".cjs", ".css", ".json", ".svg")):
        if path.name == "site-config.json":
            continue
        text = path.read_text(encoding="utf-8")
        assert "discord.gg/" not in text.replace("discord.gg/${", ""), path
        assert "discord.com/invite" not in text, path
        assert code == "REPLACE_ME" or code not in text, path


def test_site_has_no_trackers_or_ids():
    for path in site_files():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d{17,20}\b", text), f"snowflake-like ID in {path.name}"
        if path.suffix == ".md":
            continue  # the README names what was left out on purpose
        for bad in ("googletagmanager", "google-analytics", "facebook.net", "hotjar", "plausible", "posthog",
                    "sentry", "@vercel/analytics", "@vercel/speed-insights"):
            assert bad not in text.lower(), f"{bad} in {path.name}"


def test_site_dependencies_are_the_framework_only():
    pkg = json.loads(read("package.json"))
    assert set(pkg["dependencies"]) == {"next", "next-themes", "react", "react-dom"}
    assert pkg["engines"]["node"] == ">=22"
    for script in ("lint", "typecheck", "test", "build"):
        assert script in pkg["scripts"], script
    assert (SITE / "bun.lock").is_file() and not (SITE / "package-lock.json").exists()


def test_site_assets_referenced_exist():
    refs = set()
    for path in site_files((".ts", ".tsx")):
        refs |= set(re.findall(r'/(img/[\w.-]+\.(?:png|svg))["`]', path.read_text(encoding="utf-8")))
    assert {"img/cursive.svg", "img/favicon.png", "img/server-icon.png", "img/og.png"} <= refs
    for ref in refs:
        assert (SITE / "public" / ref).is_file(), ref


def test_og_image_exists_and_is_1200x630():
    pil = pytest.importorskip("PIL.Image")
    path = SITE / "public" / "img" / "og.png"
    assert path.is_file()
    with pil.open(path) as im:
        assert im.size == (1200, 630)
    assert path.stat().st_size < 1_000_000  # unfurlers skip huge images


def test_wordmark_is_a_plain_svg():
    svg = read("public/img/cursive.svg")
    assert svg.lstrip().startswith("<?xml") and "<svg" in svg
    for bad in ("<script", "href=", "<style", "<foreignObject", "<image"):
        assert bad not in svg, bad


def test_canonical_is_one_origin_everywhere():
    """No GitHub Pages mirror any more (the route handlers need a server): nothing points at one."""
    assert not (ROOT / ".github" / "workflows" / "pages.yml").exists()
    for path in site_files():
        assert "github.io" not in path.read_text(encoding="utf-8"), path
    assert 'SITE_ORIGIN = "https://discord.lscaturchio.xyz"' in read("src/lib/site.ts")
    assert f'CANONICAL = `${{SITE_ORIGIN}}/`' in read("src/lib/site.ts")


# ------------------------------------------------- lscaturchio.xyz design system

def _css():
    return read("src/app/field-notebook.css")


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


def test_tailwind_maps_the_house_tokens_and_adds_no_reset():
    css = read("src/app/globals.css")
    # theme and utilities only: no preflight, so field-notebook.css stays the only base layer
    assert '@import "tailwindcss/theme.css" layer(theme);' in css
    assert '@import "tailwindcss/utilities.css" layer(utilities);' in css
    assert '@import "tailwindcss/preflight' not in css and '@import "tailwindcss";' not in css
    theme = re.search(r"@theme inline \{(.*?)\}", css, re.S).group(1)
    for token in ("--page", "--card", "--tint", "--ink", "--ink-muted", "--hairline", "--pen"):
        assert f"var({token})" in theme, token
    assert '@import "./field-notebook.css";' in css and '@import "./appeal.css";' in css


def test_house_type_and_motion_rules():
    css = _css()
    for var in ("--font-fraunces", "--font-instrument-sans", "--font-plex-mono"):
        assert f"var({var})" in css, var
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


def test_fonts_are_the_main_sites_cuts_and_first_party():
    """next/font, as on lscaturchio.xyz: self-hosted at build time (the browser never talks to
    Google), Fraunces without the opsz axis (asking for it draws visibly thinner display type
    than the main site) and the metric-matched fallbacks next/font generates."""
    fonts = read("src/app/fonts.ts")
    assert 'from "next/font/google"' in fonts
    for face in ("Fraunces(", "Instrument_Sans(", "IBM_Plex_Mono("):
        assert face in fonts, face
    assert "opsz" not in fonts and "axes" not in fonts
    assert 'weight: ["400", "500", "600"]' in fonts  # Plex Mono's cuts
    assert fonts.count('display: "swap"') == 3
    assert "fontVariables" in read("src/app/layout.tsx")
    for path in site_files((".ts", ".tsx", ".css")):
        text = path.read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text and "fonts.gstatic.com" not in text, path
    css = _css() + read("src/app/appeal.css") + read("src/app/globals.css")
    assert "url(http" not in css and "@font-face" not in css  # nothing remote from the stylesheets


def test_house_values_match_the_main_site():
    css = _css()
    assert "h1 { font-size: clamp(2.4rem, 5.2vw, 4.9rem); font-weight: 720;" in css
    assert "font-size: clamp(1.55rem, 2.4vw, 2.25rem); font-weight: 650;" in css
    assert "--sheet-shadow: 0 8px 30px rgba(0, 0, 0, 0.08);" in css
    assert css.count("box-shadow:") == 3  # the sheet, the ledge, and the disabled reset
    assert "grid-template-columns: minmax(280px, 360px) 1fr;" in css  # the ledger rail
    assert "grid-template-columns: 1.1fr 2fr;" in css  # the footer split
    assert "max-width: 72rem" in _rule(css, ".wrap")


def test_theme_toggle_is_next_themes_on_the_same_attribute_and_key():
    provider = read("src/components/ThemeProvider.tsx")
    for want in ('attribute="data-theme"', 'defaultTheme="system"', "enableSystem", 'storageKey="theme"',
                 "enableColorScheme={false}", "nonce={nonce}"):
        assert want in provider, want
    layout = read("src/app/layout.tsx")
    assert 'colorScheme: "light dark"' in layout and "suppressHydrationWarning" in layout
    assert 'headers()).get("x-nonce")' in layout
    toggle = read("src/components/ThemeToggle.tsx")
    assert "data-theme-toggle" in toggle and 'aria-label="Toggle theme"' in toggle
    # every page has the toggle: the header component carries it
    assert "<ThemeToggle />" in read("src/components/Header.tsx")


# -------------------------------------------------------------- deploy and headers

def _security():
    return json.loads(read("src/data/security.json"))


def test_vercel_json_builds_next_with_bun():
    cfg = json.loads(read("vercel.json"))
    assert cfg["framework"] == "nextjs"
    assert cfg["installCommand"] == "bun install --frozen-lockfile"
    assert cfg["buildCommand"] == "bun run build"
    # headers, redirects and rewrites live in next.config.ts and src/proxy.ts, in one place
    for key in ("headers", "redirects", "rewrites", "cleanUrls", "outputDirectory"):
        assert key not in cfg, key


def test_security_headers():
    h = _security()["headers"]
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in h["Permissions-Policy"] and "geolocation=()" in h["Permissions-Policy"]
    assert h["X-Frame-Options"] == "DENY"
    conf = read("next.config.ts")
    assert '{ source: "/:path*", headers: list(SECURITY_HEADERS) }' in conf
    assert "poweredByHeader: false" in conf


def test_the_csp_is_nonce_based_and_strict():
    csp = _security()["csp"]
    assert csp["default-src"] == ["'self'"]
    assert csp["script-src"] == ["'self'", "'nonce-{nonce}'"]
    assert csp["style-src"] == ["'self'", "'nonce-{nonce}'"]
    # counts from Discord, live aggregates from this repo's site-data branch, the appeal form on this origin
    assert csp["connect-src"] == ["'self'", "https://discord.com", "https://raw.githubusercontent.com"]
    assert csp["font-src"] == ["'self'"]  # next/font serves the fonts from this origin
    assert csp["form-action"] == ["'none'"]  # forms only ever submit through fetch
    assert csp["frame-ancestors"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert csp["base-uri"] == ["'self'"]
    assert "unsafe" not in json.dumps(csp) and "strict-dynamic" not in json.dumps(csp)
    assert _security()["assetCsp"] == "default-src 'none'; frame-ancestors 'none'"
    # a fresh nonce per request, on the request (Next reads it) and on the response
    proxy = read("src/proxy.ts")
    assert "crypto.randomUUID()" in proxy and 'requestHeaders.set("x-nonce", nonce)' in proxy
    assert 'response.headers.set("Content-Security-Policy", csp)' in proxy
    # 'unsafe-eval' and inline styles only ever in development
    site = read("src/lib/site.ts")
    assert "if (dev && directive === \"script-src\")" in site
    assert 'process.env.NODE_ENV === "development"' in proxy


def test_every_fetched_url_is_allowed_by_the_csp():
    """The browser fetches through one helper (getJson) and the appeal form posts to /api/appeal."""
    csp = _security()["csp"]
    fetched = {json.loads(read("src/data/site-config.json"))["statsUrl"], "/api/appeal"}
    for path in site_files((".ts", ".tsx")):
        if ".test." in path.name:
            continue
        for arg in re.findall(r"getJson\(([^)]*)\)", path.read_text(encoding="utf-8")):
            if arg.startswith(("`https://", '"https://')):
                fetched.add(re.match(r"[`\"](https://[^`\"$?]+)", arg).group(1))
    assert "https://discord.com/api/v10/invites/" in fetched
    for url in fetched:
        assert sitegen.csp_allows(csp["connect-src"], url), url


def test_pages_have_no_inline_code_or_styles_in_the_source():
    for path in site_files((".tsx",)):
        if ".test." in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        assert "<style" not in text, path
        assert not re.search(r"\son[a-z]+=\"", text), f"inline handler in {path}"
        # the one style prop is the house bars' width, set by React on the client (CSSOM,
        # which the CSP allows), never server-rendered
        styles = re.findall(r"style=\{", text)
        if path.name == "LiveSection.tsx":
            assert len(styles) == 1
        else:
            assert not styles, path
        for tag in re.findall(r"<script\b[^>]*>", text):
            assert 'type="application/ld+json"' in tag, path  # a data block, never executed


def test_noindex_routes():
    conf = read("next.config.ts")
    assert '{ source: "/join", headers: list({ "X-Robots-Tag": "noindex, follow" }) }' in conf
    assert '{ source: "/(appeal|linked-role)", headers: list({ "X-Robots-Tag": "noindex, nofollow" }) }' in conf
    assert '{ source: "/api/:path*", headers: list({ "X-Robots-Tag": "noindex, nofollow" }) }' in conf


def test_join_redirects_to_the_invite_from_the_config_only():
    conf = read("next.config.ts")
    assert "INVITE_URL ? [{ source: \"/join\", destination: INVITE_URL, permanent: false }] : []" in conf
    site = read("src/lib/site.ts")
    assert "validInviteCode(INVITE_CODE) ? `https://discord.gg/${INVITE_CODE}` : null" in site
    assert "siteConfig.inviteCode" in site
    # the fallback page needs no script and steers nothing from the URL
    join = read("src/app/join/page.tsx")
    for bad in ("location", "searchParams", "useEffect", "http-equiv"):
        assert bad not in join, bad


def test_ci_builds_and_tests_the_site_with_bun():
    wf = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = wf[wf.index("\n  site:"):]
    assert re.search(r"uses: oven-sh/setup-bun@v\d+\n\s+with:\n\s+bun-version: \"1\.3\.9\"", job)
    assert "working-directory: site" in job
    steps = ["bun install --frozen-lockfile", "bun run lint", "bun run typecheck", "bun run test", "bun run build"]
    positions = [job.index(f"run: {s}") for s in steps]
    assert positions == sorted(positions)
    assert "node --test" not in wf  # the old function tests are Vitest tests now
    # the lockfile is one bun 1.3 reads
    assert '"lockfileVersion": 1,' in read("bun.lock")
    tests = [p for p in (SRC).rglob("*") if re.search(r"\.test\.tsx?$", p.name)]
    assert len(tests) >= 4


# ------------------------------------------------------------ /appeal (ban appeals)


def test_appeal_limits_agree_between_page_handler_and_bot():
    from logic import appeals

    form = read("src/lib/appeal-form.ts")
    handler = read("src/server/appeal.ts")
    page = read("src/components/AppealForm.tsx") + form
    for q in appeals.QUESTIONS:
        assert f'{{ name: "{q.key}", required: {str(q.required).lower()}, max: {q.max_length} }}' in form, q.key
        assert f'["{q.key}", {q.max_length}, {str(q.required).lower()}]' in handler, q.key
        assert q.label in page, q.label
    assert f"export const CODE_LEN = {appeals.CODE_LEN};" in handler
    assert f"export const SECRET_MIN = {config.APPEAL_SECRET_MIN};" in handler
    assert f'export const SITE_ORIGIN = "{config.SITE_URL}";' in handler
    assert int(re.search(r"export const PAYLOAD_MAX = (\d+);", handler).group(1)) <= appeals.PAYLOAD_MAX


def test_appeal_form_posts_to_its_own_origin_and_keeps_nothing():
    js = read("src/components/AppealForm.tsx")
    assert "event.preventDefault()" in js and 'fetch("/api/appeal"' in js
    assert 'credentials: "omit"' in js
    for bad in ("innerHTML", "eval(", "document.write", "localStorage", "sessionStorage", "document.cookie",
                "location.search", "console.", "https://"):
        assert bad not in js, bad  # same origin only, nothing stored, nothing logged


def test_appeal_handler_has_no_dependencies_and_logs_no_secrets():
    fn = read("src/server/appeal.ts")
    assert re.findall(r'^import .* from "([^"]+)";', fn, re.M) == ["node:crypto"]
    assert "timingSafeEqual" in fn and "allowed_mentions: { parse: [] }" in fn
    logged = re.findall(r"say\((.*)\);", fn)
    assert len(logged) == 2 and "console.log" not in fn
    for line in logged:
        assert not re.search(r"secret|code|answers|webhookUrl|payload|body|message", line), line
    route = read("src/app/api/appeal/route.ts")
    assert 'export const dynamic = "force-dynamic";' in route


def test_only_the_linked_role_sign_in_sets_a_cookie():
    """The Linked Roles sign-in (/api/linked-role/*) sets one short-lived cookie, so no page may
    say the site has none (pages.test.tsx checks the footers); nothing else sets one."""
    lib = read("src/server/linked-role/lib.ts")
    assert "HttpOnly; Secure; SameSite=Lax" in lib and "STATE_TTL" in lib
    assert "__Host-" in lib
    for path in site_files((".ts", ".tsx")):
        rel = path.relative_to(SRC).as_posix() if SRC in path.parents else path.name
        text = path.read_text(encoding="utf-8").lower()
        if rel.startswith("server/linked-role/") or ".test." in path.name:
            continue
        assert "set-cookie" not in text and "document.cookie" not in text and "cookies()" not in text, rel
