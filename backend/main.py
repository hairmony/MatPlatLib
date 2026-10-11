import asyncio
from pathlib import Path
from urllib.parse import urlparse
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from backend.steam_service import stream_ranked_backlog

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BANNER_CACHE = {}
BANNER_FILES = (
    "header.jpg",
    "capsule_616x353.jpg",
    "library_hero.jpg",
    "hero_capsule.jpg",
    "header_292x136.jpg",
    "capsule_231x87.jpg",
    "library_600x900.jpg",
    "page_bg_generated.jpg",
    "logo.png",
)
BANNER_PREFIXES = (
    "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/",
    "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{appid}/",
    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/",
    "https://steamcdn-a.akamaihd.net/steam/apps/{appid}/",
    "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/",
)
BANNER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://store.steampowered.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

ALLOWED_IMAGE_HOST_PARTS = (
    "steamstatic.com",
    "steampowered.com",
    "akamaihd.net",
    "steamcommunity.com",
    "fastly.steamstatic.com",
    "steamcontent.com",
)


def _looks_like_image(content: bytes, content_type: str) -> bool:
    if not content or len(content) < 800:
        return False
    head = content[:64].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or head.startswith(b"{") or head.startswith(b"["):
        return False
    if content_type.startswith("image/"):
        return True
    return content[:3] == b"\xff\xd8\xff" or content[:8] == b"\x89PNG\r\n\x1a\n" or content[:4] == b"RIFF"


async def _fetch_banner_url(client: httpx.AsyncClient, url: str):
    try:
        response = await client.get(url, headers=BANNER_HEADERS, timeout=6.0)
    except Exception:
        return None
    content_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if response.status_code != 200 or not _looks_like_image(response.content, content_type):
        return None
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    return response.content, content_type


@app.get("/api/banner/{appid}")
async def proxy_game_banner(appid: int):
    if appid in BANNER_CACHE:
        cached = BANNER_CACHE[appid]
        if not cached:
            raise HTTPException(status_code=404, detail="Banner not found")
        content, content_type = cached
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    urls = []
    for filename in BANNER_FILES:
        for prefix in BANNER_PREFIXES:
            urls.append(f"{prefix.format(appid=appid)}{filename}")
    found = None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
            wave_size = len(BANNER_PREFIXES)
            for start in range(0, len(urls), wave_size):
                wave = urls[start:start + wave_size]
                results = await asyncio.gather(*[_fetch_banner_url(client, url) for url in wave], return_exceptions=True)
                for result in results:
                    if isinstance(result, tuple):
                        found = result
                        break
                if found:
                    break
    except Exception:
        found = None
    BANNER_CACHE[appid] = found
    if not found:
        raise HTTPException(status_code=404, detail="Banner not found")
    content, content_type = found
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/library/{identifier}")
async def fetch_library(identifier: str):
    try:
        return StreamingResponse(stream_ranked_backlog(identifier), media_type="text/event-stream")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/image")
async def proxy_steam_image(url: str = Query(..., min_length=8)):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(status_code=400, detail="Invalid image URL")
    if not any(part in host for part in ALLOWED_IMAGE_HOST_PARTS):
        raise HTTPException(status_code=400, detail="Host not allowed")
    referer = "https://steamcommunity.com/" if "steamcommunity" in url else "https://store.steampowered.com/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    candidates = [url]
    host_swaps = (
        "community.steamstatic.com",
        "community.cloudflare.steamstatic.com",
        "cdn.cloudflare.steamstatic.com",
        "community.akamai.steamstatic.com",
        "cdn.akamai.steamstatic.com",
        "steamcdn-a.akamaihd.net",
        "media.steampowered.com",
    )
    parsed_path = parsed.path or ""
    for swap_host in host_swaps:
        candidates.append(f"https://{swap_host}{parsed_path}")
    seen = set()
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            response = None
            for candidate in unique:
                candidate_headers = dict(headers)
                if "steamcommunity" in candidate:
                    candidate_headers["Referer"] = "https://steamcommunity.com/"
                try:
                    response = await client.get(candidate, headers=candidate_headers)
                except Exception:
                    continue
                if response.status_code == 200 and response.content:
                    break
        if response is None or response.status_code != 200 or not response.content:
            raise HTTPException(status_code=404, detail="Image not found")
        content_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        return Response(
            content=response.content,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Could not load image")


@app.get("/")
async def serve_app():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")