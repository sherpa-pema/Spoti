import os
import sys
import re
import time
import threading
from typing import Dict, Any, Optional

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

MUSIC_DIR = os.path.join(PROJECT_DIR, "music_library")
os.makedirs(MUSIC_DIR, exist_ok=True)

TRANSIENT_DIR = "/tmp/spoti_transient"
os.makedirs(TRANSIENT_DIR, exist_ok=True)

import yt_dlp
from downloader import find_ffmpeg

# In-memory stream URL cache (video_id -> {'url': str, 'expires_at': float})
_STREAM_CACHE: Dict[str, Dict[str, Any]] = {}
_STREAM_CACHE_LOCK = threading.Lock()

# Download status tracker (video_id -> dict)
DOWNLOAD_STATUS: Dict[str, Dict[str, Any]] = {}
_DOWNLOAD_LOCK = threading.Lock()


def get_safe_filename(name: str) -> str:
    """Removes invalid filesystem characters."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def get_cached_or_local_stream(video_id: str) -> Dict[str, Any]:
    """Fetches direct stream URL with caching."""
    now = time.time()
    with _STREAM_CACHE_LOCK:
        if video_id in _STREAM_CACHE:
            item = _STREAM_CACHE[video_id]
            if item["expires_at"] > now:
                return {
                    "source": "stream",
                    "url": item["url"],
                }

    # Extract fresh audio stream URL via yt-dlp
    ffmpeg_dir = find_ffmpeg()
    ydl_opts: Dict[str, Any] = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb']
            }
        },
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    if ffmpeg_dir:
        ydl_opts['ffmpeg_location'] = ffmpeg_dir

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        stream_url = info.get("url")
        if not stream_url:
            raise ValueError("Could not extract audio stream URL.")

        with _STREAM_CACHE_LOCK:
            # Cache stream URL for 2 hours
            _STREAM_CACHE[video_id] = {
                "url": stream_url,
                "expires_at": now + 7200,
            }

        return {
            "source": "stream",
            "url": stream_url,
        }


def get_local_track_path(video_id: str) -> Optional[str]:
    """Finds an existing downloaded MP3 file in legacy MUSIC_DIR or TRANSIENT_DIR."""
    for search_dir in (TRANSIENT_DIR, MUSIC_DIR):
        if os.path.isdir(search_dir):
            for fname in os.listdir(search_dir):
                if f"[{video_id}].mp3" in fname or f"[{video_id}].m4a" in fname:
                    return os.path.join(search_dir, fname)
    return None


def get_transient_track_path(video_id: str) -> Optional[str]:
    """Finds the ephemeral MP3 file in TRANSIENT_DIR ready for streaming to client."""
    with _DOWNLOAD_LOCK:
        item = DOWNLOAD_STATUS.get(video_id)
        if item and item.get("file_path") and os.path.isfile(item["file_path"]):
            return item["file_path"]
    if os.path.isdir(TRANSIENT_DIR):
        for fname in os.listdir(TRANSIENT_DIR):
            if f"[{video_id}].mp3" in fname or f"[{video_id}].m4a" in fname:
                return os.path.join(TRANSIENT_DIR, fname)
    return None


def cleanup_transient_file(video_id: str):
    """Deletes transient file from iPhone 7 disk immediately and clears status."""
    file_path = get_transient_track_path(video_id)
    if file_path and os.path.isfile(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            print(f"Error removing transient file {file_path}: {e}", file=sys.stderr)
    # Clean up any lingering files with video_id in TRANSIENT_DIR
    if os.path.isdir(TRANSIENT_DIR):
        for fname in os.listdir(TRANSIENT_DIR):
            if video_id in fname:
                try:
                    os.remove(os.path.join(TRANSIENT_DIR, fname))
                except Exception:
                    pass
    with _DOWNLOAD_LOCK:
        DOWNLOAD_STATUS.pop(video_id, None)


def is_downloaded(video_id: str) -> bool:
    return get_local_track_path(video_id) is not None


def download_track(video_id: str, title: str = "", artist: str = ""):
    """Spawns background download thread for the song in transient storage."""
    with _DOWNLOAD_LOCK:
        if video_id in DOWNLOAD_STATUS and DOWNLOAD_STATUS[video_id]["status"] in ("downloading", "ready_for_transfer"):
            return  # Already in progress or ready
        DOWNLOAD_STATUS[video_id] = {
            "status": "downloading",
            "percent": 0.0,
            "title": title or video_id,
            "artist": artist,
            "file_path": None,
            "created_at": time.time(),
            "error": None,
        }

    thread = threading.Thread(
        target=_download_worker,
        args=(video_id, title, artist),
        daemon=True
    )
    thread.start()


def _download_worker(video_id: str, title: str, artist: str):
    ffmpeg_dir = find_ffmpeg()
    
    def hook(d):
        status = d.get('status')
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
            pct = round((downloaded / total) * 100, 1) if total else 0.0
            with _DOWNLOAD_LOCK:
                if video_id in DOWNLOAD_STATUS:
                    DOWNLOAD_STATUS[video_id]["percent"] = pct
        elif status == 'finished':
            with _DOWNLOAD_LOCK:
                if video_id in DOWNLOAD_STATUS:
                    DOWNLOAD_STATUS[video_id]["percent"] = 99.0

    ydl_opts: Dict[str, Any] = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(TRANSIENT_DIR, '%(title)s [%(id)s].%(ext)s'),
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb']
            }
        },
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            },
        ],
        'writethumbnail': False,
        'progress_hooks': [hook],
        'quiet': True,
        'no_warnings': True,
    }

    if ffmpeg_dir:
        ydl_opts['ffmpeg_location'] = ffmpeg_dir

    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Clean up any standalone thumbnail jpgs left behind in TRANSIENT_DIR
        for fname in os.listdir(TRANSIENT_DIR):
            if fname.endswith(('.webp', '.jpg', '.png')) and video_id in fname:
                try:
                    os.remove(os.path.join(TRANSIENT_DIR, fname))
                except Exception:
                    pass

        # Locate final MP3
        final_file = None
        for fname in os.listdir(TRANSIENT_DIR):
            if f"[{video_id}].mp3" in fname or f"[{video_id}].m4a" in fname:
                final_file = os.path.join(TRANSIENT_DIR, fname)
                break

        with _DOWNLOAD_LOCK:
            DOWNLOAD_STATUS[video_id]["status"] = "ready_for_transfer"
            DOWNLOAD_STATUS[video_id]["percent"] = 100.0
            DOWNLOAD_STATUS[video_id]["file_path"] = final_file
            DOWNLOAD_STATUS[video_id]["filename"] = os.path.basename(final_file) if final_file else f"{video_id}.mp3"
            DOWNLOAD_STATUS[video_id]["created_at"] = time.time()
    except Exception as e:
        with _DOWNLOAD_LOCK:
            DOWNLOAD_STATUS[video_id]["status"] = "error"
            DOWNLOAD_STATUS[video_id]["error"] = str(e)


def _transient_janitor_loop():
    """Background janitor that prunes any stale transient files older than 15 mins."""
    while True:
        try:
            time.sleep(180)
            now = time.time()
            if os.path.isdir(TRANSIENT_DIR):
                for fname in os.listdir(TRANSIENT_DIR):
                    fpath = os.path.join(TRANSIENT_DIR, fname)
                    if os.path.isfile(fpath):
                        if now - os.path.getmtime(fpath) > 900:  # 15 minutes
                            try:
                                os.remove(fpath)
                            except Exception:
                                pass
            with _DOWNLOAD_LOCK:
                stale_keys = [
                    vid for vid, info in DOWNLOAD_STATUS.items()
                    if now - info.get("created_at", 0) > 900
                ]
                for vid in stale_keys:
                    DOWNLOAD_STATUS.pop(vid, None)
        except Exception:
            pass


# Start background janitor
_janitor_thread = threading.Thread(target=_transient_janitor_loop, daemon=True)
_janitor_thread.start()
