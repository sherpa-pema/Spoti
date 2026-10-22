import os
import sys
from typing import List, Dict, Any, Optional

try:
    from ytmusicapi import YTMusic
except ImportError:
    YTMusic = None

# Initialize ytmusicapi instance
_ytm: Optional[Any] = None

def get_ytm() -> Any:
    global _ytm
    if _ytm is None:
        if YTMusic is None:
            raise RuntimeError("ytmusicapi is not installed. Please run: pip install ytmusicapi")
        _ytm = YTMusic()
    return _ytm


def _get_best_thumbnail(thumbnails: Optional[List[Dict[str, Any]]]) -> str:
    """Extracts the highest-resolution thumbnail URL available."""
    if not thumbnails:
        return "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=500&q=80"
    # Usually the last thumbnail has the highest resolution
    return thumbnails[-1].get("url", "")


def _format_artists(artists: Any) -> str:
    """Formats list of artist dicts or strings into a clean comma-separated string."""
    if not artists:
        return "Unknown Artist"
    if isinstance(artists, list):
        names = []
        for a in artists:
            if isinstance(a, dict):
                names.append(a.get("name", ""))
            elif isinstance(a, str):
                names.append(a)
        clean_names = [n for n in names if n]
        return ", ".join(clean_names) if clean_names else "Unknown Artist"
    if isinstance(artists, str):
        return artists
    return "Unknown Artist"


def get_trending_tracks(limit: int = 25) -> List[Dict[str, Any]]:
    """Fetches trending songs from YouTube Music charts."""
    ytm = get_ytm()
    tracks: List[Dict[str, Any]] = []

    try:
        charts = ytm.get_charts(country="US")
        # charts['videos'] has chart playlists like "Top 100 Music Videos" or "Trending"
        chart_playlists = charts.get("videos", [])
        if chart_playlists and len(chart_playlists) > 0:
            target_pl_id = chart_playlists[0].get("playlistId")
            if target_pl_id:
                pl_data = ytm.get_playlist(target_pl_id, limit=limit)
                for t in pl_data.get("tracks", []):
                    vid = t.get("videoId")
                    if vid:
                        tracks.append({
                            "id": vid,
                            "title": t.get("title", "Unknown Title"),
                            "artist": _format_artists(t.get("artists")),
                            "album": t.get("album", {}).get("name") if isinstance(t.get("album"), dict) else (t.get("album") or ""),
                            "duration": t.get("duration", "--:--"),
                            "thumbnail": _get_best_thumbnail(t.get("thumbnails")),
                        })
    except Exception as e:
        print(f"Warning: Failed to fetch chart playlist: {e}")

    # Fallback to search query 'trending hits' if charts were empty
    if not tracks:
        try:
            results = ytm.search("trending hits", filter="songs", limit=limit)
            for t in results:
                vid = t.get("videoId")
                if vid:
                    tracks.append({
                        "id": vid,
                        "title": t.get("title", "Unknown Title"),
                        "artist": _format_artists(t.get("artists")),
                        "album": t.get("album", {}).get("name") if isinstance(t.get("album"), dict) else (t.get("album") or ""),
                        "duration": t.get("duration", "--:--"),
                        "thumbnail": _get_best_thumbnail(t.get("thumbnails")),
                    })
        except Exception as e:
            print(f"Error in trending fallback: {e}")

    return tracks[:limit]


def get_new_releases(limit: int = 20) -> List[Dict[str, Any]]:
    """Fetches new release tracks from YouTube Music."""
    ytm = get_ytm()
    tracks: List[Dict[str, Any]] = []

    try:
        results = ytm.search("new releases songs", filter="songs", limit=limit)
        for t in results:
            vid = t.get("videoId")
            if vid:
                tracks.append({
                    "id": vid,
                    "title": t.get("title", "Unknown Title"),
                    "artist": _format_artists(t.get("artists")),
                    "album": t.get("album", {}).get("name") if isinstance(t.get("album"), dict) else (t.get("album") or ""),
                    "duration": t.get("duration", "--:--"),
                    "thumbnail": _get_best_thumbnail(t.get("thumbnails")),
                })
    except Exception as e:
        print(f"Error fetching new releases: {e}")

    return tracks[:limit]


def get_featured_playlists(limit: int = 10) -> List[Dict[str, Any]]:
    """Fetches popular / chart playlists (Today's Top Hits, Viral Hits, etc.)."""
    ytm = get_ytm()
    playlists: List[Dict[str, Any]] = []

    try:
        results = ytm.search("today top hits", filter="playlists", limit=limit)
        for p in results:
            pl_id = p.get("browseId") or p.get("playlistId")
            if pl_id:
                playlists.append({
                    "id": pl_id,
                    "type": "playlist",
                    "title": p.get("title", "Untitled Playlist"),
                    "artist": p.get("author", "YouTube Music"),
                    "count": p.get("itemCount", ""),
                    "thumbnail": _get_best_thumbnail(p.get("thumbnails")),
                })
    except Exception as e:
        print(f"Error fetching featured playlists: {e}")

    return playlists[:limit]


def get_search_suggestions(query: str) -> List[str]:
    """Fetches real-time search autocomplete suggestions for user input."""
    if not query or not query.strip():
        return []
    ytm = get_ytm()
    try:
        suggestions = ytm.get_search_suggestions(query.strip())
        return suggestions if isinstance(suggestions, list) else []
    except Exception as e:
        print(f"Error getting search suggestions: {e}")
        return []


def get_moods_and_genres() -> Dict[str, List[Dict[str, str]]]:
    """Fetches mood and genre categories."""
    ytm = get_ytm()
    try:
        categories = ytm.get_mood_categories()
        # Keep popular categories
        moods = categories.get("Moods & moments", [])
        genres = categories.get("Genres", [])
        return {
            "moods": [{"title": m.get("title"), "params": m.get("params")} for m in moods],
            "genres": [{"title": g.get("title"), "params": g.get("params")} for g in genres[:12]]
        }
    except Exception as e:
        print(f"Error fetching moods: {e}")
        return {"moods": [], "genres": []}


def get_category_playlists(params: str, limit: int = 15) -> List[Dict[str, Any]]:
    """Fetches playlists for a given mood/genre param string."""
    ytm = get_ytm()
    try:
        playlists = ytm.get_mood_playlists(params)
        results = []
        for p in playlists[:limit]:
            results.append({
                "id": p.get("playlistId"),
                "title": p.get("title", "Untitled Playlist"),
                "description": p.get("description", ""),
                "thumbnail": _get_best_thumbnail(p.get("thumbnails")),
                "count": p.get("count", ""),
            })
        return results
    except Exception as e:
        print(f"Error fetching mood playlists: {e}")
        return []


def search_music(query: str, filter_type: str = "songs", limit: int = 25) -> List[Dict[str, Any]]:
    """Searches YouTube Music for songs or playlists."""
    ytm = get_ytm()
    try:
        valid_filter = filter_type if filter_type in ["songs", "playlists", "albums"] else "songs"
        raw_results = ytm.search(query, filter=valid_filter, limit=limit)
        results = []
        for item in raw_results:
            if valid_filter == "songs":
                vid = item.get("videoId")
                if vid:
                    results.append({
                        "id": vid,
                        "type": "song",
                        "title": item.get("title", "Unknown Title"),
                        "artist": _format_artists(item.get("artists")),
                        "album": item.get("album", {}).get("name") if isinstance(item.get("album"), dict) else (item.get("album") or ""),
                        "duration": item.get("duration", "--:--"),
                        "thumbnail": _get_best_thumbnail(item.get("thumbnails")),
                    })
            elif valid_filter == "playlists":
                pl_id = item.get("browseId") or item.get("playlistId")
                if pl_id:
                    results.append({
                        "id": pl_id,
                        "type": "playlist",
                        "title": item.get("title", "Untitled Playlist"),
                        "artist": item.get("author", "YouTube Music"),
                        "count": item.get("itemCount", ""),
                        "thumbnail": _get_best_thumbnail(item.get("thumbnails")),
                    })
        return results
    except Exception as e:
        print(f"Error searching music: {e}")
        return []


def get_radio_tracks(video_id: str, limit: int = 35) -> List[Dict[str, Any]]:
    """Generates continuous radio recommendation tracks based on a song videoId."""
    ytm = get_ytm()
    tracks: List[Dict[str, Any]] = []

    # 1. Primary: Safe direct watch-next playlist panel extraction
    # Bypasses the broken related_browse_id tab lookup in ytmusicapi 1.10.x
    try:
        from ytmusicapi.parsers.watch import parse_watch_playlist
        endpoint = "next"
        body = {
            "enablePersistentPlaylistPanel": True,
            "isAudioOnly": True,
            "tunerSettingValue": "AUTOMIX_SETTING_NORMAL",
            "videoId": video_id,
            "playlistId": f"RDAMVM{video_id}"
        }
        resp = ytm._send_request(endpoint, body)
        watch_next = (
            resp.get("contents", {})
            .get("singleColumnMusicWatchNextResultsRenderer", {})
            .get("tabbedRenderer", {})
            .get("watchNextTabbedResultsRenderer", {})
        )
        tabs = watch_next.get("tabs", [])
        if tabs:
            tab0 = (
                tabs[0]
                .get("tabRenderer", {})
                .get("content", {})
                .get("musicQueueRenderer", {})
                .get("content", {})
                .get("playlistPanelRenderer", {})
            )
            contents = tab0.get("contents", [])
            raw_tracks = parse_watch_playlist(contents)
            for t in raw_tracks:
                vid = t.get("videoId")
                if vid and vid != video_id:
                    album_val = t.get("album")
                    album_name = album_val.get("name") if isinstance(album_val, dict) else (album_val or "")
                    thumb_val = t.get("thumbnail") or t.get("thumbnails")
                    tracks.append({
                        "id": vid,
                        "title": t.get("title", "Unknown Title"),
                        "artist": _format_artists(t.get("artists")),
                        "album": album_name,
                        "duration": t.get("length") or t.get("duration") or "--:--",
                        "thumbnail": _get_best_thumbnail(thumb_val),
                    })
    except Exception as e:
        print(f"Notice: Direct watch radio extraction error: {e}")

    # 2. Resilient Fallback: If watch panel returned no tracks
    if not tracks:
        try:
            search_results = ytm.search(f"{video_id}", filter="songs", limit=limit)
            if not search_results:
                search_results = ytm.search("top hits", filter="songs", limit=limit)
            for item in search_results:
                vid = item.get("videoId")
                if vid and vid != video_id:
                    album_val = item.get("album")
                    album_name = album_val.get("name") if isinstance(album_val, dict) else (album_val or "")
                    thumb_val = item.get("thumbnail") or item.get("thumbnails")
                    tracks.append({
                        "id": vid,
                        "title": item.get("title", "Unknown Title"),
                        "artist": _format_artists(item.get("artists")),
                        "album": album_name,
                        "duration": item.get("duration", "--:--"),
                        "thumbnail": _get_best_thumbnail(thumb_val),
                    })
        except Exception as e2:
            print(f"Fallback radio search error: {e2}")

    return tracks[:limit]


def get_playlist_details(playlist_id: str) -> Dict[str, Any]:
    """Fetches all songs from a playlist."""
    ytm = get_ytm()
    try:
        data = ytm.get_playlist(playlistId=playlist_id)
        tracks = []
        for t in data.get("tracks", []):
            vid = t.get("videoId")
            if vid:
                tracks.append({
                    "id": vid,
                    "title": t.get("title", "Unknown Title"),
                    "artist": _format_artists(t.get("artists")),
                    "album": t.get("album", {}).get("name") if isinstance(t.get("album"), dict) else (t.get("album") or ""),
                    "duration": t.get("duration", "--:--"),
                    "thumbnail": _get_best_thumbnail(t.get("thumbnails")),
                })
        return {
            "id": playlist_id,
            "title": data.get("title", "Playlist"),
            "description": data.get("description", ""),
            "thumbnail": _get_best_thumbnail(data.get("thumbnails")),
            "tracks": tracks,
        }
    except Exception as e:
        print(f"Error fetching playlist details: {e}")
        return {"id": playlist_id, "title": "Playlist", "tracks": []}
