import json
import math
import os
import httpx

STORE_CACHE_FILE = "store_cache.json"
STORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

PLAYER_CATEGORY_IDS = {
    1: "Multi-player",
    2: "Single-player",
    9: "Co-op",
    20: "MMO",
    24: "Shared/Split Screen",
    27: "Cross-Platform Multiplayer",
    36: "Online PvP",
    37: "Shared/Split Screen PvP",
    38: "Online Co-op",
    39: "Shared/Split Screen Co-op",
    47: "LAN PvP",
    48: "LAN Co-op",
    49: "PvP",
}

FEATURE_CATEGORY_IDS = {
    22: "Steam Achievements",
    23: "Steam Cloud",
    25: "Steam Leaderboards",
    29: "Steam Trading Cards",
    30: "Steam Workshop",
    35: "In-App Purchases",
    41: "Remote Play on Phone",
    42: "Remote Play on Tablet",
    43: "Remote Play on TV",
    44: "Remote Play Together",
}

HARDWARE_CATEGORY_IDS = {
    18: "Partial Controller Support",
    28: "Full Controller Support",
    31: "VR Support",
    52: "Tracked Motion Controllers",
}


def load_store_cache() -> dict:
    if os.path.exists(STORE_CACHE_FILE):
        try:
            with open(STORE_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_store_cache(cache: dict):
    try:
        with open(STORE_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


STORE_CACHE = load_store_cache()


def normalize_store_filters(filters: dict | None) -> dict:
    filters = dict(filters or empty_store_filters())
    genres = list(filters.get("genres") or [])
    filters["genres"] = genres
    filters["features"] = list(filters.get("features") or [])
    filters["players"] = list(filters.get("players") or [])
    filters["hardware"] = list(filters.get("hardware") or [])
    filters["is_free"] = bool(filters.get("is_free")) or any(
        str(name).lower() == "free to play" for name in genres
    )
    return filters


def empty_store_filters() -> dict:
    return {"genres": [], "features": [], "players": [], "hardware": [], "is_free": False}


def classify_store_data(data: dict) -> dict:
    genres = []
    for genre in data.get("genres") or []:
        name = (genre.get("description") or "").strip()
        if name:
            genres.append(name)

    players = set()
    features = set()
    hardware = set()
    for category in data.get("categories") or []:
        try:
            cat_id = int(category.get("id"))
        except (TypeError, ValueError):
            continue
        if cat_id in PLAYER_CATEGORY_IDS:
            players.add(PLAYER_CATEGORY_IDS[cat_id])
        if cat_id in FEATURE_CATEGORY_IDS:
            features.add(FEATURE_CATEGORY_IDS[cat_id])
        if cat_id in HARDWARE_CATEGORY_IDS:
            hardware.add(HARDWARE_CATEGORY_IDS[cat_id])

    platforms = data.get("platforms") or {}
    if platforms.get("windows"):
        hardware.add("Windows")
    if platforms.get("mac"):
        hardware.add("Mac")
    if platforms.get("linux"):
        hardware.add("Linux")

    controller = (data.get("controller_support") or "").lower()
    if controller == "full":
        hardware.add("Full Controller Support")
        hardware.discard("Partial Controller Support")
    elif controller == "partial":
        hardware.add("Partial Controller Support")

    is_free = bool(data.get("is_free")) or any(name.lower() == "free to play" for name in genres)

    return {
        "genres": sorted(set(genres)),
        "features": sorted(features),
        "players": sorted(players),
        "hardware": sorted(hardware),
        "is_free": is_free,
    }


async def fetch_store_filters(client: httpx.AsyncClient, app_id: int) -> dict:
    cache_key = str(app_id)
    if cache_key in STORE_CACHE:
        return normalize_store_filters(STORE_CACHE[cache_key])

    url = "https://store.steampowered.com/api/appdetails"
    try:
        response = await client.get(
            url,
            params={"appids": app_id, "l": "english"},
            headers=STORE_HEADERS,
            timeout=15.0,
        )
        if response.status_code != 200:
            return empty_store_filters()
        payload = response.json().get(str(app_id), {})
        if not payload.get("success"):
            STORE_CACHE[cache_key] = empty_store_filters()
            save_store_cache(STORE_CACHE)
            return STORE_CACHE[cache_key]
        filters = classify_store_data(payload.get("data") or {})
        STORE_CACHE[cache_key] = filters
        save_store_cache(STORE_CACHE)
        return filters
    except Exception:
        return empty_store_filters()


PLAY_STATES = [
    {"name": "Platinumed", "min_percent": 100, "kind": "platinum"},
    {"name": "Finalizing", "min_percent": 90, "kind": "percent"},
    {"name": "Advancing", "min_percent": 75, "kind": "percent"},
    {"name": "Halfway", "min_percent": 50, "kind": "percent"},
    {"name": "Started", "min_percent": 25, "kind": "percent"},
    {"name": "Played", "min_percent": None, "kind": "played"},
    {"name": "Unplayed", "min_percent": None, "kind": "unplayed"},
]


def classify_play_state(percentage: float, is_platinum: bool, playtime_hours: float) -> str:
    if is_platinum:
        return next(state["name"] for state in PLAY_STATES if state["kind"] == "platinum")
    for state in PLAY_STATES:
        if state["kind"] == "percent" and percentage >= float(state["min_percent"]):
            return state["name"]
    if playtime_hours > 0:
        return next(state["name"] for state in PLAY_STATES if state["kind"] == "played")
    return next(state["name"] for state in PLAY_STATES if state["kind"] == "unplayed")


def compute_finishability(
    percentage,
    hours_remaining,
    remaining,
    difficulty,
    is_platinum,
    est_hours=None,
) -> float:
    """0-100 difficulty-style score: lower is an easier / closer platinum.

    Mixes remaining platinum time, PSN/SteamHunters difficulty, game length,
    trophies left, and completion percentage.
    """
    if is_platinum:
        return 0.0
    percentage = min(max(float(percentage or 0), 0.0), 100.0)
    hours_remaining = max(float(hours_remaining or 0), 0.0)
    est_hours = max(float(est_hours if est_hours is not None else hours_remaining), 0.0)
    remaining = max(int(remaining or 0), 0)
    difficulty = min(max(float(difficulty or 3.5), 1.0), 10.0)

    # ~32 pts: 0h left ≈ 32, 8h ≈ 14, 20h ≈ 4
    time_part = 32.0 * math.exp(-hours_remaining / 10.0)
    # ~22 pts: 1/10 difficulty ≈ 22, 5/10 ≈ 12, 10/10 ≈ 0
    diff_part = 22.0 * (1.0 - (difficulty - 1.0) / 9.0)
    # ~22 pts: 0% → 0, 50% → 11, 90% → 20
    completion_part = 22.0 * (percentage / 100.0)
    # ~14 pts: short 5h plat ≈ 11, 20h ≈ 5, 60h ≈ 1
    length_part = 14.0 * math.exp(-est_hours / 18.0)
    # ~10 pts: few trophies left
    trophy_part = 10.0 * math.exp(-remaining / 18.0)

    ease = time_part + diff_part + completion_part + length_part + trophy_part
    difficulty_score = 100.0 - ease
    return round(min(100.0, max(0.0, difficulty_score)), 1)
