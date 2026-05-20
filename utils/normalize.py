import re


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().upper()


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
