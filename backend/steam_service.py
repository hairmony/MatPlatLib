import os
import re
import json
import asyncio
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from backend.guide_scraper import estimate_hours_remaining, get_game_guide_metrics
from backend.store_filters import classify_play_state, compute_finishability, fetch_store_filters

load_dotenv()
STEAM_API_KEY = os.getenv("STEAM_API_KEY")
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com"
COMMUNITY_CDN = "https://steamcdn-a.akamaihd.net"
STEAM_CDN_HOSTS = (
    "steamcdn-a.akamaihd.net",
    "cdn.akamai.steamstatic.com",
    "community.akamai.steamstatic.com",
    "shared.akamai.steamstatic.com",
    "clan.akamai.steamstatic.com",
    "cdn.cloudflare.steamstatic.com",
    "community.cloudflare.steamstatic.com",
    "shared.cloudflare.steamstatic.com",
    "clan.cloudflare.steamstatic.com",
    "community.steamstatic.com",
    "cdn.steamstatic.com",
    "media.steampowered.com",
)
COMMUNITY_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def store_capsule_url(app_id: int) -> str:
    return f"{STEAM_CDN}/steam/apps/{app_id}/header.jpg"


def normalize_steam_icon(raw_icon: str, app_id: int, fallback_header: bool = True) -> str:
    if not raw_icon:
        return store_capsule_url(app_id) if fallback_header else ""
    url = raw_icon.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        filename = url.split("/")[-1]
        if "." not in filename:
            filename = f"{filename}.jpg"
        url = f"{COMMUNITY_CDN}/steamcommunity/public/images/apps/{app_id}/{filename}"
    url = url.replace("http://", "https://")
    if "/steam/apps/" in url and fallback_header is False:
        return ""
    return url


JUNK_GAME_NAMES = {
    "achievement progress",
    "view stats",
    "global achievements",
    "compare",
    "stats",
    "achievements",
    "game stats",
    "personal game stats",
}


def _is_junk_game_name(name: str) -> bool:
    cleaned = (name or "").strip().lower()
    return not cleaned or cleaned in JUNK_GAME_NAMES or cleaned.startswith("app ")


def _upsert_game(games_map: dict, app_id, name: str, logo: str, hours: float, icon: str = "", allow_new: bool = True):
    try:
        app_id = int(app_id)
    except (TypeError, ValueError):
        return
    hours = float(hours or 0)
    icon = normalize_steam_icon(icon, app_id, fallback_header=False) if icon else ""
    logo = normalize_steam_icon(logo, app_id) if logo else ""
    clean_name = "" if _is_junk_game_name(name) else (name or "").strip()
    existing = games_map.get(app_id)
    if not existing:
        if not allow_new:
            return
        games_map[app_id] = {
            "appid": app_id,
            "name": clean_name or f"App {app_id}",
            "logo": logo,
            "icon": icon,
            "playtime_hours": round(hours, 1),
        }
        return
    existing["playtime_hours"] = max(existing["playtime_hours"], round(hours, 1))
    if clean_name and _is_junk_game_name(existing.get("name", "")):
        existing["name"] = clean_name
    if logo and not existing.get("logo"):
        existing["logo"] = logo
    if icon and not existing.get("icon"):
        existing["icon"] = icon


def _community_games_from_payload(data) -> list:
    games = []
    if isinstance(data, list):
        games = data
    elif isinstance(data, dict):
        games = (
            data.get("rgGames")
            or data.get("rgPerfectGames")
            or data.get("rgGamesPerfect")
            or data.get("games")
            or data.get("response", {}).get("games")
            or []
        )
    if not isinstance(games, list) or not games:
        return []
    first = games[0]
    if not isinstance(first, dict) or not (first.get("appid") or first.get("appID") or first.get("appId")):
        return []
    return games


def _ingest_game_dicts(games, games_map: dict, allow_new: bool = True):
    if not isinstance(games, list):
        return
    for game in games:
        if not isinstance(game, dict):
            continue
        hours = game.get("hours_forever", game.get("hours", game.get("hours_played", 0.0))) or 0.0
        try:
            hours = float(str(hours).replace(",", ""))
        except (TypeError, ValueError):
            hours = 0.0
        _upsert_game(
            games_map,
            game.get("appid") or game.get("appID") or game.get("appId"),
            game.get("name", ""),
            game.get("logo", ""),
            hours,
            game.get("icon", ""),
            allow_new=allow_new,
        )


def _ingest_community_page(text: str, games_map: dict, allow_new: bool = True):
    if not text:
        return
    decoder = json.JSONDecoder()
    for marker in ("rgGames", "rgPerfectGames", "rgGamesPerfect", "rgRecentGames"):
        idx = 0
        while True:
            found = text.find(marker, idx)
            if found < 0:
                break
            start = text.find("[", found)
            if start < 0 or start - found > 80:
                idx = found + len(marker)
                continue
            try:
                games, _ = decoder.raw_decode(text[start:])
                _ingest_game_dicts(games, games_map, allow_new=allow_new)
            except json.JSONDecodeError:
                pass
            idx = found + len(marker)

    soup = BeautifulSoup(text, "html.parser")
    for row in soup.select(".gameListRow"):
        row_id = row.get("id") or ""
        app_id = row.get("data-appid") or row_id.replace("game_", "")
        name_el = row.select_one(".gameListRowItemName, .ellipsis, h4")
        hours_el = row.select_one(".hours_played, .gameListHours, h5")
        hours = 0.0
        if hours_el:
            match = re.search(r"([\d,.]+)", hours_el.get_text())
            if match:
                try:
                    hours = float(match.group(1).replace(",", ""))
                except ValueError:
                    hours = 0.0
        _upsert_game(
            games_map,
            app_id,
            name_el.get_text(strip=True) if name_el else "",
            "",
            hours,
            allow_new=allow_new,
        )


def _upsert_api_game(games_map: dict, game: dict):
    app_id = game.get("appid")
    icon_hash = game.get("img_icon_url") or ""
    logo_hash = game.get("img_logo_url") or icon_hash
    icon = f"{COMMUNITY_CDN}/steamcommunity/public/images/apps/{app_id}/{icon_hash}.jpg" if icon_hash else ""
    logo = f"{COMMUNITY_CDN}/steamcommunity/public/images/apps/{app_id}/{logo_hash}.jpg" if logo_hash else ""
    _upsert_game(
        games_map,
        app_id,
        game.get("name", f"App {app_id}"),
        logo,
        round(game.get("playtime_forever", 0) / 60, 1),
        icon,
    )


async def _merge_community_json(client: httpx.AsyncClient, url: str, games_map: dict, allow_new: bool = True):
    try:
        res = await client.get(url, timeout=15.0)
        if res.status_code != 200:
            return
        content = res.content.strip()
        if not content:
            return
        if content.startswith(b"<"):
            if b"rgGames" in content or b"rgPerfectGames" in content or b"gameListRow" in content:
                _ingest_community_page(res.text, games_map, allow_new=allow_new)
            return
        data = res.json()
        _ingest_game_dicts(_community_games_from_payload(data), games_map, allow_new=allow_new)
    except Exception:
        return


async def _merge_community_html(client: httpx.AsyncClient, url: str, games_map: dict, allow_new: bool = True):
    """Parse public games lists: owned, shared, EA Play / Game Pass, and 100%s."""
    try:
        res = await client.get(url, timeout=20.0)
        if res.status_code != 200 or not res.text:
            return
        if "gameListRow" not in res.text and "rgGames" not in res.text and "rgPerfectGames" not in res.text:
            return
        _ingest_community_page(res.text, games_map, allow_new=allow_new)
    except Exception:
        return


async def _merge_community_xml(client: httpx.AsyncClient, url: str, games_map: dict, allow_new: bool = True):
    try:
        res = await client.get(url, timeout=20.0)
        if res.status_code != 200 or not res.content:
            return
        try:
            soup = BeautifulSoup(res.content, "xml")
        except Exception:
            soup = BeautifulSoup(res.content, "html.parser")
        for game in soup.find_all("game"):
            app_node = game.find("appID") or game.find("appId") or game.find("appid")
            if not app_node:
                continue
            name_node = game.find("name")
            logo_node = game.find("logo")
            icon_node = game.find("icon")
            hours_node = game.find("hoursOnRecord") or game.find("hours")
            hours_text = (hours_node.get_text() if hours_node else "0").replace(",", "")
            try:
                hours = float(hours_text or 0)
            except ValueError:
                hours = 0.0
            _upsert_game(
                games_map,
                app_node.get_text(),
                name_node.get_text() if name_node else "",
                logo_node.get_text() if logo_node else "",
                hours,
                icon_node.get_text() if icon_node else "",
                allow_new=allow_new,
            )
    except Exception:
        return


async def fetch_unified_games_list(identifier: str, steam_id: str):
    games_map = {}
    vanity = identifier if not (identifier.isdigit() and len(identifier) == 17) else ""
    profile_urls = [f"https://steamcommunity.com/profiles/{steam_id}"]
    if vanity:
        profile_urls.append(f"https://steamcommunity.com/id/{vanity}")
    html_urls = [
        f"{base}/games/?tab={tab}"
        for base in profile_urls
        for tab in ("all", "perfect", "recent")
    ]
    json_urls = [
        f"{base}/games?tab={tab}&json=1"
        for base in profile_urls
        for tab in ("all", "perfect")
    ]
    xml_urls = [
        f"{base}/games?tab={tab}&xml=1"
        for base in profile_urls
        for tab in ("all", "perfect")
    ]

    async with httpx.AsyncClient(follow_redirects=True, headers=COMMUNITY_HEADERS, timeout=20.0) as client:
        owned_url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
        owned_params = {
            "key": STEAM_API_KEY,
            "steamid": steam_id,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "include_free_sub": 1,
            "skip_unvetted_apps": 0,
        }
        try:
            res = await client.get(owned_url, params=owned_params, timeout=20.0)
            if res.status_code == 200:
                for game in res.json().get("response", {}).get("games", []):
                    _upsert_api_game(games_map, game)
        except Exception:
            pass

        recent_url = "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
        try:
            res = await client.get(
                recent_url,
                params={"key": STEAM_API_KEY, "steamid": steam_id, "count": 50},
                timeout=15.0,
            )
            if res.status_code == 200:
                for game in res.json().get("response", {}).get("games", []):
                    _upsert_api_game(games_map, game)
        except Exception:
            pass

        for url in json_urls:
            await _merge_community_json(client, url, games_map, allow_new=True)
        for url in xml_urls:
            await _merge_community_xml(client, url, games_map, allow_new=True)
        for url in html_urls:
            await _merge_community_html(client, url, games_map, allow_new=True)

    return list(games_map.values())


async def fetch_game_schema(client: httpx.AsyncClient, app_id: int):
    """Fetches official achievement display names, descriptions, and icon URLs from Steam Schema."""
    url = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
    params = {"key": STEAM_API_KEY, "appid": app_id}
    try:
        response = await client.get(url, params=params, timeout=15.0)
        if response.status_code != 200:
            return {}

        achievements = response.json().get("game", {}).get("availableGameStats", {}).get("achievements", [])
        schema_map = {}
        for achievement in achievements:
            api_name = achievement.get("name", "").strip()
            display_name = achievement.get("displayName", api_name)
            description = achievement.get("description", "No description available.")
            if isinstance(display_name, dict):
                display_name = display_name.get("english") or next(iter(display_name.values()), api_name)
            if isinstance(description, dict):
                description = description.get("english") or next(iter(description.values()), "No description available.")
            schema_map[api_name] = {
                "displayName": display_name or api_name,
                "description": description or "No description available.",
                "icon": normalize_steam_icon(achievement.get("icon") or "", app_id, fallback_header=False),
                "icon_gray": normalize_steam_icon(achievement.get("icongray") or "", app_id, fallback_header=False),
            }
        return schema_map
    except Exception:
        return {}


async def fetch_global_rarity(client: httpx.AsyncClient, app_id: int) -> dict:
    url = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
    try:
        response = await client.get(url, params={"gameid": app_id}, timeout=15.0)
        if response.status_code != 200:
            return {}
        rarity = {}
        for achievement in response.json().get("achievementpercentages", {}).get("achievements", []):
            rarity[achievement.get("name", "").strip()] = float(achievement.get("percent", 50.0))
        return rarity
    except Exception:
        return {}


async def fetch_steamhunters_profile(client: httpx.AsyncClient, steam_id: str) -> dict:
    try:
        url = f"https://steamhunters.com/api/steam-users/{steam_id}"
        response = await client.get(
            url,
            headers={**COMMUNITY_HEADERS, "Accept": "application/json"},
            timeout=12.0,
        )
        if response.status_code != 200:
            return {}
        data = response.json() or {}
        if not data.get("isProfileUpdated"):
            return {}
        agc = data.get("agc") or data.get("steamAgc") or 0
        return {
            "perfect_games": data.get("perfectGameCount") or data.get("unrestrictedPerfectGameCount") or 0,
            "agc": agc,
        }
    except Exception:
        return {}


def _guide_links(app_id: int, game_name: str, guide_metrics: dict | None = None) -> tuple[str, str]:
    clean_query_name = game_name.replace("™", "").replace("®", "").strip()
    psn = (guide_metrics or {}).get("psn_url") or (
        f"https://psnprofiles.com/search/games?q={httpx.URL(clean_query_name).raw_path.decode('utf-8')}"
    )
    steamhunters = (
        f"https://steamhunters.com/stats/{app_id}/achievements"
        if app_id
        else f"https://steamcommunity.com/app/{app_id}/guides/?browsesort=trend&section=achievements"
    )
    return psn, steamhunters


def _library_stub(game_info: dict, store_filters: dict | None = None, guide_metrics: dict | None = None) -> dict:
    app_id = game_info["appid"]
    game_name = game_info["name"]
    playtime_hrs = game_info.get("playtime_hours") or 0
    est_hours = (guide_metrics or {}).get("hours") or 0
    difficulty = (guide_metrics or {}).get("difficulty") or 3.5
    remaining_hours = est_hours if est_hours else 0
    psn_url, steamhunters_url = _guide_links(app_id, game_name, guide_metrics)
    return {
        "appid": app_id,
        "name": game_name,
        "playtime_hours": playtime_hrs,
        "icon_url": store_capsule_url(app_id),
        "logo_url": game_info.get("logo") or "",
        "library_icon": game_info.get("icon") or "",
        "total": 0,
        "unlocked": 0,
        "remaining": 0,
        "percentage": 0.0,
        "tracked": False,
        "unlocked_list": [],
        "difficulty": difficulty,
        "est_hours": est_hours,
        "hours_remaining": remaining_hours,
        "hours_remaining_source": (guide_metrics or {}).get("source") or "",
        "is_platinum": False,
        "finishability": None,
        "play_state": classify_play_state(0, False, playtime_hrs),
        "store": store_filters or {},
        "guide_source": (guide_metrics or {}).get("source") or "",
        "psnprofiles_url": psn_url,
        "steamhunters_url": steamhunters_url,
    }


async def fetch_player_achievements_for_game(client: httpx.AsyncClient, steam_id: str, game_info: dict):
    app_id = game_info["appid"]
    game_name = game_info["name"]
    playtime_hrs = game_info["playtime_hours"]
    url = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/"
    params = {"key": STEAM_API_KEY, "steamid": steam_id, "appid": app_id, "l": "english"}

    try:
        ach_response, schema_map, rarity_map, store_filters = await asyncio.gather(
            client.get(url, params=params, timeout=20.0),
            fetch_game_schema(client, app_id),
            fetch_global_rarity(client, app_id),
            fetch_store_filters(client, app_id),
        )

        data = {}
        achievements = []
        if ach_response.status_code == 200:
            data = ach_response.json().get("playerstats", {})
            achievements = data.get("achievements", []) if data.get("success", False) else []
        total = len(achievements)
        if total == 0:
            return _library_stub(game_info, store_filters)

        unlocked_list = []
        unlocked_count = 0
        for achievement in achievements:
            if achievement.get("achieved") != 1:
                continue
            unlocked_count += 1
            api_name = achievement.get("name", "").strip()
            schema_info = schema_map.get(api_name, {})
            unlocked_list.append({
                "apiname": schema_info.get("displayName") or api_name.replace("_", " ").title(),
                "description": schema_info.get("description", "No description available."),
                "icon": schema_info.get("icon", ""),
                "icon_gray": schema_info.get("icon_gray", ""),
                "game_icon": game_info.get("icon") or game_info.get("logo") or "",
                "appid": app_id,
                "unlocktime": achievement.get("unlocktime", 0),
                "game_name": game_name,
            })

        percentage = round((unlocked_count / total) * 100, 1) if total > 0 else 0.0
        guide_metrics = await get_game_guide_metrics(client, game_name, playtime_hrs, app_id)
        difficulty = guide_metrics["difficulty"]
        est_hours = guide_metrics.get("hours")
        guide_source = guide_metrics.get("source") or ""
        remaining_hours, remaining_source = estimate_hours_remaining(
            est_hours or 0,
            playtime_hrs,
            achievements,
            schema_map,
            guide_metrics.get("trophy_times") or {},
            rarity_map,
            guide_source,
        )
        if percentage == 100.0:
            remaining_hours = 0.0
            remaining_source = "complete"

        is_platinum = percentage == 100.0
        remaining = total - unlocked_count
        if est_hours:
            est_hours = float(est_hours)
            if remaining_hours > est_hours and remaining and total:
                remaining_hours = round(est_hours * remaining / total, 1)
        elif remaining and remaining_hours:
            est_hours = round(float(remaining_hours) * total / remaining, 1)
            if not guide_source:
                guide_source = "Steam achievement estimate"
        else:
            est_hours = 0.0
        finishability = compute_finishability(
            percentage,
            remaining_hours,
            remaining,
            difficulty,
            is_platinum,
            est_hours,
        )
        play_state = classify_play_state(percentage, is_platinum, playtime_hrs)
        psn_url, steamhunters_url = _guide_links(app_id, game_name, guide_metrics)

        return {
            "appid": app_id,
            "name": game_name,
            "playtime_hours": playtime_hrs,
            "icon_url": store_capsule_url(app_id),
            "logo_url": game_info.get("logo") or "",
            "library_icon": game_info.get("icon") or "",
            "total": total,
            "unlocked": unlocked_count,
            "remaining": remaining,
            "percentage": percentage,
            "tracked": True,
            "unlocked_list": unlocked_list,
            "difficulty": difficulty,
            "est_hours": est_hours,
            "hours_remaining": remaining_hours,
            "hours_remaining_source": remaining_source,
            "is_platinum": is_platinum,
            "finishability": finishability,
            "play_state": play_state,
            "store": store_filters,
            "guide_source": guide_source,
            "psnprofiles_url": psn_url,
            "steamhunters_url": steamhunters_url,
        }
    except Exception:
        return _library_stub(game_info)


def _showcase_event(sh_profile: dict, total_platinums: int = 0, trophies_in_platinums: int = 0, recent=None) -> str:
    payload = {
        "type": "showcase",
        "data": {
            "type": "showcase",
            "total_platinums": total_platinums,
            "trophies_in_platinums": trophies_in_platinums,
            "recent_achievements": recent or [],
            "perfect_games": (sh_profile or {}).get("perfect_games"),
            "agc": (sh_profile or {}).get("agc"),
        },
    }
    return f"data: {json.dumps(payload)}\n\n"


async def stream_ranked_backlog(identifier: str):
    try:
        steam_id = await resolve_steam_id(identifier)

        async with httpx.AsyncClient(timeout=20.0) as client:
            sh_task = asyncio.create_task(fetch_steamhunters_profile(client, steam_id))
            games_task = asyncio.create_task(fetch_unified_games_list(identifier, steam_id))

            sh_profile = {}
            try:
                sh_profile = await sh_task
            except Exception:
                sh_profile = {}
            yield _showcase_event(sh_profile)

            games_list = await games_task

            if not games_list:
                yield f"data: {json.dumps({'error': 'No games found. Ensure profile and game details are set to Public.'})}\n\n"
                return

            yield f"data: {json.dumps({'type': 'init', 'total': len(games_list)})}\n\n"

            all_unlocked_achievements = []
            total_platinums = 0
            trophies_in_platinums = 0

            chunk_size = 8
            for i in range(0, len(games_list), chunk_size):
                chunk = games_list[i:i + chunk_size]
                tasks = [fetch_player_achievements_for_game(client, steam_id, g) for g in chunk]
                results = await asyncio.gather(*tasks)

                for res in results:
                    if not res:
                        continue

                    if res["is_platinum"]:
                        total_platinums += 1
                        trophies_in_platinums += res["total"]

                    if res["unlocked_list"]:
                        all_unlocked_achievements.extend(res["unlocked_list"])

                    difficulty = res["difficulty"]
                    est_hours = res["est_hours"]
                    hours_remaining = res["hours_remaining"]
                    finishability = res["finishability"]

                    meta = {
                        "difficulty": difficulty,
                        "hours": est_hours,
                        "hours_remaining": hours_remaining,
                        "hours_remaining_source": res["hours_remaining_source"],
                        "playthroughs": 1,
                        "source": res["guide_source"],
                        "psnprofiles_url": res["psnprofiles_url"],
                        "steamhunters_url": res["steamhunters_url"],
                    }

                    ach_data_for_client = {
                        "total": res["total"],
                        "unlocked": res["unlocked"],
                        "remaining": res["remaining"],
                        "percentage": res["percentage"],
                        "tracked": res["tracked"],
                    }

                    game_obj = {
                        "type": "game",
                        "appid": res["appid"],
                        "name": res["name"],
                        "playtime_hours": res["playtime_hours"],
                        "icon_url": res["icon_url"],
                        "logo_url": res.get("logo_url") or "",
                        "library_icon": res.get("library_icon") or "",
                        "achievements": ach_data_for_client,
                        "metadata": meta,
                        "finishability": finishability,
                        "play_state": res["play_state"],
                        "store": res.get("store") or {},
                        "is_platinum": res["is_platinum"],
                    }
                    yield f"data: {json.dumps(game_obj)}\n\n"

            all_unlocked_achievements.sort(key=lambda x: x["unlocktime"], reverse=True)
            yield _showcase_event(
                sh_profile,
                total_platinums,
                trophies_in_platinums,
                all_unlocked_achievements[:21],
            )
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'error': f'Server stream error: {str(e)}'})}\n\n"


async def resolve_steam_id(identifier: str) -> str:
    if identifier.isdigit() and len(identifier) == 17:
        return identifier

    url = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/"
    params = {"key": STEAM_API_KEY, "vanityurl": identifier}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        data = response.json().get("response", {})

    if data.get("success") == 1:
        return data.get("steamid")

    raise ValueError(f"Could not resolve Steam ID for '{identifier}'. Ensure the profile name is correct and public.")
