"""
mb_titlecase.py
---------------
MusicBrainz English title style checker and converter.

Rules implemented:
  1. Always capitalise first and last word of the title
  2. Always capitalise first and last word of each section after
     major punctuation (: ? ! — " ')
  3. Lowercase between those positions:
     - Articles: a, an, the
     - Coordinating conjunctions: and, but, or, nor, for, yet, so
     - Short prepositions (≤3 letters): as, at, by, for, in, of,
       on, to, but, cum, mid, off, per, qua, re, up, via
  4. Contractions: o' (for "of"), 'n' / n' (for "and") stay lowercase
  5. Parenthetical ETI: capitalised as a new title

Known limitations:
  - Cannot reliably detect phrasal verbs (e.g. "Plug In Baby",
    "Shine On You Crazy Diamond") — these are flagged as warnings
  - Non-English words in titles are not handled
  - Intentional stylisation (e.g. all-caps, all-lowercase) is not detected

Usage:
    from mb_titlecase import mb_title, check_title, check_csv

    # Convert a title
    mb_title("the flowers of romance")
    # -> "The Flowers of Romance"

    # Check an existing title — returns (is_correct, suggested, warnings)
    check_title("Don't Let Our Youth go To Waste")
    # -> (False, "Don't Let Our Youth Go to Waste", [])
"""

import csv
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Word lists
# ---------------------------------------------------------------------------

# Always lowercase unless first/last/after major punctuation
LOWERCASE_WORDS = {
    # Articles
    'a', 'an', 'the',
    # Coordinating conjunctions
    'and', 'but', 'or', 'nor', 'for', 'yet', 'so',
    # Short prepositions (≤3 letters)
    'as', 'at', 'by', 'in', 'of', 'on', 'to',
    'cum', 'mid', 'off', 'per', 'qua', 're', 'up', 'via',
    # Contractions/slang that stand for lowercase words
    "o'",       # of
    "n'", "'n'", "'n",  # and
}

# Words in this set that appear mid-title may be adverbs or phrasal verb
# particles — flag for human review rather than auto-correcting
AMBIGUOUS_PREPOSITIONS = {'in', 'on', 'off', 'up', 'out', 'down', 'by', 'to'}

# Titles starting with these are likely non-English — flag for review,
# don't auto-correct (French sentence case rules apply instead)
POSSIBLE_NON_ENGLISH_STARTS = {
    'le', 'la', 'les', "l'", 'un', 'une',   # French
    'el', 'los', 'las',                       # Spanish
    'il', 'gli',                              # Italian
    'der', 'die', 'das', 'ein', 'eine',      # German
}

# Major punctuation that resets capitalisation
MAJOR_PUNCT_RE = re.compile(r'([?!—]|(?<!\w):(?!\d))')

# Matches a bracketed/parenthetical suffix: (something)
PAREN_RE = re.compile(r'^(.*?)\s*(\([^)]+\))\s*$')


# ---------------------------------------------------------------------------
# Core title casing
# ---------------------------------------------------------------------------

def _capitalise_word(word: str) -> str:
    """Capitalise first letter, preserve rest (handles apostrophes, hyphens)."""
    if not word:
        return word
    # Hyphenated words: capitalise each part
    if '-' in word:
        return '-'.join(_capitalise_word(part) for part in word.split('-'))
    # Don't touch all-caps words (likely acronyms: LP, UK, BBC)
    if word.isupper() and len(word) > 1:
        return word
    return word[0].upper() + word[1:]


def _is_lowercase_word(word: str) -> bool:
    """True if this word should be lowercase in mid-title position."""
    return word.lower().rstrip("',") in LOWERCASE_WORDS


def _apply_title_case(segment: str, force_first: bool = True) -> str:
    """
    Apply MusicBrainz title case to a single segment (no major punctuation).
    force_first: capitalise the first word regardless of word type.
    """
    words = segment.split(' ')
    if not words:
        return segment

    result = []
    for i, word in enumerate(words):
        is_first = (i == 0 and force_first)
        is_last  = (i == len(words) - 1)
        bare     = word.lower().rstrip("',")

        if is_first or is_last:
            result.append(_capitalise_word(word))
        elif _is_lowercase_word(word):
            result.append(word.lower())
        else:
            result.append(_capitalise_word(word))

    return ' '.join(result)


def mb_title(title: str) -> str:
    """
    Convert a title to MusicBrainz English title case.
    Returns the corrected string.
    """
    if not title or not title.strip():
        return title

    title = title.strip()

    # Handle parenthetical ETI: apply title case to it separately
    m = PAREN_RE.match(title)
    if m:
        main  = m.group(1).strip()
        paren = m.group(2)
        inner = paren[1:-1]  # strip parens
        # ETI gets title case applied as a new title
        cased_inner = _apply_title_case(inner, force_first=True)
        cased_main  = _apply_title_case_with_major_punct(main)
        return f"{cased_main} ({cased_inner})"

    return _apply_title_case_with_major_punct(title)


def _apply_title_case_with_major_punct(title: str) -> str:
    """Split on major punctuation, apply title case to each segment."""
    # Split keeping the delimiters
    parts  = MAJOR_PUNCT_RE.split(title)
    result = []
    force  = True  # first segment always forces capitalisation

    for part in parts:
        if MAJOR_PUNCT_RE.match(part):
            result.append(part)
            force = True  # next segment starts a new title
        else:
            result.append(_apply_title_case(part, force_first=force))
            force = False

    return ''.join(result)


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

def check_title(title: str) -> tuple[bool, str, list[str]]:
    """
    Check whether a title conforms to MusicBrainz English title style.

    Returns:
        (is_correct, suggested_title, warnings)

    warnings contains notes about cases that require human judgement
    (e.g. possible phrasal verbs).
    """
    if not title or not title.strip():
        return True, title, []

    suggested = mb_title(title)
    warnings  = []

    # Flag mid-title ambiguous prepositions for human review
    words = title.split()
    for i, word in enumerate(words[1:-1], start=1):  # skip first and last
        bare = word.lower().rstrip("',")
        if bare in AMBIGUOUS_PREPOSITIONS:
            # If we've capitalised it, it might be a phrasal verb
            if word[0].isupper():
                warnings.append(
                    f"'{word}' is capitalised mid-title — check if it's a "
                    f"phrasal verb particle (keep caps) or preposition (lowercase)"
                )

    # Flag possible non-English titles — don't auto-correct these
    first_word = title.split()[0].lower().rstrip("'") if title.split() else ''
    if first_word in POSSIBLE_NON_ENGLISH_STARTS:
        warnings.append(
            f"Title starts with '{title.split()[0]}' — may be non-English "
            f"(French/Spanish/etc. use sentence case). Review before applying suggestion."
        )

    is_correct = (title == suggested)
    return is_correct, suggested, warnings


# ---------------------------------------------------------------------------
# Batch CSV checker
# ---------------------------------------------------------------------------

def check_csv(csv_path: str, title_col: str = 'title',
              slug_col: str = 'slug') -> None:
    """
    Check all titles in a CSV file and report issues.
    Useful for auditing tracks.csv or release sheets.
    """
    path = Path(csv_path)
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("Empty file.")
        return

    if title_col not in rows[0]:
        print(f"Column '{title_col}' not found. Available: {list(rows[0].keys())}")
        return

    issues  = []
    correct = 0

    for row in rows:
        title = row.get(title_col, '').strip()
        if not title:
            continue
        ok, suggested, warnings = check_title(title)
        if ok and not warnings:
            correct += 1
        else:
            slug = row.get(slug_col, '')
            issues.append({
                'slug':      slug,
                'title':     title,
                'suggested': suggested,
                'warnings':  warnings,
                'changed':   title != suggested,
            })

    total = correct + len(issues)
    print(f"\n{path.name}: {total} titles checked")
    print(f"  Correct:  {correct}")
    print(f"  Issues:   {len(issues)}")

    if issues:
        print(f"\n{'─'*70}")
        changed  = [i for i in issues if i['changed']]
        warnings = [i for i in issues if not i['changed'] and i['warnings']]

        if changed:
            print(f"\nWrong capitalisation ({len(changed)}):")
            for i in changed:
                print(f"  {i['title']!r}")
                print(f"  → {i['suggested']!r}")
                if i['slug']:
                    print(f"    [{i['slug']}]")

        if warnings:
            print(f"\nNeeds human review ({len(warnings)}):")
            for i in warnings:
                print(f"  {i['title']!r}")
                for w in i['warnings']:
                    print(f"    ⚠ {w}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            'Check/convert titles to MusicBrainz English title style.\n\n'
            'Examples:\n'
            '  python mb_titlecase.py tracks.csv\n'
            '  python mb_titlecase.py tracks.csv --col Title --slug slug\n'
            '  python mb_titlecase.py --title "Walk On The Wild Side"\n'
            '  python mb_titlecase.py --convert "walk on the wild side"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('file', nargs='?',
                        help='CSV file to check')
    parser.add_argument('--col',     default='title',
                        help='Title column name (default: title)')
    parser.add_argument('--slug',    default='slug',
                        help='Slug column name (default: slug)')
    parser.add_argument('--title',
                        help='Check a single title')
    parser.add_argument('--convert',
                        help='Convert a single title to MB style')

    args = parser.parse_args()

    if args.title:
        ok, suggested, warnings = check_title(args.title)
        if ok:
            print(f"✓  {args.title!r}")
        else:
            print(f"✗  {args.title!r}")
            print(f"→  {suggested!r}")
        for w in warnings:
            print(f"⚠  {w}")

    elif args.convert:
        print(mb_title(args.convert))

    elif args.file:
        check_csv(args.file, title_col=args.col, slug_col=args.slug)

    else:
        # Self-test
        tests = [
            # (input, expected)
            ("the flowers of romance",
             "The Flowers of Romance"),
            ("don't let our youth go to waste",
             "Don't Let Our Youth Go to Waste"),
            ("listen, the snow is falling",
             "Listen, the Snow Is Falling"),
            ("here she comes now",
             "Here She Comes Now"),
            ("walk on the wild side",
             "Walk on the Wild Side"),
            ("i shall be released",
             "I Shall Be Released"),
            ("blue thunder",
             "Blue Thunder"),
            ("what goes on",
             "What Goes On"),
            ("satellite of love",
             "Satellite of Love"),
            ("fourth of july",
             "Fourth of July"),
            ("don't fear the reaper",
             "Don't Fear the Reaper"),
            ("fly me to the moon (in other words)",
             "Fly Me to the Moon (In Other Words)"),
            ("songs of love and hate",
             "Songs of Love and Hate"),
            ("bring your daughter to the slaughter",
             "Bring Your Daughter to the Slaughter"),
            ("a song for you",
             "A Song for You"),
            ("this is our music",
             "This Is Our Music"),
            ("on fire",
             "On Fire"),
        ]

        print("Self-test:")
        passed = 0
        for title, expected in tests:
            got = mb_title(title)
            ok  = got == expected
            if ok:
                passed += 1
            print(f"  {'✓' if ok else '✗'}  {title!r}")
            if not ok:
                print(f"       got:      {got!r}")
                print(f"       expected: {expected!r}")

        print(f"\n{passed}/{len(tests)} passed")
