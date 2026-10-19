import os
import sys
import io
import time
import socket
import threading
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Query, HTTPException, Header
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
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

app = FastAPI(title="Spoti")

# Initialize SQLite database
database.init_db()

# In-memory TTL cache for high performance
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()

# Allow requests from local network devices / phones
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_current_user_optional(authorization: Optional[str] = Header(None)) -> Optional[Dict[str, Any]]:
    if not authorization:
        return None
    token = authorization
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    return auth.verify_token(token)


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


# -------------------------------------------------------------
# System & Diagnostics Endpoints
# -------------------------------------------------------------
@app.get("/api/info")
def get_server_info():
    ip = get_local_ip()
    return {
        "local_ip": ip,
        "port": 8000,
        "url": f"http://{ip}:8000",
        "desktop_url": "http://localhost:8000",
    }


@app.get("/api/qr")
def get_qr_code():
    """Generates a QR code PNG pointing to the server's local network URL."""
    ip = get_local_ip()
    target_url = f"http://{ip}:8000"
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
def register(req: AuthRequest):
    try:
        data = auth.register_user(req.username, req.password)
        # Migrate any legacy playlists to the user
        playlist_manager._migrate_legacy_json(data["id"])
        return data
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login")
def login(req: AuthRequest):
    try:
        data = auth.authenticate_user(req.username, req.password)
        return data
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


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


# -------------------------------------------------------------
# Music Discovery & Search Endpoints
# -------------------------------------------------------------
@app.get("/api/home")
def get_home_feed():
    """Fetches trending songs and mood categories with 15-minute in-memory caching."""
    now = time.time()
    with _CACHE_LOCK:
        if "home" in _CACHE and _CACHE["home"]["expires_at"] > now:
            return _CACHE["home"]["data"]

    trending = music_api.get_trending_tracks(limit=25)
    moods_data = music_api.get_moods_and_genres()
    data = {
        "trending": trending,
        "moods": moods_data.get("moods", []),
        "genres": moods_data.get("genres", []),
    }
    with _CACHE_LOCK:
        _CACHE["home"] = {
            "data": data,
            "expires_at": now + 900,
        }
    return data


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), filter_type: str = Query("songs")):
    """Searches YouTube Music for tracks or playlists."""
    results = music_api.search_music(q, filter_type=filter_type, limit=25)
    return {"query": q, "results": results}


@app.get("/api/mood-playlists")
def get_mood_playlists(params: str = Query(...)):
    """Fetches playlists for a given mood/genre."""
    playlists = music_api.get_category_playlists(params, limit=16)
    return {"playlists": playlists}


# -------------------------------------------------------------
# Playlists & Favorites Endpoints (Multi-Tenant)
# -------------------------------------------------------------
@app.get("/api/playlist/{playlist_id}")
def get_playlist(playlist_id: str, authorization: Optional[str] = Header(None)):
    """Fetches full tracks for a playlist (YouTube Music, Shared Offline, or User Custom)."""
    user = get_current_user_optional(authorization)
    uid = user["uid"] if user else None

    if playlist_id.startswith("user_pl_"):
        pl = playlist_manager.get_playlist(playlist_id, uid)
        if pl:
            return pl
        raise HTTPException(status_code=404, detail="Playlist not found")
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
    pl = playlist_manager.create_playlist(user["uid"], req.name, req.description or "")
    return pl


@app.get("/api/user-playlists/{playlist_id}")
def get_single_user_playlist(playlist_id: str, authorization: Optional[str] = Header(None)):
    """Returns a specific custom playlist."""
    user = get_current_user_optional(authorization)
    uid = user["uid"] if user else None
    pl = playlist_manager.get_playlist(playlist_id, uid)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return pl


@app.post("/api/user-playlists/{playlist_id}/tracks")
def add_track_to_user_playlist(playlist_id: str, track: AddTrackRequest, authorization: Optional[str] = Header(None)):
    """Adds a track to a custom user playlist."""
    user = get_current_user_optional(authorization)
    uid = user["uid"] if user else None
    success = playlist_manager.add_track_to_playlist(playlist_id, track.dict(), uid)
    if not success:
        raise HTTPException(status_code=404, detail="Playlist not found or not owned")
    return {"message": "Track added to playlist successfully"}


@app.delete("/api/user-playlists/{playlist_id}/tracks/{track_id}")
def remove_track_from_user_playlist(playlist_id: str, track_id: str, authorization: Optional[str] = Header(None)):
    """Removes a track from a custom user playlist."""
    user = get_current_user_optional(authorization)
    uid = user["uid"] if user else None
    success = playlist_manager.remove_track_from_playlist(playlist_id, track_id, uid)
    return {"success": success}


@app.delete("/api/user-playlists/{playlist_id}")
def delete_user_playlist(playlist_id: str, authorization: Optional[str] = Header(None)):
    """Deletes a custom user playlist."""
    user = get_current_user(authorization)
    success = playlist_manager.delete_playlist(playlist_id, user["uid"])
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
    success = playlist_manager.like_track(user["uid"], track.dict())
    return {"success": success}


@app.delete("/api/favorites/{track_id}")
def delete_favorite(track_id: str, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    success = playlist_manager.unlike_track(user["uid"], track_id)
    return {"success": success}


@app.get("/api/favorites/check/{track_id}")
def check_favorite(track_id: str, authorization: Optional[str] = Header(None)):
    user = get_current_user_optional(authorization)
    if not user:
        return {"liked": False}
    return {"liked": playlist_manager.is_track_liked(user["uid"], track_id)}


# -------------------------------------------------------------
# Audio Playback, Radio & Download Endpoints
# -------------------------------------------------------------
@app.get("/api/radio/{video_id}")
def get_radio(video_id: str):
    """Generates continuous radio recommendations based on a track."""
    tracks = music_api.get_radio_tracks(video_id, limit=35)
    return {"tracks": tracks}


@app.get("/api/stream/{video_id}")
def get_stream(video_id: str):
    """Resolves audio stream URL or local downloaded file path."""
    try:
        data = stream_downloader.get_cached_or_local_stream(video_id)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/download/{video_id}")
def trigger_download(video_id: str, title: str = "", artist: str = ""):
    """Queues a track for MP3 download with ID3 tags and cover art."""
    stream_downloader.download_track(video_id, title=title, artist=artist)
    return {"message": "Download initiated", "videoId": video_id}


@app.get("/api/downloads/status")
def get_downloads_status():
    """Returns status of ongoing and completed downloads."""
    return stream_downloader.DOWNLOAD_STATUS


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
    """Streams a downloaded audio file from the local music library."""
    file_path = os.path.join(stream_downloader.MUSIC_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="audio/mpeg", filename=filename)


# Mount static folder
static_dir = os.path.join(PROJECT_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
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
