"""The landing page's live section: hidden by default, fed only by the published aggregates,
rendered as React text (never as HTML), and in step with the schema the bot and the publisher
use. The rendered markup is tested in site/src/app/pages.test.tsx; these tests hold the site's
sources to the bot's schema and to the publisher's branch and file name."""

import json
import re
from pathlib import Path

import config  # noqa: F401  (puts server/ on sys.path)
import publish_site_data as pub
import site_schema as schema
from logic import sitegen

SITE = Path(__file__).resolve().parents[2] / "site"
SRC = SITE / "src"
RAW = "https://raw.githubusercontent.com/gr8monk3ys/discord-server/site-data/stats.json"


def read(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def client_code() -> dict[str, str]:
    """Everything that runs in the browser or renders a page: src/ minus the route handlers
    (src/server, src/app/api), the proxy and the tests."""
    out = {}
    for path in sorted(SRC.rglob("*")):
        rel = path.relative_to(SRC).as_posix()
        if path.suffix not in {".ts", ".tsx"} or ".test." in path.name:
            continue
        if rel.startswith(("server/", "app/api/")) or rel == "proxy.ts":
            continue
        out[rel] = path.read_text(encoding="utf-8")
    return out


def test_stats_url_lives_in_the_site_config_and_names_the_data_branch():
    cfg = json.loads(read("data/site-config.json"))
    url = cfg["statsUrl"]
    assert url == RAW
    assert url.endswith(f"/{pub.BRANCH}/{pub.FILE_NAME}")
    for rel, text in client_code().items():  # the code reads it from the config
        assert "raw.githubusercontent.com" not in text, rel
    assert "STATS_URL" in read("lib/live.ts") and "siteConfig.statsUrl" in read("lib/site.ts")
    # the invite keeps the shape logic/textfilter.py parses
    assert re.fullmatch(r"[A-Za-z0-9-]+", cfg["inviteCode"])


def test_csp_allows_the_stats_fetch_and_nothing_new_besides():
    csp = json.loads(read("data/security.json"))["csp"]
    assert sitegen.csp_allows(csp["connect-src"], RAW)
    # 'self': the appeal form posts to /api/appeal
    assert csp["connect-src"] == ["'self'", "https://discord.com", "https://raw.githubusercontent.com"]
    # scripts and styles from this origin, plus the per-request nonce (src/proxy.ts) and nothing else
    assert csp["script-src"] == ["'self'", "'nonce-{nonce}'"]
    assert csp["style-src"] == ["'self'", "'nonce-{nonce}'"]


def test_the_page_renders_text_only():
    for rel, js in client_code().items():
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                    'setAttribute("style"', "cssText", "srcdoc", "javascript:", "createElement"):
            assert bad not in js, f"{bad} in {rel}"
        # only fixed attribute names are ever set from script
        assert set(re.findall(r'setAttribute\("([^"]+)"', js)) <= {"aria-current", "data-ink"}, rel
        if "dangerouslySetInnerHTML" in js:  # the JSON-LD data block, with "<" escaped
            assert rel == "app/page.tsx" and js.count("dangerouslySetInnerHTML") == 1, rel
            assert 'JSON.stringify(JSON_LD).replace(/</g, "\\\\u003c")' in js


def test_the_live_fetch_has_a_timeout_and_a_staleness_limit():
    stats, live = read("lib/stats.ts"), read("lib/live.ts")
    assert "AbortController" in live and "export const TIMEOUT_MS = 6000;" in stats
    assert "export const STALE_MS = 48 * HOUR_MS;" in stats
    assert 'credentials: "omit"' in live
    code = client_code()
    fetchers = {rel for rel, text in code.items() if "fetch(" in text}
    # one helper does every live request; the appeal form posts to its own origin
    assert fetchers == {"lib/live.ts", "components/AppealForm.tsx"}
    assert live.count("fetch(") == 1
    assert "(raw as Obj).v !== 1" in stats


def test_the_live_limits_match_the_schema():
    stats = read("lib/stats.ts")
    kinds = re.search(r"export const KINDS = \{([^}]*)\}", stats).group(1)
    assert tuple(re.findall(r"(\w+):", kinds)) == schema.KINDS
    for name in ("MAX_EVENTS", "MAX_HOUSES", "MAX_GAMES", "MAX_TITLE", "MAX_NAME", "MAX_COUNT"):
        assert f"export const {name} = {getattr(schema, name)};" in stats, name
    assert "list(r.events, MAX_EVENTS)" in stats and "list(r.houses, MAX_HOUSES)" in stats
    assert "list(r.games, MAX_GAMES)" in stats
    assert "text(e.title, MAX_TITLE)" in stats and "text(h.name, MAX_NAME)" in stats
    assert r"/\d{15,}/" in stats and "/[@<>]/" in stats  # the client's own ID and mention guard


def test_the_live_section_starts_hidden_in_the_source():
    """The rendered markup is checked in pages.test.tsx; here, that the hidden states are
    constant attributes the server always renders, lifted only by the client."""
    live = read("components/LiveSection.tsx")
    assert re.search(r'<section id="live" hidden ref=', live) or re.search(r'<section id="live" ref=\{[^}]+\} hidden', live)
    for block in ("live-events", "live-houses", "live-games", "live-tally"):
        assert re.search(rf'id="{block}"', live), block
    assert re.search(r'id="now"\s+hidden=\{!line\}', read("components/NowLine.tsx"))
    page = read("app/page.tsx")
    assert page.index('<section id="smp">') < page.index("<LiveSection />") < page.index('<section id="rules">')


def test_nothing_member_shaped_in_the_live_code():
    for rel, text in client_code().items():
        low = text.lower()
        for bad in ("avatar", "display_name", "username", "user_id", "cdn.discordapp.com"):
            if bad == "user_id" and rel in ("lib/appeal-form.ts", "components/AppealForm.tsx", "app/appeal/page.tsx"):
                continue  # the appeal form asks a banned member for their own ID
            assert bad not in low, f"{bad} in {rel}"


def test_live_styles_use_only_the_house_tokens():
    css = read("app/field-notebook.css")
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
    text = (SITE / "README.md").read_text(encoding="utf-8")
    assert "site-data" in text and "stats.json" in text and "publish_site_data.py" in text
