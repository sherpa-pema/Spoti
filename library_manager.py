import os
import re
from typing import List, Dict, Any

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(PROJECT_DIR, "music_library")
os.makedirs(MUSIC_DIR, exist_ok=True)

try:
    from mutagen.easyid3 import EasyID3
    from mutagen.mp3 import MP3
except ImportError:
    EasyID3 = None
    MP3 = None


def get_library_tracks() -> List[Dict[str, Any]]:
    """Scans music_library and returns all downloaded tracks."""
    if not os.path.isdir(MUSIC_DIR):
        return []

    tracks: List[Dict[str, Any]] = []
    for fname in os.listdir(MUSIC_DIR):
        if not fname.lower().endswith(('.mp3', '.m4a', '.flac')):
            continue

        full_path = os.path.join(MUSIC_DIR, fname)
        size_bytes = os.path.getsize(full_path)
        size_mb = f"{size_bytes / (1024 * 1024):.1f} MB"

        # Extract video_id from filename pattern: "... [video_id].mp3"
        match = re.search(r'\[([a-zA-Z0-9_-]{11})\]\.(mp3|m4a|flac)$', fname)
        video_id = match.group(1) if match else fname

        # Default title from filename
        clean_title = re.sub(r'\[[a-zA-Z0-9_-]{11}\]\.(mp3|m4a|flac)$', '', fname).strip()
        artist = "Offline Music"
        duration_str = "--:--"

        if EasyID3 and fname.lower().endswith('.mp3'):
            try:
                audio = MP3(full_path, ID3=EasyID3)
                if 'title' in audio and audio['title']:
                    clean_title = audio['title'][0]
                if 'artist' in audio and audio['artist']:
                    artist = audio['artist'][0]
                if audio.info and audio.info.length:
                    mins, secs = divmod(int(audio.info.length), 60)
                    duration_str = f"{mins:02d}:{secs:02d}"
            except Exception:
                pass

        tracks.append({
            "id": video_id,
            "filename": fname,
            "title": clean_title or fname,
            "artist": artist,
            "duration": duration_str,
            "size": size_mb,
            "url": f"/api/library/file/{fname}",
            "thumbnail": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=400&q=80",
            "is_local": True,
        })

    return tracks
