import os
import sys
import re
import time
import shutil
import threading
from typing import Dict, Any, Optional
from cachetools import TTLCache
import yt_dlp

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

DATA_DIR = os.environ.get("SPOTI_DATA_DIR", "/var/mobile/server/data")
MUSIC_DIR = os.environ.get("SPOTI_MUSIC_DIR", os.path.join(PROJECT_DIR, "music_library"))
os.makedirs(MUSIC_DIR, exist_ok=True)

SERVER_CACHE_DIR = os.path.join(DATA_DIR, "music_cache") if os.path.exists(DATA_DIR) else os.path.join(PROJECT_DIR, "music_cache")
CACHE_DIR = os.environ.get("SPOTI_CACHE_DIR", SERVER_CACHE_DIR)
os.makedirs(CACHE_DIR, exist_ok=True)

TRANSIENT_DIR = os.environ.get("SPOTI_TRANSIENT_DIR", "/tmp/spoti_transient")
try:
    os.makedirs(TRANSIENT_DIR, exist_ok=True)
    os.chmod(TRANSIENT_DIR, 0o700)
except Exception:
    pass

# In-memory stream URL cache: video_id -> {'url': str, 'expires_at': float} (bounded TTL)
_STREAM_CACHE: TTLCache = TTLCache(maxsize=150, ttl=7200)
_STREAM_CACHE_LOCK = threading.Lock()

# Deduplication locks for concurrent stream extractions
_IN_FLIGHT_STREAMS: Dict[str, threading.Event] = {}
_IN_FLIGHT_RESULTS: Dict[str, Dict[str, Any]] = {}
_IN_FLIGHT_LOCK = threading.Lock()

# Download status tracker (video_id -> dict)
DOWNLOAD_STATUS: Dict[str, Dict[str, Any]] = {}
_DOWNLOAD_LOCK = threading.Lock()


def find_ffmpeg() -> Optional[str]:
    """Finds ffmpeg executable in PATH or standard iOS/macOS/Linux directories."""
    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path:
        return os.path.dirname(ffmpeg_in_path)
    for cand_dir in ["/var/jb/usr/bin", "/usr/local/bin", "/usr/bin", "/var/mobile/server/bin", os.path.join(PROJECT_DIR, "bin")]:
        cand_bin = os.path.join(cand_dir, "ffmpeg")
        if os.path.isfile(cand_bin) and os.access(cand_bin, os.X_OK):
            return cand_dir
    return None


def get_safe_filename(name: str) -> str:
    """Removes invalid filesystem characters."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def get_cached_audio_path(video_id: str) -> Optional[str]:
    """Finds a cached audio file in CACHE_DIR, TRANSIENT_DIR, or MUSIC_DIR with O(1) checks."""
    if not video_id or not re.match(r"^[A-Za-z0-9_-]{1,64}$", video_id):
        return None

    exts = (".m4a", ".mp3", ".webm", ".opus")
    for search_dir in (CACHE_DIR, TRANSIENT_DIR, MUSIC_DIR):
        if not os.path.isdir(search_dir):
            continue
        for ext in exts:
            # 1. Exact name: {video_id}.{ext}
            cand = os.path.join(search_dir, f"{video_id}{ext}")
            if os.path.isfile(cand):
                try:
                    if os.path.getsize(cand) > 10000:
                        return cand
                except OSError:
                    pass
            # 2. Bracketed name: [{video_id}].{ext}
            cand_bracket = os.path.join(search_dir, f"[{video_id}]{ext}")
            if os.path.isfile(cand_bracket):
                try:
                    if os.path.getsize(cand_bracket) > 10000:
                        return cand_bracket
                except OSError:
                    pass
    return None


def get_cached_or_local_stream(video_id: str) -> Dict[str, Any]:
    """Fetches direct stream URL with caching and in-flight request deduplication."""
    # 1. Fast path: check if audio is already cached on server storage
    cached_path = get_cached_audio_path(video_id)
    if cached_path:
        return {
            "source": "cache",
            "url": f"/api/download/stream/{video_id}",
        }

    now = time.time()
    with _STREAM_CACHE_LOCK:
        if video_id in _STREAM_CACHE:
            item = _STREAM_CACHE[video_id]
            if item.get("expires_at", 0) > now:
                return {
                    "source": "stream",
                    "url": item["url"],
                }

    # 2. Concurrency deduplication: If another thread is already extracting this video_id, wait for it
    i_am_leader = False
    event = None
    with _IN_FLIGHT_LOCK:
        if video_id in _IN_FLIGHT_STREAMS:
            event = _IN_FLIGHT_STREAMS[video_id]
        else:
            event = threading.Event()
            _IN_FLIGHT_STREAMS[video_id] = event
            i_am_leader = True

    if not i_am_leader:
        # Wait for the leader extraction to complete (up to 25s)
        event.wait(timeout=25.0)
        with _IN_FLIGHT_LOCK:
            result = _IN_FLIGHT_RESULTS.get(video_id)
        if result:
            return result
        # Fallback if leader timed out or failed
        with _STREAM_CACHE_LOCK:
            if video_id in _STREAM_CACHE:
                return {"source": "stream", "url": _STREAM_CACHE[video_id]["url"]}

    # As extraction leader: extract fresh audio stream URL via yt-dlp
    try:
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

            result = {
                "source": "stream",
                "url": stream_url,
            }

            with _STREAM_CACHE_LOCK:
                # Cache stream URL for 2 hours
                _STREAM_CACHE[video_id] = {
                    "url": stream_url,
                    "expires_at": time.time() + 7200,
                }

            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT_RESULTS[video_id] = result

            return result
    finally:
        with _IN_FLIGHT_LOCK:
            if video_id in _IN_FLIGHT_STREAMS:
                _IN_FLIGHT_STREAMS[video_id].set()
                _IN_FLIGHT_STREAMS.pop(video_id, None)
            # Retain result briefly for any immediately trailing waiters
            threading.Timer(5.0, lambda: _IN_FLIGHT_RESULTS.pop(video_id, None)).start()


def get_local_track_path(video_id: str) -> Optional[str]:
    """Finds an existing downloaded audio file in CACHE_DIR, MUSIC_DIR, or TRANSIENT_DIR."""
    return get_cached_audio_path(video_id)


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
    if not video_id:
        return
    file_path = get_transient_track_path(video_id)
    if file_path and os.path.isfile(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            print(f"Error removing transient file {file_path}: {e}", file=sys.stderr)
    # Clean up only exact matching video_id files in TRANSIENT_DIR
    if os.path.isdir(TRANSIENT_DIR):
        pattern = re.compile(rf"^(\[{re.escape(video_id)}\]|{re.escape(video_id)})\.(mp3|m4a|webm|part|webp|jpg)$")
        for fname in os.listdir(TRANSIENT_DIR):
            if pattern.search(fname):
                try:
                    os.remove(os.path.join(TRANSIENT_DIR, fname))
                except Exception:
                    pass
    with _DOWNLOAD_LOCK:
        DOWNLOAD_STATUS.pop(video_id, None)


def is_downloaded(video_id: str) -> bool:
    return get_local_track_path(video_id) is not None


def download_track(video_id: str, title: str = "", artist: str = "", user_id: Optional[str] = None):
    """Spawns background download thread for the song in transient storage."""
    now = time.time()
    with _DOWNLOAD_LOCK:
        if video_id in DOWNLOAD_STATUS and DOWNLOAD_STATUS[video_id].get("status") in ("downloading", "ready_for_transfer"):
            return  # Already in progress or ready
        DOWNLOAD_STATUS[video_id] = {
            "status": "downloading",
            "percent": 0.0,
            "title": title or video_id,
            "artist": artist,
            "user_id": user_id,
            "file_path": None,
            "created_at": now,
            "updated_at": now,
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
        now = time.time()
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            pct = round(min(100.0, max(0.0, (downloaded / total) * 100)), 1) if (total and total > 0) else 0.0
            with _DOWNLOAD_LOCK:
                if video_id in DOWNLOAD_STATUS:
                    DOWNLOAD_STATUS[video_id]["percent"] = pct
                    DOWNLOAD_STATUS[video_id]["updated_at"] = now
        elif status == 'finished':
            with _DOWNLOAD_LOCK:
                if video_id in DOWNLOAD_STATUS:
                    DOWNLOAD_STATUS[video_id]["percent"] = 99.0
                    DOWNLOAD_STATUS[video_id]["updated_at"] = now

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
        pattern = re.compile(rf"^(\[{re.escape(video_id)}\]|{re.escape(video_id)})\.(webp|jpg|png)$")
        for fname in os.listdir(TRANSIENT_DIR):
            if pattern.search(fname):
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

        now = time.time()
        with _DOWNLOAD_LOCK:
            if video_id in DOWNLOAD_STATUS:
                DOWNLOAD_STATUS[video_id]["status"] = "ready_for_transfer"
                DOWNLOAD_STATUS[video_id]["percent"] = 100.0
                DOWNLOAD_STATUS[video_id]["file_path"] = final_file
                DOWNLOAD_STATUS[video_id]["filename"] = os.path.basename(final_file) if final_file else f"{video_id}.mp3"
                DOWNLOAD_STATUS[video_id]["updated_at"] = now
    except Exception as e:
        now = time.time()
        with _DOWNLOAD_LOCK:
            if video_id in DOWNLOAD_STATUS:
                DOWNLOAD_STATUS[video_id]["status"] = "error"
                DOWNLOAD_STATUS[video_id]["error"] = str(e)
                DOWNLOAD_STATUS[video_id]["updated_at"] = now


def prune_cache_if_needed(max_bytes: int = 25 * 1024 * 1024 * 1024):
    """Safely prunes least-recently-modified files if cache exceeds 25 GB."""
    if not os.path.isdir(CACHE_DIR):
        return
    try:
        files = []
        total_size = 0
        now = time.time()
        for fname in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, fname)
            if fname.startswith(".part_"):
                # Clean up any abandoned part files older than 30 mins
                if os.path.isfile(p) and (now - os.path.getmtime(p) > 1800):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                continue
            if os.path.isfile(p):
                sz = os.path.getsize(p)
                mtime = os.path.getmtime(p)
                files.append((mtime, sz, p))
                total_size += sz

        if total_size > max_bytes:
            # Sort oldest first
            files.sort(key=lambda x: x[0])
            for mtime, sz, fpath in files:
                try:
                    os.remove(fpath)
                    total_size -= sz
                    if total_size <= int(max_bytes * 0.85):
                        break
                except Exception:
                    pass
    except Exception as e:
        print(f"Error during cache prune: {e}", file=sys.stderr)


def _transient_janitor_loop():
    """Background janitor that prunes stale transient files and enforces cache limits."""
    loop_count = 0
    while True:
        try:
            time.sleep(180)
            now = time.time()
            loop_count += 1

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
                # Only reap completed, errored, or transferred downloads older than 15 mins
                stale_keys = [
                    vid for vid, info in DOWNLOAD_STATUS.items()
                    if (now - info.get("updated_at", info.get("created_at", 0)) > 900)
                    and info.get("status") in ("ready_for_transfer", "error", "completed")
                ]
                for vid in stale_keys:
                    DOWNLOAD_STATUS.pop(vid, None)

            # Every 10 loops (~30 mins), enforce cache size limits
            if loop_count % 10 == 0:
                prune_cache_if_needed()
        except Exception:
            pass


# Start background janitor
_janitor_thread = threading.Thread(target=_transient_janitor_loop, daemon=True)
_janitor_thread.start()
