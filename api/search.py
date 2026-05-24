from flask import Blueprint, jsonify, request

from utils.normalize import extract_car_base_name, normalize_text


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


@search_bp.route("/api/search_cars")
def search_cars():
    page = request.args.get("page", 1, type=int)
    per_page = min(max(request.args.get("per_page", 30, type=int), 1), 50)
    query = request.args.get("q", "")

    ensure_loaded = _dependencies.get("ensure_data_loaded")
    if ensure_loaded is not None:
        try:
            ensure_loaded()
        except Exception:
            pass

    car_models = _get_dependency("get_car_models")()
    matches, has_more = _paginate_matches(car_models, query, page, per_page)
    return jsonify({
        "results": [{"id": value, "text": value} for value in matches],
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


@search_bp.route("/api/search_customers")
def search_customers():
    page = request.args.get("page", 1, type=int)
    per_page = min(max(request.args.get("per_page", 30, type=int), 1), 50)
    query = request.args.get("q", "")
    customers = _get_dependency("get_customer_users")()

    if not normalize_text(query):
        return jsonify({"results": [], "pagination": {"more": False}})

    matches = []
    normalized_query = normalize_text(query)
    for customer in customers:
        username = str(customer.get("username") or "")
        paused = bool(customer.get("force_contact_us"))
        status = "Paused" if paused else "Active"
        if normalized_query in normalize_text(username):
            matches.append({"id": customer["id"], "text": f"{username} ({status})"})

    start = (page - 1) * per_page
    end = start + per_page
    return jsonify({
        "results": matches[start:end],
        "pagination": {"more": end < len(matches)},
    })


@search_bp.route("/api/get_stock_items_for_car")
def get_stock_items_for_car():
    car_full = request.args.get("car", "")
    items = _get_dependency("get_stock_items_for_car")(car_full)
    return jsonify({
        "car": car_full,
        "base_car": extract_car_base_name(car_full),
        "count": len(items),
        "items": items,
    })
