import os
import sys
import time
from typing import List, Dict, Any, Optional

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import database
import library_manager
from pocketbase.client import ClientResponseError


def _clean_thumbnail(thumb: Any) -> str:
    """Ensures thumbnail is a valid, safe URL and does not exceed PocketBase field limits."""
    if not thumb or not isinstance(thumb, str):
        return ""
    clean = thumb.strip()
    # Strip base64 data URIs and oversized strings to satisfy PocketBase 5000-char constraint
    if clean.startswith("data:") or len(clean) > 4000:
        return ""
    return clean


def _migrate_legacy_json(user_id: str):
    """Legacy migration no-op (data already migrated to PocketBase)."""
    pass


def get_all_playlists(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns playlists accessible to the user (Personal + Liked + Shared Downloaded)."""
    result: List[Dict[str, Any]] = []

    # 1. Include Downloaded / Synced Library Songs
    try:
        lib_tracks = get_user_library_tracks(user_id) if user_id else []
        lib_thumb = lib_tracks[0].get("thumbnail") if lib_tracks else "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=400&q=80"
        result.append({
            "id": "user_pl_offline_downloads",
            "title": "Downloaded Songs",
            "description": f"{len(lib_tracks)} songs in your synced library" if user_id else "Songs saved on this device (offline)",
            "created_at": 0,
            "thumbnail": lib_thumb,
            "tracks": lib_tracks,
            "track_count": len(lib_tracks),
            "is_offline_playlist": True,
            "is_system": True,
        })
    except Exception as e:
        print(f"Error initializing offline playlist entry: {e}", file=sys.stderr)

    if not user_id or not str(user_id).strip() or len(str(user_id).strip()) < 5:
        return result

    # 2. Include User's Liked Songs playlist
    try:
        liked_tracks = get_user_liked_tracks(user_id)
        liked_thumb = liked_tracks[0].get("thumbnail") if liked_tracks else "https://images.unsplash.com/photo-1494232410401-ad00d5433cfa?w=400&q=80"
        result.append({
            "id": "user_pl_liked_songs",
            "title": "Liked Songs",
            "description": f"{len(liked_tracks)} favorite songs",
            "created_at": 0,
            "thumbnail": liked_thumb,
            "tracks": liked_tracks,
            "track_count": len(liked_tracks),
            "is_liked_playlist": True,
            "is_system": True,
        })
    except Exception as e:
        print(f"Error loading liked songs into playlist: {e}", file=sys.stderr)

    # 3. Include Custom User Playlists from PocketBase
    try:
        admin_pb = database.get_admin_pb()
        esc_uid = database.pb_escape(user_id)
        playlists_res = admin_pb.collection("playlists").get_full_list(
            query_params={"filter": f'user = "{esc_uid}"', "sort": "-created"}
        )

        for pl in playlists_res:
            # Query track count
            try:
                esc_pid = database.pb_escape(pl.id)
                tracks_check = admin_pb.collection("playlist_tracks").get_list(
                    1, 1, query_params={"filter": f'playlist = "{esc_pid}"'}
                )
                cnt = tracks_check.total_items
            except Exception:
                cnt = 0

            thumb = getattr(pl, "thumbnail", "") or "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=400&q=80"

            result.append({
                "id": pl.id,
                "title": pl.title,
                "description": getattr(pl, "description", "") or "",
                "thumbnail": thumb,
                "created_at": getattr(pl, "created", ""),
                "track_count": cnt,
                "is_user_playlist": True,
                "is_system": False,
            })
    except Exception as e:
        print(f"Error fetching user playlists from PocketBase: {e}", file=sys.stderr)

    return result


def create_playlist(user_id: str, name: str, description: str = "") -> Dict[str, Any]:
    """Creates a new playlist in PocketBase for the user."""
    name = name.strip() or "Untitled Playlist"
    description = description.strip()
    thumb = "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=400&q=80"

    admin_pb = database.get_admin_pb()
    record = admin_pb.collection("playlists").create({
        "user": user_id,
        "title": name,
        "description": description,
        "thumbnail": thumb
    })

    return {
        "id": record.id,
        "title": name,
        "description": description,
        "created_at": getattr(record, "created", time.time()),
        "thumbnail": thumb,
        "tracks": [],
        "track_count": 0,
        "is_user_playlist": True,
    }


def get_playlist(playlist_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Returns a single playlist with all its tracks."""
    # 1. Downloaded / Saved Library Songs (Backed by PocketBase user_library when user is authenticated)
    if playlist_id == "user_pl_offline_downloads":
        lib_tracks = get_user_library_tracks(user_id) if user_id else []
        thumb = lib_tracks[0].get("thumbnail") if lib_tracks else "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=400&q=80"
        return {
            "id": "user_pl_offline_downloads",
            "title": "Downloaded Songs",
            "description": f"{len(lib_tracks)} songs in your synced library" if user_id else "Songs saved on this device (offline)",
            "thumbnail": thumb,
            "tracks": lib_tracks,
            "track_count": len(lib_tracks),
            "is_user_playlist": True,
            "is_offline_playlist": True,
            "is_system": True,
        }

    # 2. Liked Songs
    if playlist_id == "user_pl_liked_songs":
        liked_tracks = get_user_liked_tracks(user_id) if user_id else []
        thumb = liked_tracks[0].get("thumbnail") if liked_tracks else "https://images.unsplash.com/photo-1494232410401-ad00d5433cfa?w=400&q=80"
        return {
            "id": "user_pl_liked_songs",
            "title": "Liked Songs",
            "description": f"{len(liked_tracks)} favorite songs",
            "thumbnail": thumb,
            "tracks": liked_tracks,
            "track_count": len(liked_tracks),
            "is_user_playlist": True,
            "is_liked_playlist": True,
            "is_system": True,
        }

    # 3. Custom Playlist from PocketBase
    admin_pb = database.get_admin_pb()
    try:
        pl = admin_pb.collection("playlists").get_one(playlist_id)
        if not pl:
            return None

        # Enforce ownership check or explicit public visibility
        is_public = getattr(pl, "is_public", False)
        if not is_public and (not user_id or pl.user != user_id):
            return None

        # Fetch tracks in playlist with escaped playlist_id
        esc_pid = database.pb_escape(playlist_id)
        tracks_records = admin_pb.collection("playlist_tracks").get_full_list(
            query_params={"filter": f'playlist = "{esc_pid}"', "sort": "created"}
        )

        tracks = [{
            "id": t.track_id,
            "title": t.title,
            "artist": getattr(t, "artist", ""),
            "duration": getattr(t, "duration", "--:--"),
            "thumbnail": getattr(t, "thumbnail", ""),
            "album": getattr(t, "album", "")
        } for t in tracks_records]

        thumb = getattr(pl, "thumbnail", "")
        if not thumb and tracks:
            thumb = tracks[0]["thumbnail"]
        if not thumb:
            thumb = "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=400&q=80"

        return {
            "id": pl.id,
            "title": pl.title,
            "description": getattr(pl, "description", "") or "",
            "thumbnail": thumb,
            "created_at": getattr(pl, "created", ""),
            "tracks": tracks,
            "track_count": len(tracks),
            "is_user_playlist": True,
            "user_id": pl.user,
        }
    except ClientResponseError as e:
        if getattr(e, 'status', None) != 404:
            print(f"PocketBase error fetching playlist {playlist_id}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        return None


def add_track_to_playlist(playlist_id: str, track: Dict[str, Any], user_id: Optional[str] = None) -> bool:
    """Adds a track to a playlist in PocketBase with strict ownership verification."""
    if not user_id:
        return False

    if playlist_id in ("user_pl_offline_downloads", "user_pl_liked_songs"):
        if playlist_id == "user_pl_liked_songs":
            return like_track(user_id, track)
        return False

    admin_pb = database.get_admin_pb()
    try:
        # Strictly verify ownership
        pl = admin_pb.collection("playlists").get_one(playlist_id)
        if getattr(pl, "user", None) != user_id:
            return False

        track_id = track.get("id")
        if not track_id:
            return False

        # Check if already in playlist
        esc_pid = database.pb_escape(playlist_id)
        esc_tid = database.pb_escape(track_id)
        existing = admin_pb.collection("playlist_tracks").get_list(
            1, 1, query_params={"filter": f'playlist = "{esc_pid}" && track_id = "{esc_tid}"'}
        )
        if existing.total_items > 0:
            return True

        admin_pb.collection("playlist_tracks").create({
            "playlist": playlist_id,
            "track_id": track_id,
            "title": track.get("title", "Unknown Title"),
            "artist": track.get("artist", ""),
            "duration": track.get("duration", "--:--"),
            "thumbnail": _clean_thumbnail(track.get("thumbnail")),
            "album": track.get("album", "")
        })
        return True
    except Exception as e:
        print(f"Error adding track to playlist {playlist_id} in PocketBase: {e}", file=sys.stderr)
        return False


def remove_track_from_playlist(playlist_id: str, track_id: str, user_id: Optional[str] = None) -> bool:
    """Removes a track from a playlist in PocketBase with strict ownership verification."""
    if not user_id or not track_id:
        return False

    if playlist_id == "user_pl_offline_downloads":
        # Decouple user removal from deleting shared audio cache files!
        # Only unlinks the track from the user's library in PocketBase.
        return remove_track_from_library(user_id, track_id)

    if playlist_id == "user_pl_liked_songs":
        return unlike_track(user_id, track_id)

    admin_pb = database.get_admin_pb()
    try:
        # Strictly verify ownership
        pl = admin_pb.collection("playlists").get_one(playlist_id)
        if getattr(pl, "user", None) != user_id:
            return False

        esc_pid = database.pb_escape(playlist_id)
        esc_tid = database.pb_escape(track_id)
        existing = admin_pb.collection("playlist_tracks").get_list(
            1, 1, query_params={"filter": f'playlist = "{esc_pid}" && track_id = "{esc_tid}"'}
        )
        for item in existing.items:
            admin_pb.collection("playlist_tracks").delete(item.id)
        return True
    except Exception as e:
        print(f"Error removing track {track_id} from playlist {playlist_id}: {e}", file=sys.stderr)
        return False


def delete_playlist(playlist_id: str, user_id: Optional[str] = None) -> bool:
    """Deletes a playlist from PocketBase (cascade deletes its tracks). Requires user_id ownership."""
    if not user_id or not playlist_id:
        return False
    if playlist_id in ("user_pl_offline_downloads", "user_pl_liked_songs"):
        return False

    admin_pb = database.get_admin_pb()
    try:
        pl = admin_pb.collection("playlists").get_one(playlist_id)
        if not pl or getattr(pl, "user", None) != user_id:
            return False

        admin_pb.collection("playlists").delete(playlist_id)
        return True
    except Exception as e:
        print(f"Error deleting playlist {playlist_id} from PocketBase: {e}", file=sys.stderr)
        return False


# -------------------------------------------------------------
# User Favorites / Liked Songs
# -------------------------------------------------------------
def get_user_liked_tracks(user_id: str) -> List[Dict[str, Any]]:
    """Returns all liked tracks for a given user from PocketBase."""
    if not user_id:
        return []

    admin_pb = database.get_admin_pb()
    try:
        esc_uid = database.pb_escape(user_id)
        favs = admin_pb.collection("user_favorites").get_full_list(
            query_params={"filter": f'user = "{esc_uid}"', "sort": "-created"}
        )
        return [{
            "id": f.track_id,
            "title": f.title,
            "artist": getattr(f, "artist", ""),
            "duration": getattr(f, "duration", "--:--"),
            "thumbnail": getattr(f, "thumbnail", ""),
            "album": getattr(f, "album", ""),
            "liked_at": getattr(f, "created", "")
        } for f in favs]
    except Exception as e:
        print(f"Error fetching user favorites from PocketBase: {e}", file=sys.stderr)
        return []


def like_track(user_id: str, track: Dict[str, Any]) -> bool:
    """Adds a track to the user's favorites in PocketBase."""
    if not user_id:
        return False

    track_id = track.get("id")
    if not track_id:
        return False

    admin_pb = database.get_admin_pb()
    try:
        esc_uid = database.pb_escape(user_id)
        esc_tid = database.pb_escape(track_id)
        existing = admin_pb.collection("user_favorites").get_list(
            1, 1, query_params={"filter": f'user = "{esc_uid}" && track_id = "{esc_tid}"'}
        )
        if existing.total_items > 0:
            return True

        admin_pb.collection("user_favorites").create({
            "user": user_id,
            "track_id": track_id,
            "title": track.get("title", "Unknown Title"),
            "artist": track.get("artist", ""),
            "duration": track.get("duration", "--:--"),
            "thumbnail": _clean_thumbnail(track.get("thumbnail")),
            "album": track.get("album", "")
        })
        return True
    except Exception as e:
        print(f"Error liking track {track_id} in PocketBase: {e}", file=sys.stderr)
        return False


def unlike_track(user_id: str, track_id: str) -> bool:
    """Removes a track from the user's favorites in PocketBase with strict ownership verification."""
    if not user_id or not track_id:
        return False

    admin_pb = database.get_admin_pb()
    try:
        esc_uid = database.pb_escape(user_id)
        esc_tid = database.pb_escape(track_id)
        existing = admin_pb.collection("user_favorites").get_list(
            1, 10, query_params={"filter": f'user = "{esc_uid}" && track_id = "{esc_tid}"'}
        )
        for item in existing.items:
            # Re-verify ownership before deleting
            if getattr(item, "user", None) == user_id:
                admin_pb.collection("user_favorites").delete(item.id)
        return True
    except Exception as e:
        print(f"Error unliking track {track_id} in PocketBase: {e}", file=sys.stderr)
        return False


def is_track_liked(user_id: str, track_id: str) -> bool:
    """Checks if a track is in the user's favorites in PocketBase."""
    if not user_id or not track_id:
        return False

    admin_pb = database.get_admin_pb()
    try:
        esc_uid = database.pb_escape(user_id)
        esc_tid = database.pb_escape(track_id)
        existing = admin_pb.collection("user_favorites").get_list(
            1, 1, query_params={"filter": f'user = "{esc_uid}" && track_id = "{esc_tid}"'}
        )
        return existing.total_items > 0
    except Exception:
        return False


# -------------------------------------------------------------
# User Library / Central Downloaded & Saved Tracks Sync
# -------------------------------------------------------------
def get_user_library_tracks(user_id: str) -> List[Dict[str, Any]]:
    """Returns all tracks in user's central library from PocketBase."""
    if not user_id:
        return []

    admin_pb = database.get_admin_pb()
    try:
        esc_uid = database.pb_escape(user_id)
        records = admin_pb.collection("user_library").get_full_list(
            query_params={"filter": f'user = "{esc_uid}"', "sort": "-created"}
        )
        return [{
            "id": r.track_id,
            "track_id": r.track_id,
            "title": r.title,
            "artist": getattr(r, "artist", "") or "Unknown Artist",
            "duration": getattr(r, "duration", "--:--") or "--:--",
            "thumbnail": getattr(r, "thumbnail", "") or "",
            "album": getattr(r, "album", "") or "",
            "source": getattr(r, "source", "download") or "download",
            "saved_at": getattr(r, "created", "")
        } for r in records]
    except Exception as e:
        print(f"Error fetching user library from PocketBase: {e}", file=sys.stderr)
        return []


def add_track_to_library(user_id: str, track: Dict[str, Any]) -> bool:
    """Adds a track to the user's central library in PocketBase."""
    if not user_id:
        return False

    track_id = track.get("id") or track.get("track_id")
    if not track_id:
        return False

    admin_pb = database.get_admin_pb()
    try:
        esc_uid = database.pb_escape(user_id)
        esc_tid = database.pb_escape(track_id)
        existing = admin_pb.collection("user_library").get_list(
            1, 1, query_params={"filter": f'user = "{esc_uid}" && track_id = "{esc_tid}"'}
        )
        if existing.total_items > 0:
            return True

        admin_pb.collection("user_library").create({
            "user": user_id,
            "track_id": track_id,
            "title": track.get("title", "Unknown Title"),
            "artist": track.get("artist", "") or "",
            "duration": track.get("duration", "--:--") or "--:--",
            "thumbnail": _clean_thumbnail(track.get("thumbnail")),
            "album": track.get("album", "") or "",
            "source": track.get("source", "download") or "download"
        })
        return True
    except Exception as e:
        print(f"Error saving track {track_id} to user library in PocketBase: {e}", file=sys.stderr)
        return False


def remove_track_from_library(user_id: str, track_id: str) -> bool:
    """Removes a track from the user's central library in PocketBase with strict ownership verification."""
    if not user_id or not track_id:
        return False

    admin_pb = database.get_admin_pb()
    try:
        esc_uid = database.pb_escape(user_id)
        esc_tid = database.pb_escape(track_id)
        existing = admin_pb.collection("user_library").get_list(
            1, 10, query_params={"filter": f'user = "{esc_uid}" && track_id = "{esc_tid}"'}
        )
        for item in existing.items:
            # Re-verify ownership before deleting
            if getattr(item, "user", None) == user_id:
                admin_pb.collection("user_library").delete(item.id)
        return True
    except Exception as e:
        print(f"Error removing track {track_id} from user library in PocketBase: {e}", file=sys.stderr)
        return False


def is_track_in_library(user_id: str, track_id: str) -> bool:
    """Checks if a track is in the user's library in PocketBase."""
    if not user_id or not track_id:
        return False

    admin_pb = database.get_admin_pb()
    try:
        esc_uid = database.pb_escape(user_id)
        esc_tid = database.pb_escape(track_id)
        existing = admin_pb.collection("user_library").get_list(
            1, 1, query_params={"filter": f'user = "{esc_uid}" && track_id = "{esc_tid}"'}
        )
        return existing.total_items > 0
    except Exception:
        return False
        return False
