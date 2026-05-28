"""
sort_title.py
-------------
Generates a sort_title from a song title for A-Z indexing.

Rules (in order):
  1. Strip leading brackets/parens: "(scene change)" -> "SCENE CHANGE"
  2. Strip diacritics:              "Araçá Azul"     -> "ARACA AZUL"
  3. Strip punctuation noise:       apostrophes, commas, question marks, periods
  4. Article handling:
       "The X"  -> "X"              (dropped)
       "An X"   -> "X"              (dropped)
       "A X"    -> "X"   EXCEPT
       "A Silver Thread" -> "SILVER THREAD, A"  (the one AACR2 move-to-end case)
       "Le X"   -> "X"              (dropped)
       "La X"   -> "X"              (dropped)
       "Les X"  -> "X"              (dropped)
       "L'X"    -> "X"              (dropped)
       "Un X"   -> "X"              (dropped)
       "Une X"  -> "X"              (dropped)
  5. Uppercase result
  6. Numeral overrides (hardcoded — only 3 exist):
       "14 Auspicious Dreams" -> "FOURTEEN AUSPICIOUS DREAMS"
       "23 Minutes in Brussels" -> "TWENTY THREE MINUTES IN BRUSSELS"
       "1995" -> "NINETEEN-NINETY-FIVE"

Known fixed errors from source data (applied at import time):
  "Le Chat Noir" sort should be "CHAT NOIR" (La titles drop article, Le should too)
  "Requiem For a Mouse" sort had typo "REQUIUM" — fixed to "REQUIEM"
  "The Past Is Our Plaything" was truncated — restored to full
  "The World's Strongest Man" was truncated — restored to full
  "Along The Santa Fe Trail" had wrong sort — corrected
  "The Ghosts of Girton" had article kept — corrected
"""

import re
import unicodedata

# Hardcoded overrides: title (lowercase, stripped) -> sort_title
# Used for the handful of cases no algorithm can derive correctly
OVERRIDES = {
    "14 auspicious dreams":             "FOURTEEN AUSPICIOUS DREAMS",
    "23 minutes in brussels":           "TWENTY THREE MINUTES IN BRUSSELS",
    "1995":                             "NINETEEN-NINETY-FIVE",
    # Source data errors fixed:
    "le chat noir":                     "CHAT NOIR",
    "the past is our plaything":        "PAST IS OUR PLAYTHING",
    "the world's strongest man":        "WORLD'S STRONGEST MAN",
    "along the santa fe trail":         "ALONG THE SANTA FE TRAIL",  # no special sort
}

# Single AACR2 move-to-end case
A_MOVE_TO_END = {"a silver thread"}

# Articles to strip, longest-match first to avoid "les" matching before "le"
_STRIP_ARTICLES = [
    "les ", "le ", "la ", "l'", "une ", "un ",   # French
    "the ", "an ", "a ",                           # English (a last — shortest)
]


def strip_diacritics(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def make_sort_title(title: str) -> str:
    """Return the sort_title for a given song title."""
    key = title.strip().lower()

    if key in OVERRIDES:
        return OVERRIDES[key]

    t = title.strip()

    # 1. Strip leading brackets/parens
    t = re.sub(r"^\s*[\(\[]\s*", "", t)
    t = re.sub(r"\s*[\)\]]\s*$", "", t).strip()

    # 2. Diacritics
    t = strip_diacritics(t)

    # 3. AACR2 move-to-end for "A <title>"
    if t.lower() in A_MOVE_TO_END:
        body = re.sub(r"^[Aa]\s+", "", t)
        return (strip_noise(body) + ", A").upper()

    # 4. Strip leading articles
    t_lower = t.lower()
    for article in _STRIP_ARTICLES:
        if t_lower.startswith(article):
            t = t[len(article):]
            break

    # 5. Strip noise punctuation for sort purposes
    t = strip_noise(t)

    return t.upper().strip()


def strip_noise(t: str) -> str:
    """Remove punctuation that shouldn't affect sort order."""
    # Remove apostrophes, question marks, trailing periods
    t = t.replace("'", "").replace("'", "").replace("?", "").replace("!", "").replace(",", "")
    # Collapse multiple spaces
    t = re.sub(r"\s+", " ", t)
    return t.strip()


if __name__ == "__main__":
    # Quick self-test
    cases = [
        ("The Aftertime",           "AFTERTIME"),
        ("A Silver Thread",         "SILVER THREAD, A"),
        ("A Gift",                  "GIFT"),
        ("A Song for You",          "SONG FOR YOU"),
        ("La Poupee Qui Fait Non",  "POUPEE QUI FAIT NON"),
        ("Le Chat Noir",            "CHAT NOIR"),
        ("Les Fleurs",              "FLEURS"),
        ("L'Oiseau",                "OISEAU"),
        ("Araçá Azul",              "ARACA AZUL"),
        ("(scene change)",          "SCENE CHANGE"),
        ("14 Auspicious Dreams",    "FOURTEEN AUSPICIOUS DREAMS"),
        ("23 Minutes in Brussels",  "TWENTY THREE MINUTES IN BRUSSELS"),
        ("1995",                    "NINETEEN-NINETY-FIVE"),
        ("The Past Is Our Plaything", "PAST IS OUR PLAYTHING"),
        ("Blue Thunder",            "BLUE THUNDER"),
        ("Don't Think Twice, It's Alright", "DONT THINK TWICE ITS ALRIGHT"),
    ]
    all_pass = True
    for title, expected in cases:
        got = make_sort_title(title)
        status = "✓" if got == expected else "✗"
        if got != expected:
            all_pass = False
        print(f"  {status}  {title!r:45} -> {got!r}  (expected {expected!r})")
    print(f"\n{'All tests passed' if all_pass else 'FAILURES above'}")
