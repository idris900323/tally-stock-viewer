import re
from difflib import SequenceMatcher

from database import get_confirmed_mappings, get_folder_car_model


CODE_PATTERN = re.compile(r"(?<!\d)(\d{3,5}-\d{3,5}(?:-\d{3,5})?)(?!\d)")


def normalize_text(text):
    return re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).strip()


def extract_codes(text):
    return CODE_PATTERN.findall(str(text or ""))


def _candidate_stock_items(available_stock_items):
    candidates = []
    for item in available_stock_items or []:
        if isinstance(item, dict):
            stock_item = item.get("design") or item.get("raw") or item.get("stock_item_name") or item.get("name")
        else:
            stock_item = item
        if stock_item:
            candidates.append(str(stock_item))
    return candidates


def _similarity(left, right):
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()


def _image_text(image_record):
    return " ".join(
        str(part)
        for part in [
            image_record.get("car_folder", ""),
            image_record.get("filename", ""),
            image_record.get("filepath", ""),
        ]
        if part
    )


def _match_by_code(image_record, stock_items):
    image_codes = set(extract_codes(_image_text(image_record)))
    if not image_codes:
        return None, 0.0

    best_item = None
    best_score = 0.0
    for stock_item in stock_items:
        stock_codes = set(extract_codes(stock_item))
        if not stock_codes:
            continue
        if image_codes & stock_codes:
            return stock_item, 0.95

        image_code_text = next(iter(image_codes))
        stock_text = normalize_text(stock_item)
        if normalize_text(image_code_text) in stock_text and 0.85 > best_score:
            best_item = stock_item
            best_score = 0.85
    return best_item, best_score


def _match_by_car_folder(image_record, stock_items):
    folder_name = normalize_text(image_record.get("car_folder", ""))
    if not folder_name:
        return None, 0.0

    folder_mapping = get_folder_car_model(image_record.get("car_folder", ""))
    folder_hint = normalize_text(folder_mapping["car_model_name"]) if folder_mapping else folder_name

    best_item = None
    best_score = 0.0
    for stock_item in stock_items:
        stock_text = normalize_text(stock_item)
        if not stock_text:
            continue

        if folder_hint and (folder_hint in stock_text or stock_text in folder_hint):
            return stock_item, 0.7

        common_tokens = set(folder_name.split()) & set(stock_text.split())
        if common_tokens:
            similarity = _similarity(folder_name, stock_text)
            if similarity > best_score:
                best_item = stock_item
                best_score = max(0.6, similarity)

    return best_item, best_score


def _match_by_learned_patterns(image_record, stock_items):
    confirmed_mappings = get_confirmed_mappings()
    if not confirmed_mappings:
        return None, 0.0

    current_filename = normalize_text(image_record.get("filename", ""))
    if not current_filename:
        current_filename = normalize_text(_image_text(image_record))

    best_item = None
    best_score = 0.0

    for mapping in confirmed_mappings:
        learned_filename = normalize_text(mapping.get("filename", ""))
        learned_stock_item = mapping.get("stock_item_name")
        if not learned_stock_item:
            continue

        score = _similarity(current_filename, learned_filename)
        if score < 0.55:
            continue

        for stock_item in stock_items:
            if normalize_text(stock_item) != normalize_text(learned_stock_item):
                continue
            if score > best_score:
                best_item = stock_item
                best_score = min(0.75, round(score, 2))

    return best_item, best_score


def suggest_match(image_record, available_stock_items):
    stock_items = _candidate_stock_items(available_stock_items)
    if not stock_items:
        return None, 0.0

    exact_code_item, exact_code_score = _match_by_code(image_record, stock_items)
    if exact_code_item:
        return exact_code_item, exact_code_score

    folder_item, folder_score = _match_by_car_folder(image_record, stock_items)
    learned_item, learned_score = _match_by_learned_patterns(image_record, stock_items)

    ranked_candidates = [
        (folder_item, folder_score),
        (learned_item, learned_score),
    ]
    ranked_candidates = [candidate for candidate in ranked_candidates if candidate[0]]
    if ranked_candidates:
        ranked_candidates.sort(key=lambda candidate: candidate[1], reverse=True)
        return ranked_candidates[0]

    return None, 0.0
