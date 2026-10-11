import os
import json
import re
import asyncio
import random
from difflib import SequenceMatcher
import httpx
from bs4 import BeautifulSoup

CACHE_FILE = "guide_cache.json"
STALE_FALLBACK_HOURS = 15.0
STALE_FALLBACK_SOURCES = {"Estimated Fallback", "Smart Playtime Estimate"}
SITE_GUIDE_SOURCES = ("PSNProfiles", "SteamHunters", "HowLongToBeat")
KNOWN_GUIDE_SOURCES = set(SITE_GUIDE_SOURCES)
LONGEST_SITE_PICK = "longest-site"
DLC_HINTS = ("dlc", "pack", "add-on", "addon", "expansion", "pre-order", "preorder", "bundle")
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}


def _is_stale_cache_entry(entry) -> bool:
    if not isinstance(entry, dict):
        return True
    source = entry.get("source")
    if source in STALE_FALLBACK_SOURCES:
        return True
    if entry.get("pick") != LONGEST_SITE_PICK:
        return True
    return source not in KNOWN_GUIDE_SOURCES


def save_cache(cache: dict):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                raw = json.load(f)
            cleaned = {
                name: entry
                for name, entry in raw.items()
                if not _is_stale_cache_entry(entry)
            }
            if cleaned != raw:
                save_cache(cleaned)
            return cleaned
        except Exception:
            return {}
    return {}


GUIDE_CACHE = load_cache()


def clean_game_title(name: str) -> str:
    name = re.sub(r"[™®©]|complete edition|deluxe edition|game of the year edition", "", name, flags=re.IGNORECASE)
    return name.strip()


def normalize_trophy_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def parse_duration_to_hours(text: str):
    if not text:
        return None
    raw = text.lower().replace(",", "")
    hours = 0.0
    minutes = 0.0
    found = False
    hour_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", raw)
    minute_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b", raw)
    if hour_match:
        hours = float(hour_match.group(1))
        found = True
    if minute_match:
        minutes = float(minute_match.group(1))
        found = True
    if found:
        return round(hours + minutes / 60.0, 2)
    lone = re.search(r"(\d+(?:\.\d+)?)", raw)
    if lone:
        return float(lone.group(1))
    return None


def _looks_like_dlc(title: str, query: str) -> bool:
    title_l = (title or "").lower()
    query_l = (query or "").lower()
    return any(hint in title_l for hint in DLC_HINTS) and not any(hint in query_l for hint in DLC_HINTS)


def _title_match_score(title: str, query: str) -> float:
    nt = normalize_trophy_key(title)
    nq = normalize_trophy_key(query)
    if not nt or not nq:
        return 0.0
    if nt == nq:
        return 1.0
    if nq in nt or nt in nq:
        return 0.88
    return SequenceMatcher(None, nt, nq).ratio()


def _absolute_psn_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return "https://psnprofiles.com" + href


def _is_direct_psn_url(url: str) -> bool:
    return bool(url) and "/guide/" in url


def _guide_url_from_trophy(url: str) -> str:
    if url and "/trophies/" in url:
        return url.replace("/trophies/", "/guide/", 1)
    return ""


def extract_psn_trophy_times(soup: BeautifulSoup) -> dict:
    times = {}
    rows = soup.select("table.trophy tbody tr, tr.trophy, div.trophy, li.trophy")
    for row in rows:
        name_el = row.select_one("a.title, a.trophy, .trophy-name, td.title, td a")
        if not name_el:
            continue
        time_el = row.select_one(".hours, .tag.hours, td.hours, [class*='hour']")
        time_text = time_el.get_text(" ", strip=True) if time_el else ""
        if not time_text or parse_duration_to_hours(time_text) is None:
            row_text = row.get_text(" ", strip=True)
            match = re.search(
                r"(\d+(?:\.\d+)?\s*(?:hours?|hrs?|h|minutes?|mins?|m)(?:\s*\d+\s*(?:minutes?|mins?|m))?)",
                row_text,
                re.I,
            )
            time_text = match.group(1) if match else ""
        hours = parse_duration_to_hours(time_text)
        key = normalize_trophy_key(name_el.get_text())
        if key and hours is not None and hours > 0:
            times[key] = hours
    return times


META_COMPLETION_HINTS = (
    "unlock all achievement",
    "unlock every achievement",
    "all achievements",
    "every achievement",
    "complete all achievement",
    "earn all achievement",
    "obtain all achievement",
    "collect all achievement",
    "get all achievement",
    "got them all",
    "platinum trophy",
    "100% the game",
)


def _is_meta_completion_trophy(ach: dict, schema_map: dict) -> bool:
    api_name = (ach.get("name") or "").strip()
    info = schema_map.get(api_name) or {}
    blob = " ".join(
        [
            api_name,
            str(info.get("displayName") or ""),
            str(info.get("description") or ach.get("description") or ""),
        ]
    ).lower()
    return any(hint in blob for hint in META_COMPLETION_HINTS)


def _lookup_trophy_hours(ach: dict, schema_map: dict, trophy_times: dict):
    api_name = (ach.get("name") or "").strip()
    display = schema_map.get(api_name, {}).get("displayName") or api_name
    keys = [normalize_trophy_key(display), normalize_trophy_key(api_name)]
    for key in keys:
        if key and key in trophy_times:
            return trophy_times[key]
    display_key = keys[0]
    for trophy_key, trophy_hours in trophy_times.items():
        if trophy_key and display_key and (trophy_key in display_key or display_key in trophy_key):
            return trophy_hours
    return None


def estimate_hours_remaining(
    total_hours: float,
    playtime_hrs: float,
    achievements: list,
    schema_map: dict,
    trophy_times: dict,
    rarity_map: dict,
    estimate_source: str = "",
):
    """Remaining time = (guide platinum hours / trophy count) * remaining trophies.

    Does not subtract the player's playtime. Extra hours with a low % complete
    just means they weren't trophy hunting.
    """
    remaining_achs = [a for a in achievements if a.get("achieved") != 1]
    if not remaining_achs:
        return 0.0, "complete"

    real_remaining = [a for a in remaining_achs if not _is_meta_completion_trophy(a, schema_map)]
    if not real_remaining:
        return 0.0, "complete"

    remaining_count = len(real_remaining)
    real_total = sum(1 for a in achievements if not _is_meta_completion_trophy(a, schema_map)) or len(achievements)
    weak_total = (
        "smart playtime" in (estimate_source or "").lower()
        or "all players" in (estimate_source or "").lower()
    )

    if trophy_times:
        matched = []
        unmatched = 0
        for ach in real_remaining:
            hours = _lookup_trophy_hours(ach, schema_map, trophy_times)
            if hours is not None:
                matched.append(hours)
            else:
                unmatched += 1
        if matched:
            avg = sum(matched) / len(matched)
            remaining = sum(matched) + avg * unmatched
            return round(max(0.1, remaining), 1), "per-trophy"

    if total_hours and real_total > 0 and not weak_total:
        hours_per_trophy = float(total_hours) / real_total
        remaining = hours_per_trophy * remaining_count
        return round(max(0.1, remaining), 1), "per-trophy"

    return round(max(0.5, remaining_count * 1.25), 1), "per-trophy"


async def _scrape_steamhunters(client: httpx.AsyncClient, app_id: int):
    if not app_id:
        return None, {}, None
    try:
        url = f"https://steamhunters.com/api/apps/{int(app_id)}"
        res = await client.get(url, headers={**BROWSER_HEADERS, "Accept": "application/json"}, timeout=12.0)
        if res.status_code != 200:
            return None, {}, None
        data = res.json()
        minutes = data.get("medianCompletionTime")
        hours = round(float(minutes) / 60.0, 1) if minutes and float(minutes) > 0 else None
        return hours, {}, None
    except Exception:
        return None, {}, None


async def _scrape_completionist_median(client: httpx.AsyncClient, app_id: int):
    """Median playtime of players who 100%'d the game on completionist.me."""
    if not app_id:
        return None
    try:
        url = f"https://completionist.me/steam/app/{app_id}"
        res = await client.get(url, headers=BROWSER_HEADERS, timeout=15.0)
        if res.status_code != 200:
            return None
        blob = BeautifulSoup(res.text, "html.parser").get_text(" ", strip=True)
        match = re.search(
            r"(\d+h\s*\d+m|\d+(?:\.\d+)?\s*hours?)\s*:?\s*Completion Playtime Median",
            blob,
            re.I,
        )
        if not match:
            match = re.search(
                r"Completion Playtime Median[:\s]*(\d+h\s*\d+m|\d+(?:\.\d+)?\s*hours?)",
                blob,
                re.I,
            )
        if not match:
            match = re.search(
                r"(\d+h\s*\d+m|\d+(?:\.\d+)?\s*hours?)\s*:?\s*Completion Playtime Avg",
                blob,
                re.I,
            )
        return parse_duration_to_hours(match.group(1)) if match else None
    except Exception:
        return None


async def _scrape_hltb_completionist(client: httpx.AsyncClient, app_id: int):
    """Average 100% / completionist time from HowLongToBeat, keyed by Steam app id."""
    if not app_id:
        return None
    try:
        url = f"https://hltbapi.codepotatoes.de/steam/{int(app_id)}"
        res = await client.get(url, headers=BROWSER_HEADERS, timeout=12.0)
        if res.status_code != 200:
            return None
        data = res.json()
        hours = data.get("completionist") or data.get("completionistHours")
        if hours and float(hours) > 0:
            return round(float(hours), 1)
    except Exception:
        return None
    return None


def _pick_psn_game_link(soup: BeautifulSoup, query: str) -> str:
    candidates = []
    for link in soup.select("a[href*='/trophies/']"):
        title = link.get_text(" ", strip=True)
        href = link.get("href", "")
        if not title or not href:
            continue
        if _looks_like_dlc(title, query) or _looks_like_dlc(href, query):
            continue
        score = _title_match_score(title, query)
        if score < 0.55:
            continue
        candidates.append((score, -len(title), _absolute_psn_url(href.split("?")[0])))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][2]


def _pick_psn_guide_link(soup: BeautifulSoup, query: str, trophy_page_url: str) -> str:
    candidates = []
    for link in soup.select("a[href*='/guide/']"):
        title = link.get_text(" ", strip=True)
        href = link.get("href", "")
        if not href:
            continue
        if _looks_like_dlc(title, query) or _looks_like_dlc(href, query):
            continue
        score = _title_match_score(title or query, query)
        parent = link.find_parent(["article", "div", "li", "tr", "td"])
        blob = parent.get_text(" ", strip=True) if parent else title
        views = 0
        view_match = re.search(r"([\d,]+)\s*(?:views?|likes?|ratings?)", blob or "", re.I)
        if view_match:
            views = int(view_match.group(1).replace(",", ""))
        plat_bonus = 80 if re.search(r"platinum|trophy guide|100%", title or "", re.I) else 0
        candidates.append((views + plat_bonus, score, _absolute_psn_url(href.split("?")[0])))
    if candidates:
        candidates.sort(reverse=True)
        if candidates[0][1] >= 0.35 or candidates[0][0] > 0:
            return candidates[0][2]
    guessed = _guide_url_from_trophy(trophy_page_url)
    if guessed and not _looks_like_dlc(guessed, query):
        return guessed
    return trophy_page_url


async def _scrape_psn_base_game(client: httpx.AsyncClient, clean_name: str):
    encoded = httpx.URL(clean_name).raw_path.decode("utf-8")
    search_url = f"https://psnprofiles.com/search/games?q={encoded}"
    res = await client.get(search_url, headers=BROWSER_HEADERS, timeout=15.0)
    if res.status_code != 200:
        return None
    soup = BeautifulSoup(res.text, "html.parser")
    trophy_url = _pick_psn_game_link(soup, clean_name)
    if not trophy_url:
        return None

    await asyncio.sleep(random.uniform(0.8, 1.6))
    page_res = await client.get(trophy_url, headers=BROWSER_HEADERS, timeout=15.0)
    if page_res.status_code != 200:
        return {"psn_url": trophy_url, "hours": None, "difficulty": None, "trophy_times": {}}

    g_soup = BeautifulSoup(page_res.text, "html.parser")
    difficulty = None
    hours = None
    difficulty_tag = g_soup.select_one(".difficulty, .tag.difficulty")
    hours_tag = g_soup.select_one(".hours, .tag.hours")
    if difficulty_tag:
        diff_match = re.search(r"(\d+(\.\d+)?)", difficulty_tag.text)
        if diff_match:
            difficulty = float(diff_match.group(1))
    if hours_tag:
        hours = parse_duration_to_hours(hours_tag.text)
    if hours is None:
        blob = g_soup.get_text(" ", strip=True)
        hours_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:Hours|hrs)", blob, re.I)
        if hours_match:
            hours = float(hours_match.group(1))

    guide_url = _pick_psn_guide_link(g_soup, clean_name, trophy_url)
    return {
        "psn_url": guide_url or trophy_url,
        "hours": hours,
        "difficulty": difficulty,
        "trophy_times": extract_psn_trophy_times(g_soup),
    }


async def _safe_psn_metrics(client: httpx.AsyncClient, clean_name: str):
    try:
        await asyncio.sleep(random.uniform(0.6, 1.4))
        return await _scrape_psn_base_game(client, clean_name)
    except Exception:
        return None


def _positive_hours(value):
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if hours > 0:
        return hours
    return None


async def get_game_guide_metrics(client: httpx.AsyncClient, game_name: str, playtime_hrs: float = 0.0, app_id: int | None = None) -> dict:
    clean_name = clean_game_title(game_name).lower().strip()

    cached = GUIDE_CACHE.get(clean_name)
    if cached and not _is_stale_cache_entry(cached):
        cached.setdefault("trophy_times", {})
        cached.setdefault("psn_url", "")
        if cached.get("source", "").startswith("PSNProfiles") and not _is_direct_psn_url(cached.get("psn_url", "")):
            pass
        else:
            return cached
    if cached and _is_stale_cache_entry(cached):
        GUIDE_CACHE.pop(clean_name, None)

    psn, sh_result, hltb_hours = await asyncio.gather(
        _safe_psn_metrics(client, clean_name),
        _scrape_steamhunters(client, app_id),
        _scrape_hltb_completionist(client, app_id),
    )
    sh_hours, sh_times, sh_diff = sh_result if sh_result else (None, {}, None)

    difficulties = []
    trophy_times = {}
    psn_url = ""
    candidates = []

    if psn:
        psn_url = psn.get("psn_url") or ""
        if psn.get("difficulty") is not None:
            difficulties.append(float(psn["difficulty"]))
        trophy_times = psn.get("trophy_times") or {}
        psn_hours = _positive_hours(psn.get("hours"))
        if psn_hours is not None:
            candidates.append((psn_hours, "PSNProfiles"))

    if sh_times:
        trophy_times = {**trophy_times, **sh_times}
    if sh_diff is not None:
        difficulties.append(float(sh_diff))
    sh_hours = _positive_hours(sh_hours)
    if sh_hours is not None:
        candidates.append((sh_hours, "SteamHunters"))

    hltb_hours = _positive_hours(hltb_hours)
    if hltb_hours is not None:
        candidates.append((hltb_hours, "HowLongToBeat"))

    hours = None
    source = ""
    if candidates:
        hours, source = max(candidates, key=lambda item: item[0])
        hours = round(float(hours), 1)

    difficulty = round(sum(difficulties) / len(difficulties), 2) if difficulties else 3.5

    result = {
        "difficulty": difficulty,
        "hours": hours,
        "source": source,
        "pick": LONGEST_SITE_PICK,
        "hours_by_site": {name: round(value, 1) for value, name in candidates},
        "trophy_times": trophy_times,
        "psn_url": psn_url,
    }
    if hours and source and source not in STALE_FALLBACK_SOURCES:
        GUIDE_CACHE[clean_name] = result
        save_cache(GUIDE_CACHE)
    return result
