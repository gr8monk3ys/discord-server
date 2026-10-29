"""The landing page's live section: hidden by default, fed only by the published aggregates,
rendered without innerHTML, and in step with the schema the bot and the publisher use."""

import json
import re
from pathlib import Path

import config  # noqa: F401  (puts server/ on sys.path)
import publish_site_data as pub
import site_schema as schema
from logic import sitegen

SITE = Path(__file__).resolve().parents[2] / "site"
RAW = "https://raw.githubusercontent.com/gr8monk3ys/discord-server/site-data/stats.json"


def read(name: str) -> str:
    return (SITE / name).read_text(encoding="utf-8")


def test_stats_url_lives_in_config_js_and_names_the_data_branch():
    url = re.search(r'STATS_URL\s*:\s*"([^"]*)"', read("config.js")).group(1)
    assert url == RAW
    assert url.endswith(f"/{pub.BRANCH}/{pub.FILE_NAME}")
    assert "raw.githubusercontent.com" not in read("app.js")  # app.js reads it from the config
    assert "cfg.STATS_URL" in read("app.js")
    # the invite line keeps the shape logic/textfilter.py parses
    assert re.search(r'INVITE_CODE\s*:\s*"[A-Za-z0-9-]+"', read("config.js"))


def test_csp_allows_the_stats_fetch_and_nothing_new_besides():
    cfg = json.loads(read("vercel.json"))
    value = next(h["value"] for rule in cfg["headers"] for h in rule["headers"]
                 if h["key"] == "Content-Security-Policy")
    csp = sitegen.parse_csp(value)
    assert sitegen.csp_allows(csp["connect-src"], RAW)
    # 'self': the appeal form posts to /api/appeal
    assert csp["connect-src"] == ["'self'", "https://discord.com", "https://raw.githubusercontent.com"]
    assert csp["script-src"] == ["'self'"] and csp["style-src"] == ["'self'", "https://fonts.googleapis.com"]


def test_app_js_renders_with_text_content_only():
    js = read("app.js")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                'setAttribute("style"', "cssText", "srcdoc", "javascript:"):
        assert bad not in js, bad
    assert "textContent" in js and "createElement" in js
    # only fixed attribute names are ever set from script
    assert set(re.findall(r'setAttribute\("([^"]+)"', js)) <= {"href", "rel", "aria-disabled", "aria-hidden",
                                                              "aria-current", "datetime"}


def test_app_js_has_a_timeout_and_a_staleness_limit():
    js = read("app.js")
    assert "AbortController" in js and "TIMEOUT_MS = 6000" in js
    assert "STALE_MS = 48 * HOUR_MS" in js
    assert 'credentials: "omit"' in js
    assert js.count("fetch(") == 1  # one helper does every request
    assert "raw.v !== 1" in js


def test_app_js_limits_match_the_schema():
    js = read("app.js")
    kinds = re.search(r"var KINDS = \{([^}]*)\}", js).group(1)
    assert tuple(re.findall(r"(\w+):", kinds)) == schema.KINDS
    assert f"list(raw.events, {schema.MAX_EVENTS})" in js
    assert f"list(raw.houses, {schema.MAX_HOUSES})" in js
    assert f"list(raw.games, {schema.MAX_GAMES})" in js
    assert f"text(e.title, {schema.MAX_TITLE})" in js and f"text(h.name, {schema.MAX_NAME})" in js
    assert f"MAX_COUNT = {schema.MAX_COUNT}" in js
    assert r"/\d{15,}/" in js and "/[@<>]/" in js  # the client's own ID and mention guard


def test_live_markup_is_hidden_until_script_fills_it():
    page = read("index.html")
    live = re.search(r'<section id="live" hidden>.*?</section>', page, re.S)
    assert live, "the live section starts hidden"
    live = live.group(0)
    for block in ("live-events", "live-houses", "live-games", "live-tally"):
        assert re.search(rf'id="{block}" hidden', live), block
    assert re.search(r'<p class="now label" id="now" hidden>', page)
    # between "Monkey SMP" and "House rules", outside the numbered sequence
    assert page.index('<section id="smp">') < page.index('<section id="live"') < page.index('<section id="rules">')
    assert re.search(r'<div class="ledger-head">\s*<h2>This week</h2>', live)
    assert not re.search(r'<p class="label">\d\d\b', live)
    assert "<li" not in live and "<img" not in live  # no placeholder content, no avatars
    assert 'href="#live"' in page


def test_nothing_member_shaped_in_the_live_markup_or_script():
    for name in ("index.html", "app.js"):
        text = read(name).lower()
        for bad in ("avatar", "display_name", "username", "user_id", "cdn.discordapp.com"):
            assert bad not in text, f"{bad} in {name}"


def test_live_styles_use_only_the_house_tokens():
    css = read("style.css")
    assert "[hidden] { display: none !important; }" in css
    live = css[css.index("/* -------------------------------------------------------------------- live */"):]
    live = live[:live.index("/* ------------------------------------------------------------------ motion */")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", live), "a colour outside the token set"
    assert "rgb(" not in live and "hsl(" not in live and "@font-face" not in live
    assert set(re.findall(r"var\((--[\w-]+)\)", live)) <= {"--pen", "--tint", "--ink-muted", "--mono", "--default",
                                                           "--ease-ui"}
    assert "animation" not in live  # nothing to switch off for reduced motion
    for selector in (".now", ".live-body", ".houses", ".bar", ".bar-fill", ".game-hours", ".tally"):
        assert selector + " {" in live or selector + " a {" in live, selector


def test_site_readme_documents_the_live_section():
    text = read("README.md")
    assert "site-data" in text and "stats.json" in text and "publish_site_data.py" in text
