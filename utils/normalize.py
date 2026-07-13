import re


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().upper()


def normalize_lookup_key(text):
    """Collapse-whitespace + strip + lowercase key for dict/DB lookups.

    Single source of truth so a stock item name with irregular internal
    whitespace (common in Tally exports) always maps to the same key on
    both the app-side lookup builder and the database query side.
    """
    return normalize_text(text).lower()


def strip_shelf_code_for_display(name):
    """Strip a trailing Tally shelf-location code (e.g. "* M-20",
    "****H-4****", "***G-4)", "**E-1,2, G-8") from a car name for DISPLAY
    only. Truncates at the first "*", then trims trailing separator
    punctuation (whitespace, ".", "-", ",") and a dangling unbalanced
    trailing ")" left over from the cut.

    Callers must keep using the original, unmodified `name` for anything
    sent to the backend (?car= query params, matching against
    car_master.json/main_hierarchy.json) -- this is presentation-only.
    """
    text = str(name or "")
    idx = text.find("*")
    if idx == -1:
        return text.strip()

    before = text[:idx]
    before = re.sub(r"[\s.\-,]+$", "", before)
    while before.endswith(")") and before.count("(") < before.count(")"):
        before = before[:-1].rstrip()

    cleaned = before.strip()
    return cleaned or text.strip()


def extract_car_base_name(full_name):
    """Extract the searchable base car name from a noisy dropdown label."""
    value = str(full_name or "").strip()
    if not value:
        return ""

    value = re.split(r"\*\*", value, maxsplit=1)[0]
    value = re.split(r"\bFRONT\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.split(r"\bREAR\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.split(r"\bSIDE\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = value.split("(")[0]
    value = value.split("[")[0]
    value = value.split("{")[0]
    value = re.sub(r"\s+\*.*$", "", value).strip()
    value = re.sub(r"\s+-\s+.*$", "", value).strip()
    value = re.sub(r"\s+\.{3}.*$", "", value).strip()

    normalized = normalize_text(value)
    if not normalized:
        return ""

    tokens = normalized.split()
    filtered = []
    for token in tokens:
        if token in {"FRONT", "REAR", "SIDE", "LEFT", "RIGHT", "OLD", "NEW"}:
            break
        if token.startswith("M-") or token.startswith("V-"):
            break
        filtered.append(token)

    return " ".join(filtered).strip() or normalized
