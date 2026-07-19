from flask import Blueprint, jsonify, request

from utils.normalize import extract_car_base_name, normalize_text, strip_shelf_code_for_display


search_bp = Blueprint("search", __name__)
_dependencies = {}


def set_search_dependencies(**dependencies):
    _dependencies.update(dependencies)


def _get_dependency(name):
    dependency = _dependencies.get(name)
    if dependency is None:
        raise RuntimeError(f"Missing search dependency: {name}")
    return dependency


def _paginate_matches(values, query, page, per_page):
    normalized_query = normalize_text(query)
    if not normalized_query:
        matches = list(values)
    else:
        matches = [value for value in values if normalized_query in normalize_text(value)]
    start = (page - 1) * per_page
    end = start + per_page
    page_values = matches[start:end]
    has_more = end < len(matches)
    return page_values, has_more


def _anchor_paginate(values, anchor_id, page, per_page):
    """Page through `values` (in their existing, already-alphabetical order)
    positioned around `anchor_id`, instead of filtering to name-matches.

    Used so reopening the car dropdown with a car already selected can show
    real alphabetical neighbors -- not a subset of similarly-named cars --
    scrolled to that car's position. The window is centered on the anchor
    (not started at it) so a freshly-opened dropdown already has genuine
    unrelated-name neighbors visible above the selection, not just below it.

    `page` continues from that same window with ordinary (page-1)*per_page
    arithmetic, so page 2, 3, ... page deeper into the FULL list exactly the
    way scrolling normally would from page 1 -- just offset by a fixed
    starting point instead of starting at index 0. This is fully stateless:
    the same window start is recomputed from `anchor_id` on every call.

    Returns None if `anchor_id` isn't found in `values` (e.g. a car deleted
    from Tally since it was selected), so the caller can fall back to
    normal unfiltered pagination instead of silently returning an unrelated
    slice.
    """
    normalized_target = normalize_text(anchor_id)
    anchor_index = None
    for index, value in enumerate(values):
        if normalize_text(value) == normalized_target:
            anchor_index = index
            break
    if anchor_index is None:
        return None

    window_start = max(0, anchor_index - per_page // 2)
    start = window_start + (page - 1) * per_page
    end = start + per_page
    page_values = values[start:end]
    has_more = end < len(values)
    return page_values, has_more


@search_bp.route("/api/search_cars")
def search_cars():
    page = request.args.get("page", 1, type=int)
    per_page = min(max(request.args.get("per_page", 30, type=int), 1), 50)
    query = request.args.get("q", "")
    anchor_id = request.args.get("anchor_id", "")

    ensure_loaded = _dependencies.get("ensure_data_loaded")
    if ensure_loaded is not None:
        try:
            ensure_loaded()
        except Exception:
            pass

    car_models = _get_dependency("get_car_models")()

    # Additive: only takes effect when a caller opts in with anchor_id AND
    # sends no search term. Every existing q=&page=N caller (with or without
    # a query, with no anchor_id) falls straight through to the unchanged
    # _paginate_matches() call below, byte-identical to before this param
    # existed.
    if anchor_id and not query:
        anchored = _anchor_paginate(car_models, anchor_id, page, per_page)
        if anchored is not None:
            page_values, has_more = anchored
            return jsonify({
                "results": [{"id": value, "text": strip_shelf_code_for_display(value)} for value in page_values],
                "pagination": {"more": has_more},
            })
        # anchor_id not found (car deleted from Tally, or a bad value) --
        # fall through to the normal unfiltered listing below rather than
        # erroring or returning an empty result.

    matches, has_more = _paginate_matches(car_models, query, page, per_page)
    return jsonify({
        # "id" stays the exact raw name -- that's what gets sent back as the
        # ?car= param and matched against car_master.json/main_hierarchy.json.
        # Only "text" (what the user reads in the dropdown) is cleaned up.
        "results": [{"id": value, "text": strip_shelf_code_for_display(value)} for value in matches],
        "pagination": {"more": has_more},
    })


@search_bp.route("/api/search_car_folders")
def search_car_folders():
    page = request.args.get("page", 1, type=int)
    per_page = min(max(request.args.get("per_page", 30, type=int), 1), 50)
    query = request.args.get("q", "")
    folders = _get_dependency("get_car_folders")()
    matches, has_more = _paginate_matches(folders, query, page, per_page)
    return jsonify({
        "results": [{"id": value, "text": value} for value in matches],
        "pagination": {"more": has_more},
    })


@search_bp.route("/api/get_stock_items_for_car")
def get_stock_items_for_car():
    car_full = request.args.get("car", "")
    hierarchy_lookup = _dependencies.get("get_stock_items_for_car_from_hierarchy")
    items = hierarchy_lookup(car_full) if hierarchy_lookup is not None else None
    if items is None:
        items = _get_dependency("get_stock_items_for_car")(car_full)
    return jsonify({
        "car": car_full,
        "base_car": extract_car_base_name(car_full),
        "count": len(items),
        "items": items,
    })
