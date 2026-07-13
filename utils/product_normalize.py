import re

from utils.normalize import normalize_text


# Ordered (label, regex) pairs for generic product types that repeat across
# many car-specific stock item names. Regexes tolerate spacing/apostrophe
# variations seen in the stock item catalog (e.g. "7D", "7'D", "7 D").
TYPE_PATTERNS = [
    ("FOOT MAT", re.compile(r"\bFOOT\s*MAT\b", re.IGNORECASE)),
    ("7D MAT", re.compile(r"\b7\s*'?\s*D\s*MAT\b", re.IGNORECASE)),
    ("SPLIT MAT", re.compile(r"\bSPLIT\s*MAT\b", re.IGNORECASE)),
    ("9X MAT", re.compile(r"\b9\s*X\s*MAT\b", re.IGNORECASE)),
    ("GRASS MAT", re.compile(r"\bGRASS\s*MAT\b", re.IGNORECASE)),
    ("DICKY MAT", re.compile(r"\bDICKY\s*MAT\b", re.IGNORECASE)),
    ("NOODLE MAT", re.compile(r"\bNOODLE\s*MAT\b", re.IGNORECASE)),
    ("MATERIAL", re.compile(r"\bMATERIAL\b", re.IGNORECASE)),
    ("CURTAINS", re.compile(r"\bCURTAINS?\b", re.IGNORECASE)),
    ("NECK REST", re.compile(r"\bNECK\s*REST\b", re.IGNORECASE)),
    ("LUMBER SUPPORT", re.compile(r"\bLUMBER\s*SUPPORT\b", re.IGNORECASE)),
    ("CUSHION", re.compile(r"\bCUSHIONS?\b", re.IGNORECASE)),
    ("SUN SHADE", re.compile(r"\bSUN\s*SHADE\b", re.IGNORECASE)),
]


# Spelling variants seen in the stock item catalog, mapped to one canonical
# color name. Reversed/mixed color order in the source data (TAN-BLACK vs
# BLACK-TAN) is handled by extract_type_and_color() sorting the result.
COLOR_MAP = {
    "BAIGE": "BEIGE",
    "BEIGE": "BEIGE",
    "BLK": "BLACK",
    "BALCK": "BLACK",
    "BLACK": "BLACK",
    "GREY": "GREY",
    "GRAY": "GREY",
    "D.GREY": "GREY",
    "I-GREY": "GREY",
    "TAN": "TAN",
    "COCO": "COCO",
    "RED": "RED",
    "BLUE": "BLUE",
    "BROWN": "BROWN",
    "IVORY": "IVORY",
    "SILVER": "SILVER",
    "PEARL": "PEARL",
    "ORANGE": "ORANGE",
    "WHITE": "WHITE",
    "CHROME": "CHROME",
}


def _build_combined_color_pattern():
    # One alternation instead of one pattern per variant, scanned with
    # finditer() for non-overlapping matches. This is what makes counting
    # correct: some variants overlap as substrings of another (e.g. "GREY"
    # inside "D.GREY", since "." is a non-word character so \b matches right
    # before the "G"). A per-variant .search() only needs presence so the
    # overlap never mattered before, but counted independently it would
    # double-count a single "D.GREY" mention as two GREY occurrences.
    # finditer() on one combined pattern consumes "D.GREY" as a single match
    # and continues scanning after it, so the embedded "GREY" is never
    # matched a second time. Longest-first keeps matching predictable if
    # more overlapping variants are ever added.
    variants = sorted(COLOR_MAP.keys(), key=len, reverse=True)
    alternation = "|".join(re.escape(variant) for variant in variants)
    return re.compile(r"\b(?:" + alternation + r")\b", re.IGNORECASE)


COLOR_PATTERN = _build_combined_color_pattern()


def extract_colors(normalized_name):
    """Return canonical colors for an ALREADY-normalized (normalize_text'd)
    name, one entry per occurrence -- e.g. "BLACK+BLACK" returns
    ["BLACK", "BLACK"], not a deduped ["BLACK"]. A doubled same-color
    mention is a distinct product in this catalog, not a duplicate mention
    of the same one, so occurrence count must be preserved rather than
    collapsed into a presence set.
    """
    return [COLOR_MAP[match.group(0)] for match in COLOR_PATTERN.finditer(normalized_name)]


def extract_type_and_color(name):
    """Return (type_label_or_None, sorted_color_list) for a stock item name.

    The color list is repeated per occurrence (see extract_colors), then
    sorted -- e.g. one BLACK + one TAN -> ["BLACK", "TAN"]; two BLACK ->
    ["BLACK", "BLACK"]. Callers joining this with "-" therefore get
    "BLACK-TAN" vs "BLACK-BLACK" as distinct category keys.

    Word-boundary regex matching against TYPE_PATTERNS/COLOR_MAP means naming
    inconsistencies (spacing, apostrophes, misspellings, reversed color
    order) still resolve to the same canonical type/color pair.
    """
    normalized = normalize_text(name)

    type_label = None
    for label, pattern in TYPE_PATTERNS:
        if pattern.search(normalized):
            type_label = label
            break

    return type_label, sorted(extract_colors(normalized))
