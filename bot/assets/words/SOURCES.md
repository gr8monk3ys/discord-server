# Daily Word lists

`answers.txt` (1,883 words, one daily answer each, cycling in a fixed shuffled order) and
`allowed.txt` (8,843 words accepted as guesses) are built by `build_words.py` from two word
lists. Neither is derived from any other word game's lists.

## SCOWL (Spell Checker Oriented Word Lists), release 2020.12.07

- Source: http://wordlist.aspell.net/ (download: `scowl-2020.12.07.tar.gz` on SourceForge,
  project `wordlist`)
- Used: `final/english-words.*` and `final/american-words.*`. Sizes 10, 20 and 35 (common
  words) give the answer candidates; sizes up to 70 add to the allowed guesses.
- Licence: permissive. The notice, as the licence requires:

  > Copyright 2000-2018 by Kevin Atkinson
  >
  > Permission to use, copy, modify, distribute and sell these word lists, the associated
  > scripts, the output created from the scripts, and its documentation for any purpose is
  > hereby granted without fee, provided that the above copyright notice appears in all copies
  > and that both that copyright notice and this permission notice appear in supporting
  > documentation. Kevin Atkinson makes no representations about the suitability of this array
  > for any purpose. It is provided "as is" without express or implied warranty.

  SCOWL's lower size levels draw on the public-domain Moby Words II and Brian Kelk's UK English
  Wordlist with Frequency Classification (also public domain); see SCOWL's `Copyright` file.

## ENABLE (Enhanced North American Benchmark Lexicon)

- Source: `enable1.txt`, e.g. https://raw.githubusercontent.com/dolph/dictionary/master/enable1.txt
- Used: the 5-letter words, for allowed guesses only.
- Licence: public domain.

## Filtering

Only lowercase a-z words of exactly five letters. Answers drop inflected forms (plurals, -s
verbs, -ed past tenses, comparatives whose base is also a word), slang contractions, obscure
plurals and a blocklist of slurs, sexual and bodily terms (the `BLOCK` set in
`build_words.py`). Blocked words can still be guessed; guesses are only shown to the player.

Rebuild: `python build_words.py path/to/scowl-2020.12.07 path/to/enable1.txt`
