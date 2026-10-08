"""Text filter for member text that Front Desk reposts.

Discord AutoMod never acts on bots, so anything a member types into a command and the bot
then posts (an /lfg note, a /gamenight note, a shoutout or Spotlight, a tournament name, a
partner description) skips the server's AutoMod rules. `check()` is the bot-side stand-in:

- invite links to other servers (discord.gg/, discord.com/invite/, discordapp.com/invite/,
  dsc.gg/), also when spaced out or written "discord(dot)gg"; the server's own invite code
  (read from site/config.js, or passed in) is allowed
- with `allow_links=False`: masked links `[text](url)`, raw URLs and bare domains, for short
  fields where a link makes no sense
- a short blocklist of slurs and severe sexual terms, matched as whole words after
  normalisation (case, accents, look-alike letters, leetspeak, stretched letters, letters
  split by dots/spaces), so "Scunthorpe", "classic", "assassin" or "cockpit" are fine

Whole-word matching is deliberate: substring matching blocks ordinary words, and the
server's own AutoMod keyword preset already covers chat. Words glued together without a
separator ("xxslurxx") are not caught; that's the trade for no false positives.

Pure: no Discord I/O. Cogs call `check()`/`check_all()` and reply with `message(reason)`,
never echoing the text back.
"""

from __future__ import annotations

import itertools
import logging
import re
import unicodedata
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

INVITE = "invite"
LINK = "link"
WORD = "word"

MESSAGES = {
    INVITE: "Invite links to other servers can't go in that. Take it out and try again.",
    LINK: "Links can't go in that. Take it out and try again.",
    WORD: "That has language we don't allow here. Please reword it and try again.",
}


def message(reason: str | None) -> str:
    """The friendly, ephemeral reply for a blocked reason. Never includes the text."""
    return MESSAGES.get(reason, MESSAGES[WORD])


# ---------------------------------------------------------------- the server's own invite
CONFIG_JS = Path(__file__).resolve().parents[2] / "site" / "config.js"
_CODE_IN_CONFIG = re.compile(r'INVITE_CODE\s*:\s*"([A-Za-z0-9-]+)"')


def load_own_invites(path: Path = CONFIG_JS) -> frozenset[str]:
    """The site's INVITE_CODE (lowercased), or nothing if the file is missing or unset."""
    try:
        found = _CODE_IN_CONFIG.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return frozenset()
    if not found or found.group(1).upper() == "REPLACE_ME":
        return frozenset()
    return frozenset({found.group(1).lower()})


OWN_INVITES = load_own_invites()

# ---------------------------------------------------------------- normalisation
# Letters from other scripts that look like Latin ones (NFKD leaves these alone).
LOOKALIKES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "з": "3", "і": "i", "ї": "i", "ј": "j", "к": "k",
    "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "ı": "i", "ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe",
})
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
MAX_SCAN = 4000  # longer input is cut here; every field the bot reposts is far shorter


def fold(text: str) -> str:
    """Lowercase, strip accents (NFKD), map look-alike letters, drop invisible characters."""
    text = unicodedata.normalize("NFKD", str(text or "")[:MAX_SCAN])
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Mn" or cat == "Cf":  # combining accents; zero-width and other format chars
            continue
        out.append(ch)
    return "".join(out).casefold().translate(LOOKALIKES)


# ---------------------------------------------------------------- invites and links
_DOT = r"(?:\.|\(\s*\.\s*\)|\[\s*\.\s*\]|\(\s*dot\s*\)|\[\s*dot\s*\]|\s+dot\s+)"
_SLASH = r"\s*/\s*"
INVITE_RE = re.compile(
    rf"(?:discord(?:app)?\s*{_DOT}\s*com{_SLASH}invite|discord\s*{_DOT}\s*gg|dsc\s*{_DOT}\s*gg)"
    rf"(?:{_SLASH}([a-z0-9-]*))?"
)
MASKED_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?\s*[a-z][a-z0-9+.-]*:")
URL_RE = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\s*\.)\S")
# Bare domains: only TLDs that are rarely a word, and only with no space around the dot,
# so "I'm in. To play" or "e.g." never count.
TLDS = "com|net|org|gg|io|xyz|ru|ly|tv|gift|gifts|biz|icu|su|pw|tk|ml|ga|cf|gq"
DOMAIN_RE = re.compile(rf"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.(?:{TLDS})\b")


def find_invite(folded: str, allow: Iterable[str]) -> bool:
    allowed = {a.lower() for a in allow}
    found = list(INVITE_RE.finditer(folded))
    if any(not m.group(1) or m.group(1) not in allowed for m in found):
        return True
    # "d1sc0rd.gg/..." only shows up once leetspeak is decoded; any extra match is foreign.
    return len(INVITE_RE.findall(folded.translate(LEET))) > len(found)


def find_link(folded: str) -> bool:
    return bool(MASKED_LINK_RE.search(folded) or URL_RE.search(folded) or DOMAIN_RE.search(folded))


# ---------------------------------------------------------------- blocked words
# Kept deliberately short: unambiguous slurs and severe sexual terms only, in base form.
# Plurals (-s/-z) are handled by the matcher. Mild swearing is not the bot's job.
BLOCKED_WORDS = frozenset({
    # racial and ethnic slurs
    "nigger", "nigga", "nigguh", "niglet", "kike", "chink", "gook", "spic", "wetback", "beaner",
    "raghead", "towelhead", "paki", "coon", "jigaboo", "porchmonkey",
    # anti-LGBT slurs
    "faggot", "fag", "fagot", "tranny", "shemale",
    # ableist slur
    "retard", "retarded",
    # severe sexual terms
    "cunt", "whore", "slut", "rape", "raped", "rapist", "raping", "porn", "porno", "pornhub",
    "hentai", "blowjob", "handjob", "cumshot", "creampie", "gangbang", "lolicon", "shotacon",
    "pedo", "pedophile", "paedophile", "cock", "pussy", "dildo",
})

# Real words close to a blocked one, never blocked as typed.
ALLOWED_WORDS = frozenset({
    "niger",  # the country
    "spick",  # "spick and span"
})

MIN_JOIN = 3  # single letters split by separators ("c u n t") are joined from this many


def _runs(word: str) -> list[tuple[str, int]]:
    return [(ch, len(list(group))) for ch, group in itertools.groupby(word)]


def stretched_forms(word: str) -> set[str]:
    """The word with every run of a repeated letter cut to 1 or 2 ("niiiigger" -> "nigger")."""
    runs = _runs(word)
    options = [(ch,) if n == 1 else (ch, ch * 2) for ch, n in runs]
    if sum(len(o) > 1 for o in options) > 8:  # cap the 2^n blow-up on silly input
        return {"".join(ch * min(n, 2) for ch, n in runs), "".join(ch for ch, _ in runs)}
    return {"".join(p) for p in itertools.product(*options)}


def word_is_blocked(word: str) -> bool:
    if not word or word in ALLOWED_WORDS:
        return False
    for form in stretched_forms(word):  # a set: the result must not depend on its order
        if form in BLOCKED_WORDS:
            return True
        if len(form) > 3 and form[-1] in "sz" and form[:-1] in BLOCKED_WORDS:
            return True
        if len(form) > 4 and form.endswith("es") and form[:-2] in BLOCKED_WORDS:
            return True
    return False


def candidate_words(folded: str) -> set[str]:
    """Every word to test, from two splits of the leetspeak-decoded text:
    - by whitespace, punctuation inside a word removed ("n.i.g.g.e.r", "c-u-n-t" stay whole)
    - by anything that isn't a letter ("slur,word" splits)
    plus runs of single letters joined up ("c u n t", "c . u . n . t")."""
    decoded = folded.translate(LEET)
    words: set[str] = set()
    for chunk in decoded.split():
        joined = re.sub(r"[^a-z]", "", chunk)
        if joined:
            words.add(joined)
    letters = re.findall(r"[a-z]+", decoded)
    words.update(letters)
    run: list[str] = []
    for token in letters + [""]:  # the sentinel flushes the last run
        if len(token) == 1:
            run.append(token)
            continue
        if len(run) >= MIN_JOIN:
            run = run[:40]
            for i in range(len(run)):
                for j in range(i + MIN_JOIN, len(run) + 1):
                    words.add("".join(run[i:j]))
        run = []
    return words


def find_word(folded: str) -> bool:
    return any(word_is_blocked(w) for w in candidate_words(folded))


# ---------------------------------------------------------------- API
def check(text: str | None, *, allow_links: bool = True,
          invite_allowlist: Iterable[str] | None = None) -> tuple[bool, str | None]:
    """(True, None) if `text` can be reposted, else (False, reason) with reason one of
    INVITE, LINK, WORD. `allow_links=False` also refuses masked links, URLs and bare
    domains (the server's own invite counts as a link there). `invite_allowlist`
    defaults to the server's own invite code."""
    if not text:
        return True, None
    folded = fold(text)
    allow = OWN_INVITES if invite_allowlist is None else invite_allowlist
    if find_invite(folded, allow):
        return False, INVITE
    if not allow_links and find_link(folded):
        return False, LINK
    if find_word(folded):
        return False, WORD
    return True, None


def check_all(texts: Iterable[str | None], **kwargs) -> tuple[bool, str | None]:
    """check() each text in order; the first refusal wins."""
    for text in texts:
        ok, reason = check(text, **kwargs)
        if not ok:
            return ok, reason
    return True, None


def screen(where: str, user_id: int, texts: Iterable[str | None], **kwargs) -> str | None:
    """For cogs: None if every text can be reposted, else the friendly reply to send
    ephemerally. A block is logged at INFO with the user id and reason, never the text."""
    ok, reason = check_all(texts, **kwargs)
    if ok:
        return None
    log.info("textfilter: blocked %s text from user %s (%s)", where, user_id, reason)
    return message(reason)
