import os
import sys
import re
import io
import time
import socket
import threading
import subprocess
from typing import Optional, Dict, Any, List
import requests
from fastapi import FastAPI, Query, Path, HTTPException, Header, Request, Depends
from fastapi.responses import FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from cachetools import TTLCache
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
try:
    import qrcode
    from PIL import Image
except ImportError:
    qrcode = None
    Image = None

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import auth
import database
import music_api
import stream_downloader
import library_manager
import playlist_manager
from pydantic import BaseModel


def to_dict(model: BaseModel) -> Dict[str, Any]:
    """Compatibility helper for Pydantic v1 (.dict()) and v2 (.model_dump())."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


class AuthRequest(BaseModel):
    username: str
    password: str


class CreatePlaylistRequest(BaseModel):
    name: str
    description: Optional[str] = ""


class AddTrackRequest(BaseModel):
    id: str
    title: str
    artist: Optional[str] = ""
    duration: Optional[str] = ""
    thumbnail: Optional[str] = ""
    album: Optional[str] = ""


class FavoriteRequest(BaseModel):
    id: str
    title: str
    artist: Optional[str] = ""
    duration: Optional[str] = ""
    thumbnail: Optional[str] = ""
    album: Optional[str] = ""


class LibraryTrackRequest(BaseModel):
    id: str
    title: str
    artist: Optional[str] = ""
    duration: Optional[str] = ""
    thumbnail: Optional[str] = ""
    album: Optional[str] = ""
    source: Optional[str] = "download"


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Spoti")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Initialize database
database.init_db()

# General in-memory cache for home feed and explore
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()

# Bounded in-memory resolution cache (300 items max, 24h TTL)
_RESOLVE_CACHE: TTLCache = TTLCache(maxsize=300, ttl=86400)
_RESOLVE_LOCK = threading.Lock()

# Allow requests from local LAN devices, private subnets (including 172.16/12), and tunnels
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2[0-9]|3[0-1])\.\d+\.\d+)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    if request.url.path.startswith("/api/"):
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
    return response


def get_current_user_optional(authorization: Optional[str] = Header(None)) -> Optional[Dict[str, Any]]:
    if not authorization:
        return None
    token = authorization
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    try:
        return auth.verify_token(token)
    except auth.AuthServiceUnavailableError as e:
        raise HTTPException(
            status_code=503,
            detail="Authentication service is temporarily unavailable. Please retry shortly."
        )


def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = get_current_user_optional(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required. Please log in.")
    return user


def get_local_ip() -> str:
    """Finds the local LAN IP address of this computer."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def resolve_request_url(request: Request) -> str:
    ip = get_local_ip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or f"{ip}:8000"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    return f"{proto}://{host}"


# -------------------------------------------------------------
# System & Diagnostics Endpoints
# -------------------------------------------------------------
@app.get("/api/info")
def get_server_info(request: Request):
    ip = get_local_ip()
    active_url = resolve_request_url(request)
    return {
        "local_ip": ip,
        "port": 8000,
        "url": active_url,
        "desktop_url": "http://localhost:8000",
    }


@app.get("/api/qr")
def get_qr_code(request: Request):
    """Generates a QR code PNG pointing to the server's local network or HTTPS URL."""
    target_url = resolve_request_url(request)
    if qrcode is not None:
        try:
            qr = qrcode.QRCode(box_size=8, border=2)
            qr.add_data(target_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#10b981", back_color="#0b0f19")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")
        except Exception:
            pass
    return JSONResponse({"url": target_url, "message": "QR generation unavailable"})


# -------------------------------------------------------------
# Authentication Endpoints
# -------------------------------------------------------------
@app.post("/api/auth/register")
@limiter.limit("10/minute")
def register(request: Request, req: AuthRequest):
    try:
        data = auth.register_user(req.username, req.password)
        # Migrate any legacy playlists to the user
        playlist_manager._migrate_legacy_json(data["id"])
        return data
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except auth.AuthServiceUnavailableError as e:
        raise HTTPException(status_code=503, detail="Authentication service is temporarily unavailable.")


@app.post("/api/auth/login")
@limiter.limit("15/minute")
def login(request: Request, req: AuthRequest):
    try:
        data = auth.authenticate_user(req.username, req.password)
        return data
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except auth.AuthServiceUnavailableError as e:
        raise HTTPException(status_code=503, detail="Authentication service is temporarily unavailable.")


@app.get("/api/auth/me")
def get_me(authorization: Optional[str] = Header(None)):
    user = get_current_user_optional(authorization)
    if not user:
        return {"authenticated": False, "user": None}
    return {
        "authenticated": True,
        "user": {
            "id": user["uid"],
            "username": user["usr"]
        }
    }


import json

_FETCH_IN_PROGRESS = False
_FETCH_LOCK = threading.Lock()


def get_explore_cache_meta() -> Dict[str, Any]:
    """Retrieves metadata about when Explore charts were last refreshed."""
    paths_to_check = [
        "/var/mobile/music-data/.cache_meta.json",
        os.path.join(PROJECT_DIR, "music-data", ".cache_meta.json")
    ]
    for p in paths_to_check:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def trigger_daily_fetch_if_stale(force: bool = False) -> bool:
    """Checks if Explore data is older than 24 hours and kicks off background daily_fetcher."""
    global _FETCH_IN_PROGRESS
    with _FETCH_LOCK:
        if _FETCH_IN_PROGRESS:
            return False

        meta = get_explore_cache_meta()
        last_fetch = meta.get("last_fetch", 0)
        now = time.time()
        if not force and (now - last_fetch < 86400):
            return False

        _FETCH_IN_PROGRESS = True

    def _worker():
        global _FETCH_IN_PROGRESS
        try:
            runner = "/var/mobile/server/scripts/spoti-fetcher-runner.sh"
            cmd = [runner] if os.path.exists(runner) else [sys.executable, os.path.join(PROJECT_DIR, "daily_fetcher.py")]
            if force:
                cmd.append("--force")
            print(f"[DailyFetcher] Launching background chart fetch (force={force})...")
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if res.returncode == 0:
                print("[DailyFetcher] Background chart fetch succeeded!")
                # Invalidate in-memory cache so next request gets fresh data
                with _CACHE_LOCK:
                    if "home" in _CACHE:
                        del _CACHE["home"]
            else:
                print(f"[DailyFetcher] Background fetch failed (code {res.returncode}): {res.stderr}")
        except Exception as e:
            print(f"Warning: Daily fetch worker error: {e}")
        finally:
            with _FETCH_LOCK:
                _FETCH_IN_PROGRESS = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return True


def load_explore_from_pocketbase() -> Optional[Dict[str, Any]]:
    """Loads curated Explore data from PocketBase collections."""
    try:
        pb = database.get_pb()
        trending_recs = pb.collection("trending").get_full_list(query_params={"sort": "rank"})
        billboard_recs = pb.collection("billboard").get_full_list(query_params={"sort": "rank"})
        new_releases_recs = pb.collection("new_releases").get_full_list()
        genres_recs = pb.collection("genres").get_full_list(query_params={"sort": "genre_id"})

        if not trending_recs and not billboard_recs:
            return None

        trending = [{
            "id": r.id,
            "title": r.title,
            "artist": getattr(r, "artist", ""),
            "thumbnail": getattr(r, "thumbnail", ""),
            "rank": getattr(r, "rank", 0),
            "source": getattr(r, "source", ""),
            "yt_id": getattr(r, "yt_id", "")
        } for r in trending_recs]

        billboard = [{
            "id": r.id,
            "title": r.title,
            "artist": getattr(r, "artist", ""),
            "thumbnail": getattr(r, "thumbnail", ""),
            "rank": getattr(r, "rank", 0),
            "peak_pos": getattr(r, "peak_pos", None),
            "last_pos": getattr(r, "last_pos", None),
            "weeks_on_chart": getattr(r, "weeks_on_chart", None),
            "yt_id": getattr(r, "yt_id", "")
        } for r in billboard_recs]

        new_releases = [{
            "id": r.id,
            "title": r.title,
            "artist": getattr(r, "artist", ""),
            "thumbnail": getattr(r, "thumbnail", ""),
            "release_date": getattr(r, "release_date", ""),
            "source": getattr(r, "source", ""),
            "yt_id": getattr(r, "yt_id", "")
        } for r in new_releases_recs]

        genres = [{
            "id": r.id,
            "genre_id": getattr(r, "genre_id", 0),
            "name": getattr(r, "name", ""),
            "picture": getattr(r, "picture", ""),
            "tracks": getattr(r, "tracks", [])
        } for r in genres_recs]

        meta = get_explore_cache_meta()

        return {
            "trending": trending,
            "billboard": billboard,
            "new_releases": new_releases,
            "genres": genres,
            "source": "pocketbase",
            "last_updated": meta.get("date_str", ""),
            "last_fetch": meta.get("last_fetch", 0)
        }
    except Exception as e:
        print(f"Warning: Failed to load explore data from PocketBase: {e}")
        return None


def load_explore_from_json() -> Optional[Dict[str, Any]]:
    """Loads curated Explore data from local JSON fallback files."""
    paths_to_check = [
        "/var/mobile/music-data",
        os.path.join(PROJECT_DIR, "music-data")
    ]
    for d in paths_to_check:
        trending_file = os.path.join(d, "trending.json")
        billboard_file = os.path.join(d, "billboard.json")
        new_rel_file = os.path.join(d, "new_releases.json")
        genres_file = os.path.join(d, "genres.json")

        if os.path.exists(trending_file) and os.path.exists(billboard_file):
            try:
                with open(trending_file, "r", encoding="utf-8") as f:
                    trending = json.load(f)
                with open(billboard_file, "r", encoding="utf-8") as f:
                    billboard = json.load(f)
                new_releases = []
                if os.path.exists(new_rel_file):
                    with open(new_rel_file, "r", encoding="utf-8") as f:
                        new_releases = json.load(f)
                genres = []
                if os.path.exists(genres_file):
                    with open(genres_file, "r", encoding="utf-8") as f:
                        genres = json.load(f)

                meta = get_explore_cache_meta()

                return {
                    "trending": trending,
                    "billboard": billboard,
                    "new_releases": new_releases,
                    "genres": genres,
                    "source": "fallback_json",
                    "last_updated": meta.get("date_str", ""),
                    "last_fetch": meta.get("last_fetch", 0)
                }
            except Exception as e:
                print(f"Warning: Failed to load explore JSON from {d}: {e}")
    return None


def load_explore_legacy() -> Dict[str, Any]:
    """Emergency fallback to legacy YouTube Music scraper."""
    trending = music_api.get_trending_tracks(limit=25)
    new_releases = music_api.get_new_releases(limit=20)
    moods_data = music_api.get_moods_and_genres()
    return {
        "trending": trending,
        "billboard": [],
        "new_releases": new_releases,
        "genres": moods_data.get("genres", []),
        "source": "legacy"
    }


# -------------------------------------------------------------
# Music Discovery & Search Endpoints
# -------------------------------------------------------------
@app.get("/api/home")
def get_home_feed():
    """Fetches daily curated Explore feed (Trending, Billboard Hot 100, New Releases, Genres) from PocketBase with JSON fallback and in-memory caching."""
    # Self-healing check: if charts are older than 24 hours, fire background fetcher
    trigger_daily_fetch_if_stale(force=False)

    now = time.time()
    with _CACHE_LOCK:
        if "home" in _CACHE and _CACHE["home"]["expires_at"] > now:
            return _CACHE["home"]["data"]

    data = load_explore_from_pocketbase()
    if not data:
        data = load_explore_from_json()
    if not data:
        data = load_explore_legacy()

    with _CACHE_LOCK:
        _CACHE["home"] = {
            "data": data,
            "expires_at": now + 600,
        }
    return data


@app.post("/api/home/refresh")
def force_refresh_home_feed():
    """Forces an immediate on-demand upstream fetch for Trending, Billboard, and New Releases."""
    triggered = trigger_daily_fetch_if_stale(force=True)
    return {
        "success": True,
        "triggered": triggered,
        "message": "Fresh music fetch initiated in background. Refreshing charts..." if triggered else "A chart fetch is already in progress."
    }


@app.get("/api/genre/{genre_id}/tracks")
def get_genre_tracks(genre_id: int):
    """Fetches curated top tracks for a Deezer genre."""
    try:
        pb = database.get_pb()
        genre = pb.collection("genres").get_first_list_item(f"genre_id = {genre_id}")
        if genre and getattr(genre, "tracks", None):
            return {"genre_id": genre_id, "name": genre.name, "tracks": genre.tracks}
    except Exception:
        pass

    data = load_explore_from_json()
    if data and "genres" in data:
        for g in data["genres"]:
            if g.get("genre_id") == genre_id:
                return {"genre_id": genre_id, "name": g.get("name"), "tracks": g.get("tracks", [])}

    raise HTTPException(status_code=404, detail="Genre not found")


@app.get("/api/resolve")
@limiter.limit("60/minute")
def resolve_track(
    request: Request,
    title: str = Query(..., min_length=1, max_length=200),
    artist: str = Query("", max_length=200)
):
    """Resolves an external song title + artist to a playable YouTube track ID with in-memory caching."""
    clean_title = title.strip()
    clean_artist = artist.strip()
    cache_key = f"{clean_title.lower()}::{clean_artist.lower()}"

    with _RESOLVE_LOCK:
        if cache_key in _RESOLVE_CACHE:
            return {"track": _RESOLVE_CACHE[cache_key], "cached": True}

    query = f"{clean_title} {clean_artist}".strip()
    resolved_track = None

    # 1. Fast path: search YouTube Music songs (~250-300ms)
    try:
        results = music_api.search_music(query, filter_type="songs", limit=3)
        if results:
            first = results[0]
            resolved_track = {
                "id": first["id"],
                "title": clean_title,
                "artist": clean_artist or first.get("artist", ""),
                "thumbnail": first.get("thumbnail") or "",
                "duration": first.get("duration", "--:--")
            }
    except Exception as e:
        print(f"music_api resolve failed for '{query}': {e}")

    # 2. Resilient fallback: yt-dlp flat extract (~2s)
    if not resolved_track:
        try:
            import yt_dlp
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'ios', 'mweb']}}
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(f"ytsearch1:{query}", download=False)
                if res and 'entries' in res and len(res['entries']) > 0:
                    entry = res['entries'][0]
                    resolved_track = {
                        "id": entry["id"],
                        "title": clean_title,
                        "artist": clean_artist or "YouTube",
                        "thumbnail": entry.get("thumbnails", [{}])[-1].get("url", "") if entry.get("thumbnails") else "",
                        "duration": "--:--"
                    }
        except Exception as e:
            print(f"yt-dlp resolve failed for '{query}': {e}")

    if not resolved_track:
        raise HTTPException(status_code=404, detail="Track could not be resolved on YouTube")

    with _RESOLVE_LOCK:
        _RESOLVE_CACHE[cache_key] = resolved_track

    return {"track": resolved_track, "cached": False}


@app.get("/api/search")
@limiter.limit("45/minute")
def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=150),
    filter_type: str = Query("songs", max_length=30)
):
    """Searches YouTube Music for tracks or playlists."""
    results = music_api.search_music(q, filter_type=filter_type, limit=25)
    return {"query": q, "results": results}


@app.get("/api/search/suggestions")
def get_search_suggestions(q: str = Query(..., min_length=1)):
    """Returns autocomplete suggestions as user types in search bar."""
    suggestions = music_api.get_search_suggestions(q)
    return {"query": q, "suggestions": suggestions}


@app.get("/api/mood-playlists")
def get_mood_playlists(params: str = Query(...)):
    """Fetches playlists for a given mood/genre."""
    playlists = music_api.get_category_playlists(params, limit=16)
    return {"playlists": playlists}


# -------------------------------------------------------------
# Playlists & Favorites Endpoints (Multi-Tenant)
# -------------------------------------------------------------
@app.get("/api/playlist/{playlist_id}")
def get_playlist(
    playlist_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    """Fetches full tracks for a playlist (User Custom / PocketBase, System, or YouTube Music)."""
    user = get_current_user_optional(authorization)
    uid = user["uid"] if user else None

    # 1. Check if it's a PocketBase user playlist or system virtual playlist
    pl = playlist_manager.get_playlist(playlist_id, uid)
    if pl is not None:
        return pl

    # 2. Otherwise query YouTube Music
    data = music_api.get_playlist_details(playlist_id)
    return data


@app.get("/api/user-playlists")
def get_user_playlists(authorization: Optional[str] = Header(None)):
    """Returns playlists accessible to the user (Personal + Liked + Shared Downloaded)."""
    user = get_current_user_optional(authorization)
    uid = user["uid"] if user else None
    return {"playlists": playlist_manager.get_all_playlists(uid)}


@app.post("/api/user-playlists")
def create_user_playlist(req: CreatePlaylistRequest, authorization: Optional[str] = Header(None)):
    """Creates a new custom user playlist (requires login)."""
    user = get_current_user(authorization)
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Playlist name cannot be empty")
    pl = playlist_manager.create_playlist(user["uid"], req.name.strip()[:100], (req.description or "").strip()[:500])
    return pl


@app.get("/api/user-playlists/{playlist_id}")
def get_single_user_playlist(
    playlist_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    """Returns a specific custom playlist."""
    user = get_current_user_optional(authorization)
    uid = user["uid"] if user else None
    pl = playlist_manager.get_playlist(playlist_id, uid)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return pl


@app.post("/api/user-playlists/{playlist_id}/tracks")
def add_track_to_user_playlist(
    track: AddTrackRequest,
    playlist_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    """Adds a track to a custom user playlist (requires authentication)."""
    user = get_current_user(authorization)
    success = playlist_manager.add_track_to_playlist(playlist_id, to_dict(track), user["uid"])
    if not success:
        raise HTTPException(status_code=403, detail="Playlist not found or not owned")
    return {"message": "Track added to playlist successfully"}


@app.delete("/api/user-playlists/{playlist_id}/tracks/{track_id}")
def remove_track_from_user_playlist(
    playlist_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    track_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    """Removes a track from a custom user playlist (requires authentication)."""
    user = get_current_user(authorization)
    success = playlist_manager.remove_track_from_playlist(playlist_id, track_id, user["uid"])
    if not success:
        raise HTTPException(status_code=403, detail="Failed to remove track: playlist not found or not owned")
    return {"success": success}


@app.delete("/api/user-playlists/{playlist_id}")
def delete_user_playlist(
    playlist_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    """Deletes a custom user playlist."""
    user = get_current_user(authorization)
    success = playlist_manager.delete_playlist(playlist_id, user["uid"])
    if not success:
        raise HTTPException(status_code=403, detail="Failed to delete playlist: playlist not found or not owned")
    return {"success": success}


# -------------------------------------------------------------
# User Favorites / Liked Songs Endpoints
# -------------------------------------------------------------
@app.get("/api/favorites")
def get_favorites(authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    tracks = playlist_manager.get_user_liked_tracks(user["uid"])
    return {"tracks": tracks}


@app.post("/api/favorites")
def add_favorite(track: FavoriteRequest, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    success = playlist_manager.like_track(user["uid"], to_dict(track))
    return {"success": success}


@app.delete("/api/favorites/{track_id}")
def delete_favorite(
    track_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    user = get_current_user(authorization)
    success = playlist_manager.unlike_track(user["uid"], track_id)
    return {"success": success}


@app.get("/api/favorites/check/{track_id}")
def check_favorite(
    track_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    user = get_current_user_optional(authorization)
    if not user:
        return {"liked": False}
    return {"liked": playlist_manager.is_track_liked(user["uid"], track_id)}


# -------------------------------------------------------------
# User Library / Central Downloaded & Saved Tracks Endpoints
# -------------------------------------------------------------
@app.get("/api/library/tracks")
def get_user_library(authorization: Optional[str] = Header(None)):
    """Returns all tracks in user's central library from PocketBase."""
    user = get_current_user(authorization)
    tracks = playlist_manager.get_user_library_tracks(user["uid"])
    return {"tracks": tracks, "count": len(tracks)}


@app.post("/api/library/tracks")
def add_user_library_track(track: LibraryTrackRequest, authorization: Optional[str] = Header(None)):
    """Adds a track to user's central library in PocketBase."""
    user = get_current_user(authorization)
    success = playlist_manager.add_track_to_library(user["uid"], to_dict(track))
    return {"success": success}


@app.delete("/api/library/tracks/{track_id}")
def remove_user_library_track(
    track_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    """Removes a track from user's central library in PocketBase."""
    user = get_current_user(authorization)
    success = playlist_manager.remove_track_from_library(user["uid"], track_id)
    return {"success": success}


@app.get("/api/library/check/{track_id}")
def check_user_library_track(
    track_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    authorization: Optional[str] = Header(None)
):
    """Checks if a track is in user's central library."""
    user = get_current_user_optional(authorization)
    if not user:
        return {"saved": False}
    return {"saved": playlist_manager.is_track_in_library(user["uid"], track_id)}


# -------------------------------------------------------------
# Audio Playback, Radio & Download Endpoints
# -------------------------------------------------------------
@app.get("/api/radio/{video_id}")
def get_radio(video_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{11}$")):
    """Generates continuous radio recommendations based on a track."""
    tracks = music_api.get_radio_tracks(video_id, limit=35)
    return {"tracks": tracks}


@app.get("/api/stream/{video_id}")
def get_stream(video_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{11}$")):
    """Resolves audio stream URL or local downloaded file path."""
    try:
        data = stream_downloader.get_cached_or_local_stream(video_id)
        return data
    except Exception as e:
        print(f"Stream resolution error for {video_id}: {e}", file=sys.stderr)
        raise HTTPException(status_code=500, detail="Could not resolve audio stream")


@app.api_route("/api/download/stream/{video_id}", methods=["GET", "HEAD"])
def stream_download_proxy(
    request: Request,
    video_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{11}$")
):
    """
    High-performance audio streaming endpoint.
    1. Serves directly from server SSD cache (/var/mobile/server/data/music_cache) if present (<10ms).
    2. Streams from upstream Google CDN forwarding HTTP Range headers for smooth Safari seeking.
    3. Caches full streams to disk with worker-isolated temporary files to prevent race conditions.
    """
    try:
        # Check if local cached audio exists first
        local_path = stream_downloader.get_cached_audio_path(video_id)
        if local_path and os.path.isfile(local_path):
            if local_path.endswith(".mp3"):
                media_type = "audio/mpeg"
            elif local_path.endswith(".webm"):
                media_type = "audio/webm"
            elif local_path.endswith(".opus"):
                media_type = "audio/ogg"
            else:
                media_type = "audio/mp4"
            # Return inline audio stream without forcing attachment download
            return FileResponse(local_path, media_type=media_type)

        data = stream_downloader.get_cached_or_local_stream(video_id)
        stream_url = data.get("url")
        if not stream_url:
            raise HTTPException(status_code=404, detail="Could not resolve stream URL")

        upstream_headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"
        }
        client_range = request.headers.get("range")
        if client_range:
            upstream_headers["Range"] = client_range

        # Stream directly from upstream Google CDN
        req = requests.get(
            stream_url,
            stream=True,
            timeout=20,
            headers=upstream_headers
        )
        if req.status_code not in (200, 206):
            raise HTTPException(status_code=req.status_code, detail="Upstream stream fetch failed")

        content_length = req.headers.get("Content-Length")
        content_type = req.headers.get("Content-Type", "audio/mp4")
        content_range = req.headers.get("Content-Range")

        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        }
        if content_length:
            headers["Content-Length"] = content_length
        if content_range:
            headers["Content-Range"] = content_range

        if request.method == "HEAD":
            req.close()
            return Response(status_code=req.status_code, headers=headers, media_type=content_type)

        import uuid
        worker_id = uuid.uuid4().hex[:6]
        part_path = os.path.join(stream_downloader.CACHE_DIR, f".part_{video_id}_{worker_id}.m4a")
        final_path = os.path.join(stream_downloader.CACHE_DIR, f"{video_id}.m4a")

        # Determine if this stream represents the 100% full audio track (never cache a partial range slice)
        expected_total_bytes = None
        is_full_stream = False

        if req.status_code == 200:
            if content_length and content_length.isdigit():
                expected_total_bytes = int(content_length)
                is_full_stream = True
        elif req.status_code == 206 and content_range:
            # Format: bytes start-end/total
            cr_match = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+)$", content_range.strip())
            if cr_match:
                s_byte = int(cr_match.group(1))
                e_byte = int(cr_match.group(2))
                tot_bytes = int(cr_match.group(3))
                # Only a full stream if it starts at 0 and delivers up to total - 1
                if s_byte == 0 and (e_byte + 1) == tot_bytes:
                    expected_total_bytes = tot_bytes
                    is_full_stream = True

        can_cache_locally = (
            is_full_stream and 
            expected_total_bytes is not None and 
            expected_total_bytes > 50000 and 
            (not client_range or client_range.strip() in ("", "bytes=0-"))
        )

        def iter_chunks():
            f = None
            total_written = 0
            try:
                if can_cache_locally and not os.path.isfile(final_path):
                    try:
                        f = open(part_path, "wb")
                    except Exception:
                        f = None

                for chunk in req.iter_content(chunk_size=65536):
                    if chunk:
                        if f:
                            try:
                                f.write(chunk)
                                total_written += len(chunk)
                            except Exception:
                                pass
                        yield chunk

                if f:
                    f.close()
                    f = None
                    # Verify stream was 100% completed to the exact expected length
                    if can_cache_locally and expected_total_bytes and total_written == expected_total_bytes:
                        # Validate audio container with mutagen before promoting to permanent cache
                        is_valid = True
                        try:
                            import mutagen
                            audio_meta = mutagen.File(part_path)
                            if audio_meta is None or getattr(getattr(audio_meta, "info", None), "length", 0) <= 0:
                                is_valid = False
                        except Exception:
                            is_valid = False

                        if is_valid:
                            try:
                                os.replace(part_path, final_path)
                            except Exception:
                                pass
                        else:
                            try:
                                if os.path.exists(part_path):
                                    os.remove(part_path)
                            except Exception:
                                pass
                    else:
                        try:
                            if os.path.exists(part_path):
                                os.remove(part_path)
                        except Exception:
                            pass
            finally:
                if f:
                    try:
                        f.close()
                    except Exception:
                        pass
                if os.path.exists(part_path) and not os.path.exists(final_path):
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                req.close()

        return StreamingResponse(
            iter_chunks(),
            status_code=req.status_code,
            media_type=content_type,
            headers=headers
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"stream_download_proxy error: {e}", file=sys.stderr)
        raise HTTPException(status_code=500, detail="Audio streaming error")


@app.post("/api/download/{video_id}")
@limiter.limit("20/minute")
def trigger_download(
    request: Request,
    video_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]{11}$"),
    title: str = Query("", max_length=200),
    artist: str = Query("", max_length=200),
    authorization: Optional[str] = Header(None)
):
    """Queues a track for MP3 download with ID3 tags and cover art (requires auth)."""
    user = get_current_user(authorization)
    stream_downloader.download_track(video_id, title=title, artist=artist, user_id=user["uid"])
    return {"message": "Download initiated", "videoId": video_id}


@app.get("/api/downloads/status")
def get_downloads_status(authorization: Optional[str] = Header(None)):
    """Returns status of ongoing and completed downloads scoped strictly to caller."""
    user = get_current_user_optional(authorization)
    uid = user["uid"] if user else None
    with stream_downloader._DOWNLOAD_LOCK:
        return {
            vid: {
                "status": s.get("status"),
                "percent": s.get("percent", 0.0),
                "title": s.get("title", ""),
                "artist": s.get("artist", ""),
                "error": s.get("error")
            }
            for vid, s in stream_downloader.DOWNLOAD_STATUS.items()
            if (uid is None or s.get("user_id") == uid or s.get("user_id") is None)
        }


@app.get("/api/download/file/{video_id}")
def serve_transient_download_file(video_id: str):
    """
    Streams the transient MP3 file to the client phone and immediately deletes
    it from the iPhone 7 disk once the stream is fully delivered to the client.
    """
    file_path = stream_downloader.get_transient_track_path(video_id)
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File expired or not found")

    filename = os.path.basename(file_path)

    def cleanup():
        try:
            stream_downloader.cleanup_transient_file(video_id)
            print(f"[Spoti Cleanup] Auto-deleted transient file {filename} after delivery.", flush=True)
        except Exception as e:
            print(f"[Spoti Cleanup Error] Failed to delete {filename}: {e}", flush=True)

    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        filename=filename,
        background=BackgroundTask(cleanup)
    )


@app.get("/api/library")
def get_library():
    """Returns all offline downloaded songs."""
    tracks = library_manager.get_library_tracks()
    return {"tracks": tracks}


@app.get("/api/library/file/{filename}")
def serve_library_file(filename: str):
    """Streams a downloaded audio file from the local music library securely."""
    safe_name = os.path.basename(filename)
    base_dir = os.path.realpath(stream_downloader.MUSIC_DIR)
    file_path = os.path.realpath(os.path.join(base_dir, safe_name))

    # Path traversal guard: must reside strictly inside MUSIC_DIR
    if not file_path.startswith(base_dir) or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    media_type = "audio/mpeg" if safe_name.lower().endswith(".mp3") else "audio/mp4"
    return FileResponse(file_path, media_type=media_type, filename=safe_name)


# Mount static folder
static_dir = os.path.join(PROJECT_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/sw.js")
def serve_service_worker():
    sw_file = os.path.join(static_dir, "sw.js")
    if os.path.isfile(sw_file):
        return FileResponse(
            sw_file,
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )
    raise HTTPException(status_code=404, detail="sw.js not found")


@app.get("/manifest.json")
def serve_manifest():
    manifest_file = os.path.join(static_dir, "manifest.json")
    if os.path.isfile(manifest_file):
        return FileResponse(manifest_file, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="manifest.json not found")


@app.api_route("/", methods=["GET", "HEAD"])
def serve_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.isfile(index_file):
        return FileResponse(index_file)
    return JSONResponse({"status": "Server running", "message": "index.html not found"})



if __name__ == "__main__":
    import uvicorn
    local_ip = get_local_ip()
    print("=" * 60, flush=True)
    print("⚡ SPOTI - Private Music Player & Downloader (Mobile PWA)", flush=True)
    print(f"👉 Local Access:   http://localhost:8000", flush=True)
    print(f"📱 Phone on Wi-Fi: http://{local_ip}:8000", flush=True)
    print("=" * 60, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8000)
