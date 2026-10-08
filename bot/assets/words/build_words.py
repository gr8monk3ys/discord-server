"""Rebuild answers.txt and allowed.txt for Daily Word (cogs/wordgame.py). Offline: point it at
an unpacked SCOWL release and the ENABLE list (see SOURCES.md for where to get them).

    python build_words.py SCOWL_DIR ENABLE1_TXT

answers: 5-letter a-z words in SCOWL sizes 10-35 (common English), minus inflected forms
(plurals, -s verbs, -ed past tenses, -ier/-er comparatives whose base is a word), slang
contractions, obscure scientific plurals and the blocklist below (slurs, sexual terms, bodily
fluids, cruelty). allowed: every 5-letter word in SCOWL up to size 70 plus ENABLE, plus answers.
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIVE = re.compile(r"^[a-z]{5}$")

# Never a daily answer. Still accepted as guesses: a guess is only ever shown to its author.
BLOCK = set("""
bitch booby booty bosom buxom fagot feces enema horny kinky penis pussy semen sperm whore sissy
lynch moron queer pansy gayer spank nappy thong urine vomit uteri ovary bowel mucus chink prick
spunk lusty bawdy harem slave fetus leper opium psych chick
coyer apter laxer rawer wryer shyer slier weest icier odder
unman unsay unset infix octal halon fiche genii radii cacti oases maria peter roman datum
lemme gonna wanna kinda sorta dunno gimme tepee karat pokey letup sunup
aping awing cuing ruing eking axing suing acing
redid undid upped tatty lorry tonne multi inter dully
""".split())


def scowl(final: Path, sizes) -> set[str]:
    out: set[str] = set()
    for size in sizes:
        for kind in ("english-words", "american-words"):
            path = final / f"{kind}.{size}"
            if path.exists():
                out.update(path.read_text(encoding="latin-1").split())
    return out


def inflected(word: str, vocab: set[str]) -> bool:
    if word.endswith("s") and not word.endswith("ss") and word[:-1] in vocab:
        return True  # cats, makes
    if word.endswith("es") and len(word) >= 5 and word[:-2] in vocab:
        return True  # boxes
    if word.endswith("ies") and word[:-3] + "y" in vocab:
        return True  # flies
    if word.endswith("ed") and (word[:-1] in vocab or word[:-2] in vocab):
        return True  # baked, added
    if word.endswith("ied") and word[:-3] + "y" in vocab:
        return True  # cried
    if word.endswith("er") and word[:-1] in vocab and word[:-2] + "e" == word[:-1]:
        return True  # safer
    return False


def build(scowl_dir: Path, enable: Path) -> tuple[list[str], list[str]]:
    final = scowl_dir / "final"
    common = scowl(final, (10, 20, 35))
    vocab = common | scowl(final, (40, 50, 55, 60, 70)) | set(enable.read_text().split())
    answers = sorted(w for w in common if FIVE.match(w) and not inflected(w, vocab) and w not in BLOCK)
    allowed = sorted({w for w in vocab if FIVE.match(w)} | set(answers))
    return answers, allowed


def main() -> None:
    answers, allowed = build(Path(sys.argv[1]), Path(sys.argv[2]))
    (HERE / "answers.txt").write_text("\n".join(answers) + "\n", encoding="utf-8")
    (HERE / "allowed.txt").write_text("\n".join(allowed) + "\n", encoding="utf-8")
    print(f"{len(answers)} answers, {len(allowed)} allowed")


if __name__ == "__main__":
    main()
