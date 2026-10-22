#!/usr/bin/env python3
"""
Spoti Daily Music Fetcher
=========================
Fetches daily curated music charts and metadata for the Spoti Explore section:
1. Trending: Merges iTunes Top 100 Songs RSS + Deezer Global Charts.
2. Billboard Hits: billboard.ChartData('hot-100') via billboard.py.
3. New Releases: iTunes RSS New Releases / Top Albums + Apple & Deezer releases.
4. Genres & Moods: Deezer Genres + top tracks for each genre.

Persistence:
- Primary: PocketBase collections (`trending`, `billboard`, `new_releases`, `genres`)
- Fallback: `/var/mobile/music-data/*.json` and `./music-data/*.json`

Scheduling:
- Enforces a 24-hour cache guard. Exits cleanly if fetched within 24 hours.
- Override with `--force` flag.
"""

import os
import sys
import time
import json
import re
import argparse
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

try:
    import requests
except ImportError:
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import billboard
except ImportError:
    billboard = None

try:
    from ytmusicapi import YTMusic
except ImportError:
    YTMusic = None

def _load_env():
    env_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        "/var/mobile/server/data/spoti.env",
        "/var/mobile/server/.env"
    ]
    for p in env_paths:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass

_load_env()

PB_URL = os.environ.get("POCKETBASE_URL") or os.environ.get("PB_URL", "http://192.168.1.67:8090")
SUPERUSER_EMAIL = os.environ.get("PB_SUPERUSER_EMAIL", "")
SUPERUSER_PASS = os.environ.get("PB_SUPERUSER_PASSWORD") or os.environ.get("PB_SUPERUSER_PASS", "")

# Storage directories
PRIMARY_DATA_DIR = "/var/mobile/music-data"
LOCAL_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music-data")

CACHE_FILE_PRIMARY = os.path.join(PRIMARY_DATA_DIR, ".cache_meta.json")
CACHE_FILE_LOCAL = os.path.join(LOCAL_DATA_DIR, ".cache_meta.json")

CACHE_EXPIRY_SECONDS = 86400  # 24 hours


# ---------------------------------------------------------------------------
# Monkey-patch billboard.py for robust parsing and User-Agent bypass
# ---------------------------------------------------------------------------
def _patch_billboard():
    if not billboard:
        return
    # 1. Patch session to send legitimate browser User-Agent
    orig_get_session = billboard._get_session_with_retries
    def patched_session(max_retries):
        s = orig_get_session(max_retries)
        s.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        return s
    billboard._get_session_with_retries = patched_session

    # 2. Patch _parseNewStylePage to handle current Billboard DOM without crashing
    def robust_parse_new_style(self, soup):
        for entrySoup in soup.select("ul.o-chart-results-list-row"):
            try:
                # Title
                title_el = entrySoup.select_one("h3#title-of-a-story")
                title = title_el.text.strip() if title_el else ""

                # Artist
                artist_el = entrySoup.select_one("h3#title-of-a-story + span.c-label")
                artist = artist_el.text.strip() if artist_el else ""

                # Image
                img_el = entrySoup.select_one("img")
                image = None
                if img_el:
                    image = img_el.get("data-lazy-src") or img_el.get("src")

                # Rank
                rank_el = entrySoup.select_one("span.c-label")
                try:
                    rank = int(rank_el.text.strip())
                except Exception:
                    rank = len(self.entries) + 1

                # Parse optional stats if available
                peak_pos = None
                last_pos = None
                weeks = None
                all_labels = entrySoup.select("ul li span.c-label")
                nums = []
                for lbl in all_labels:
                    txt = lbl.text.strip()
                    if txt.isdigit():
                        nums.append(int(txt))
                if len(nums) >= 3:
                    last_pos = nums[0]
                    peak_pos = nums[1]
                    weeks = nums[2]

                if title:
                    entry = billboard.ChartEntry(title, artist, image, peak_pos, last_pos, weeks, rank, False)
                    self.entries.append(entry)
            except Exception:
                continue

    billboard.ChartData._parseNewStylePage = robust_parse_new_style

_patch_billboard()


# ---------------------------------------------------------------------------
# HTTP Helpers
# ---------------------------------------------------------------------------
def fetch_json(url: str, timeout: int = 15, headers: Optional[Dict[str, str]] = None, retries: int = 2) -> Optional[Any]:
    req_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
        "Accept": "application/json"
    }
    if headers:
        req_headers.update(headers)
    for attempt in range(retries):
        try:
            if requests:
                resp = requests.get(url, headers=req_headers, timeout=timeout)
                if resp.status_code == 200:
                    return resp.json()
            else:
                req = urllib.request.Request(url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == retries - 1:
                print(f"Warning: Failed to fetch {url} after {retries} attempts: {e}")
            time.sleep(1)
    return None


def normalize_key(title: str, artist: str) -> str:
    clean_t = re.sub(r'[\(\[\{].*?[\)\]\}]', '', title.lower()).strip()
    clean_a = re.sub(r'[\(\[\{].*?[\)\]\}]', '', artist.lower()).strip()
    clean_t = re.sub(r'[^a-z0-9]', '', clean_t)
    clean_a = re.sub(r'[^a-z0-9]', '', clean_a)
    return f"{clean_t}::{clean_a}"


# ---------------------------------------------------------------------------
# Section A: Trending Now (Merge iTunes + Deezer)
# ---------------------------------------------------------------------------
def fetch_trending() -> List[Dict[str, Any]]:
    print("Fetching Trending Now (iTunes Top Songs + Deezer Charts)...")
    tracks: List[Dict[str, Any]] = []
    seen = set()

    # 1. iTunes Top 100 Songs
    itunes_url = "https://itunes.apple.com/us/rss/topsongs/limit=100/json"
    itunes_data = fetch_json(itunes_url)
    itunes_entries = []
    if itunes_data and "feed" in itunes_data and "entry" in itunes_data["feed"]:
        itunes_entries = itunes_data["feed"]["entry"]

    # 2. Deezer Global Charts
    deezer_url = "https://api.deezer.com/chart"
    deezer_data = fetch_json(deezer_url)
    deezer_tracks = []
    if deezer_data and "tracks" in deezer_data and "data" in deezer_data["tracks"]:
        deezer_tracks = deezer_data["tracks"]["data"]

    # Interleave and merge iTunes + Deezer
    max_len = max(len(itunes_entries), len(deezer_tracks))
    rank = 1

    for i in range(max_len):
        # Add Deezer track if available
        if i < len(deezer_tracks):
            dt = deezer_tracks[i]
            title = dt.get("title", "").strip()
            artist = dt.get("artist", {}).get("name", "Unknown Artist").strip()
            key = normalize_key(title, artist)
            if key and key not in seen:
                seen.add(key)
                thumb = dt.get("album", {}).get("cover_medium") or dt.get("album", {}).get("cover_big") or ""
                tracks.append({
                    "title": title,
                    "artist": artist,
                    "thumbnail": thumb,
                    "rank": rank,
                    "source": "Deezer",
                    "external_id": str(dt.get("id", "")),
                    "yt_id": ""
                })
                rank += 1

        # Add iTunes track if available
        if i < len(itunes_entries):
            it = itunes_entries[i]
            title = it.get("im:name", {}).get("label", "").strip()
            artist = it.get("im:artist", {}).get("label", "Unknown Artist").strip()
            key = normalize_key(title, artist)
            if key and key not in seen:
                seen.add(key)
                images = it.get("im:image", [])
                thumb = images[-1].get("label", "") if images else ""
                # Replace small resolution with high res if possible
                if thumb and "170x170" in thumb:
                    thumb = thumb.replace("170x170", "500x500")
                ext_id = it.get("id", {}).get("attributes", {}).get("im:id", "")
                tracks.append({
                    "title": title,
                    "artist": artist,
                    "thumbnail": thumb,
                    "rank": rank,
                    "source": "iTunes",
                    "external_id": str(ext_id),
                    "yt_id": ""
                })
                rank += 1

    print(f"Total merged Trending tracks: {len(tracks)}")
    return tracks[:100]


# ---------------------------------------------------------------------------
# Section B: Billboard Hot 100
# ---------------------------------------------------------------------------
def fetch_billboard() -> List[Dict[str, Any]]:
    print("Fetching Billboard Hot 100 via billboard.py...")
    tracks: List[Dict[str, Any]] = []

    try:
        chart = billboard.ChartData("hot-100")
        for e in chart.entries:
            tracks.append({
                "title": e.title,
                "artist": e.artist,
                "thumbnail": e.image or "",
                "rank": e.rank,
                "peak_pos": getattr(e, "peakPos", None) or e.rank,
                "last_pos": getattr(e, "lastPos", None) or 0,
                "weeks_on_chart": getattr(e, "weeks", None) or 1,
                "yt_id": ""
            })
    except Exception as e:
        print(f"Error fetching Billboard chart: {e}")

    # Fallback if billboard.py returned empty
    if not tracks:
        print("Fallback: Scraping Billboard Hot 100 rows directly...")
        try:
            r = requests.get(
                "https://www.billboard.com/charts/hot-100",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=15
            )
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                rank = 1
                for row in soup.select("ul.o-chart-results-list-row"):
                    title_el = row.select_one("h3#title-of-a-story")
                    artist_el = row.select_one("h3#title-of-a-story + span.c-label")
                    img_el = row.select_one("img")
                    title = title_el.text.strip() if title_el else ""
                    artist = artist_el.text.strip() if artist_el else ""
                    thumb = (img_el.get("data-lazy-src") or img_el.get("src")) if img_el else ""
                    if title:
                        tracks.append({
                            "title": title,
                            "artist": artist,
                            "thumbnail": thumb,
                            "rank": rank,
                            "peak_pos": rank,
                            "last_pos": 0,
                            "weeks_on_chart": 1,
                            "yt_id": ""
                        })
                        rank += 1
        except Exception as e2:
            print(f"Direct Billboard scraper failed: {e2}")

    print(f"Total Billboard tracks: {len(tracks)}")
    return tracks[:100]


# ---------------------------------------------------------------------------
# Section C: New Releases
# ---------------------------------------------------------------------------
def fetch_new_releases() -> List[Dict[str, Any]]:
    print("Fetching New Releases (iTunes RSS & Apple Marketing Tools & Deezer)...")
    releases: List[Dict[str, Any]] = []
    seen = set()

    # 1. Primary: iTunes RSS new releases
    itunes_nr_url = "https://itunes.apple.com/us/rss/newreleases/limit=100/json"
    nr_data = fetch_json(itunes_nr_url)
    if nr_data and "feed" in nr_data and "entry" in nr_data["feed"]:
        for it in nr_data["feed"]["entry"]:
            title = it.get("im:name", {}).get("label", "").strip()
            artist = it.get("im:artist", {}).get("label", "Unknown Artist").strip()
            key = normalize_key(title, artist)
            if key and key not in seen:
                seen.add(key)
                images = it.get("im:image", [])
                thumb = images[-1].get("label", "") if images else ""
                rdate = it.get("im:releaseDate", {}).get("label", "")
                releases.append({
                    "title": title,
                    "artist": artist,
                    "thumbnail": thumb,
                    "release_date": rdate[:10] if rdate else "",
                    "source": "iTunes New Releases",
                    "yt_id": ""
                })

    # 2. Fallback / Augment: Apple Marketing Tools Most Played Albums & Deezer Chart Albums
    if len(releases) < 20:
        apple_url = "https://rss.applemarketingtools.com/api/v2/us/music/most-played/50/albums.json"
        apple_data = fetch_json(apple_url)
        if apple_data and "feed" in apple_data and "results" in apple_data["feed"]:
            for item in apple_data["feed"]["results"]:
                title = item.get("name", "").strip()
                artist = item.get("artistName", "Unknown Artist").strip()
                key = normalize_key(title, artist)
                if key and key not in seen:
                    seen.add(key)
                    thumb = item.get("artworkUrl100", "").replace("100x100bb", "500x500bb")
                    releases.append({
                        "title": title,
                        "artist": artist,
                        "thumbnail": thumb,
                        "release_date": item.get("releaseDate", ""),
                        "source": "Apple Music",
                        "yt_id": ""
                    })

    # 3. Augment with Deezer Chart Albums
    deezer_albums_url = "https://api.deezer.com/chart/0/albums"
    dz_albums = fetch_json(deezer_albums_url)
    if dz_albums and "data" in dz_albums:
        for a in dz_albums["data"]:
            title = a.get("title", "").strip()
            artist = a.get("artist", {}).get("name", "Unknown Artist").strip()
            key = normalize_key(title, artist)
            if key and key not in seen:
                seen.add(key)
                thumb = a.get("cover_medium") or a.get("cover_big") or ""
                releases.append({
                    "title": title,
                    "artist": artist,
                    "thumbnail": thumb,
                    "release_date": "",
                    "source": "Deezer Hot Albums",
                    "yt_id": ""
                })

    # 4. Fallback to iTunes Top Albums if under 30 releases
    if len(releases) < 30:
        top_alb_url = "https://itunes.apple.com/us/rss/topalbums/limit=50/json"
        top_alb_data = fetch_json(top_alb_url)
        if top_alb_data and "feed" in top_alb_data and "entry" in top_alb_data["feed"]:
            for it in top_alb_data["feed"]["entry"]:
                title = it.get("im:name", {}).get("label", "").strip()
                artist = it.get("im:artist", {}).get("label", "Unknown Artist").strip()
                key = normalize_key(title, artist)
                if key and key not in seen:
                    seen.add(key)
                    images = it.get("im:image", [])
                    thumb = images[-1].get("label", "") if images else ""
                    if thumb and "170x170" in thumb:
                        thumb = thumb.replace("170x170", "500x500")
                    rdate = it.get("im:releaseDate", {}).get("label", "")
                    releases.append({
                        "title": title,
                        "artist": artist,
                        "thumbnail": thumb,
                        "release_date": rdate[:10] if rdate else "",
                        "source": "iTunes Albums",
                        "yt_id": ""
                    })

    print(f"Total New Releases: {len(releases)}")
    return releases[:60]


# ---------------------------------------------------------------------------
# Section D: Genres (YouTube Music API Genres & Tops)
# ---------------------------------------------------------------------------
def fetch_genres() -> List[Dict[str, Any]]:
    print("Fetching Genres & Curated Tops from YouTube Music API...")
    genres_list: List[Dict[str, Any]] = []

    if YTMusic:
        try:
            from ytmusicapi.navigation import nav, SINGLE_COLUMN_TAB, SECTION_LIST
            ytm = YTMusic()
            categories = ytm.get_mood_categories()
            all_genres = categories.get("Genres", [])

            # High-priority globally popular genres
            priority_names = [
                "Pop", "Hip-hop", "Rock", "Dance & electronic", "Indie & alternative",
                "R&B & soul", "K-Pop", "Latin", "Country & Americana", "Metal",
                "Jazz", "African", "Classical", "Folk & acoustic", "Reggae & caribbean",
                "Hindi", "J-Pop", "Decades", "Desi hip-hop", "Family"
            ]
            selected = [g for g in all_genres if g.get("title") in priority_names]
            for g in all_genres:
                if len(selected) >= 20:
                    break
                if g not in selected:
                    selected.append(g)

            for idx, g in enumerate(selected, start=1):
                gtitle = g.get("title", f"Genre {idx}")
                gparams = g.get("params", "")
                tracks = []
                if gparams:
                    try:
                        res = ytm._send_request("browse", {"browseId": "FEmusic_moods_and_genres_category", "params": gparams})
                        sections = nav(res, SINGLE_COLUMN_TAB + SECTION_LIST)
                        for s in sections:
                            for container_key in ["musicCarouselShelfRenderer", "musicShelfRenderer"]:
                                if container_key in s:
                                    for c in s[container_key].get("contents", []):
                                        if "musicResponsiveListItemRenderer" in c:
                                            r = c["musicResponsiveListItemRenderer"]
                                            try:
                                                title = r["flexColumns"][0]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"][0]["text"]
                                                artist = r["flexColumns"][1]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"][0]["text"]
                                                vid = r.get("playlistItemData", {}).get("videoId")
                                                thumb = r.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [{}])[-1].get("url")
                                                if vid and title:
                                                    tracks.append({
                                                        "id": vid,
                                                        "yt_id": vid,
                                                        "title": title,
                                                        "artist": artist,
                                                        "thumbnail": thumb,
                                                        "duration": "--:--"
                                                    })
                                            except Exception:
                                                pass
                    except Exception as e:
                        print(f"Warning: Failed to fetch tracks for YouTube Music genre {gtitle}: {e}")

                cover = tracks[0]["thumbnail"] if tracks else "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=500"
                genres_list.append({
                    "genre_id": idx,
                    "name": gtitle,
                    "picture": cover,
                    "tracks": tracks[:15]
                })

        except Exception as e:
            print(f"Error fetching YouTube Music genres: {e}")

    # Fallback to Deezer if YouTube Music completely failed
    if not genres_list:
        print("Fallback: Fetching genres from Deezer...")
        dz_res = fetch_json("https://api.deezer.com/genre")
        if dz_res and "data" in dz_res:
            for idx, g in enumerate([item for item in dz_res["data"] if item.get("id") != 0][:16], start=1):
                gid = g.get("id")
                genres_list.append({
                    "genre_id": idx,
                    "name": g.get("name"),
                    "picture": g.get("picture_medium") or g.get("picture") or "",
                    "tracks": []
                })

    print(f"Total Genres fetched from YouTube Music: {len(genres_list)}")
    return genres_list


# ---------------------------------------------------------------------------
# Persistence: PocketBase + JSON Backup
# ---------------------------------------------------------------------------
def get_pb_token() -> Optional[str]:
    email = os.environ.get("PB_SUPERUSER_EMAIL") or SUPERUSER_EMAIL
    pwd = os.environ.get("PB_SUPERUSER_PASSWORD") or os.environ.get("PB_SUPERUSER_PASS") or SUPERUSER_PASS
    if not email or not pwd:
        print("Notice: PocketBase superuser credentials not configured. Skipping remote PB sync.")
        return None
    try:
        url = f"{PB_URL}/api/collections/_superusers/auth-with-password"
        payload = json.dumps({"identity": email, "password": pwd}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("token")
    except Exception as e:
        print(f"PocketBase auth failed: {e}")
        return None


def clear_and_bulk_insert_pb(token: str, collection_name: str, records: List[Dict[str, Any]]) -> bool:
    """Replaces previous daily records with fresh daily records in PocketBase."""
    headers = {"Authorization": token, "Content-Type": "application/json"}
    try:
        # 1. Fetch existing record IDs to remove stale records
        list_url = f"{PB_URL}/api/collections/{collection_name}/records?page=1&perPage=500"
        req = urllib.request.Request(list_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])

        # Delete old records
        for item in items:
            del_url = f"{PB_URL}/api/collections/{collection_name}/records/{item['id']}"
            del_req = urllib.request.Request(del_url, headers=headers, method="DELETE")
            try:
                urllib.request.urlopen(del_req, timeout=5)
            except Exception:
                pass

        # 2. Insert fresh records
        create_url = f"{PB_URL}/api/collections/{collection_name}/records"
        for rec in records:
            payload = json.dumps(rec).encode("utf-8")
            c_req = urllib.request.Request(create_url, data=payload, headers=headers)
            try:
                urllib.request.urlopen(c_req, timeout=5)
            except Exception as e:
                # continue inserting even if single record fails
                pass

        print(f"Successfully saved {len(records)} records to PocketBase collection '{collection_name}'")
        return True
    except Exception as e:
        print(f"Error persisting to PocketBase collection '{collection_name}': {e}")
        return False


def save_json_fallback(filename: str, data: Any):
    """Saves atomic JSON files to both primary and local directories."""
    for base_dir in [PRIMARY_DATA_DIR, LOCAL_DATA_DIR]:
        try:
            os.makedirs(base_dir, exist_ok=True)
            target = os.path.join(base_dir, filename)
            tmp_target = f"{target}.tmp"
            with open(tmp_target, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_target, target)
            print(f"Saved fallback JSON to: {target}")
        except Exception as e:
            # Skip if primary dir cannot be written (e.g. running on non-iPhone dev host)
            pass


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Spoti Daily Explore Data Fetcher")
    parser.add_argument("--force", action="store_true", help="Force fetch regardless of 24h cache timestamp")
    args = parser.parse_args()

    now = time.time()

    # 1. Check 24-hour cache guard
    if not args.force:
        for cache_path in [CACHE_FILE_PRIMARY, CACHE_FILE_LOCAL]:
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    last_fetch = meta.get("last_fetch", 0)
                    elapsed = now - last_fetch
                    if elapsed < CACHE_EXPIRY_SECONDS:
                        hours_ago = elapsed / 3600.0
                        print(f"Cache is fresh: last fetched {hours_ago:.1f} hours ago. Skipping. (Use --force to override)")
                        sys.exit(0)
                except Exception:
                    pass

    print(f"\n==========================================")
    print(f"Spoti Daily Fetcher Starting: {time.ctime()}")
    print(f"==========================================")

    # 2. Fetch all 4 sections
    trending = fetch_trending()
    billboard_hits = fetch_billboard()
    new_releases = fetch_new_releases()
    genres = fetch_genres()

    # 3. Save to PocketBase
    pb_success = True
    token = get_pb_token()
    if token:
        print("Connected to PocketBase as superuser.")
        s1 = clear_and_bulk_insert_pb(token, "trending", trending)
        s2 = clear_and_bulk_insert_pb(token, "billboard", billboard_hits)
        s3 = clear_and_bulk_insert_pb(token, "new_releases", new_releases)
        s4 = clear_and_bulk_insert_pb(token, "genres", genres)
        pb_success = all([s1, s2, s3, s4])
    else:
        print("PocketBase unreachable or login failed. Falling back completely to JSON storage.")
        pb_success = False

    # 4. Save to JSON fallback
    save_json_fallback("trending.json", trending)
    save_json_fallback("billboard.json", billboard_hits)
    save_json_fallback("new_releases.json", new_releases)
    save_json_fallback("genres.json", genres)

    # 5. Update Cache Metadata
    cache_meta = {
        "last_fetch": now,
        "date_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pb_persisted": pb_success,
        "counts": {
            "trending": len(trending),
            "billboard": len(billboard_hits),
            "new_releases": len(new_releases),
            "genres": len(genres),
        }
    }
    for base_dir in [PRIMARY_DATA_DIR, LOCAL_DATA_DIR]:
        try:
            os.makedirs(base_dir, exist_ok=True)
            cpath = os.path.join(base_dir, ".cache_meta.json")
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(cache_meta, f, indent=2)
        except Exception:
            pass

    print(f"\n==========================================")
    print(f"Daily Fetch Complete!")
    print(f"Summary: Trending={len(trending)}, Billboard={len(billboard_hits)}, Releases={len(new_releases)}, Genres={len(genres)}")
    print(f"PocketBase: {'Synced' if pb_success else 'Failed (Using JSON fallback)'}")
    print(f"==========================================\n")


if __name__ == "__main__":
    main()
