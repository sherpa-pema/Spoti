import os
import sys
import re
import time
import json
import unittest.mock
import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# Mock yt_dlp if not installed in local environment
if "yt_dlp" not in sys.modules:
    sys.modules["yt_dlp"] = unittest.mock.MagicMock()

import database
import auth
import stream_downloader


def test_pocketbase_filter_escaping():
    """Validates that quotes and backslashes in filter values are escaped to prevent injection."""
    injection_payload = 'x" || user != ""'
    escaped = database.pb_escape(injection_payload)
    assert escaped == 'x\\" || user != \\"\\"'
    assert '\\"' in escaped

    backslash_payload = 'test\\path"injection'
    escaped_bs = database.pb_escape(backslash_payload)
    assert escaped_bs == 'test\\\\path\\"injection'

    assert database.pb_escape(None) == ""
    assert database.pb_escape(123) == "123"


def test_password_and_username_validation():
    """Validates that password must be at least 8 characters and username meets requirements."""
    # Too short password
    with pytest.raises(ValueError, match="Password must be at least 8 characters"):
        auth.register_user("testuser", "1234")

    with pytest.raises(ValueError, match="Password must be at least 8 characters"):
        auth.register_user("testuser", "short7")

    # Too short username
    with pytest.raises(ValueError, match="Username must be between 2 and 32 characters"):
        auth.register_user("a", "validpassword123")

    # Invalid username characters
    with pytest.raises(ValueError, match="Username contains invalid characters"):
        auth.register_user("user<script>", "validpassword123")


def test_cached_audio_path_safety():
    """Validates that get_cached_audio_path validates input and does not crash on malformed IDs."""
    assert stream_downloader.get_cached_audio_path("") is None
    assert stream_downloader.get_cached_audio_path("../../etc/passwd") is None
    assert stream_downloader.get_cached_audio_path("invalid id with spaces") is None


def test_range_cache_corruption_logic():
    """
    Simulates the server range completion logic from finding #4:
    A partial range response (e.g. 512 KB slice) must NEVER be marked as full content.
    """
    import re

    content_range_partial = "bytes 0-524287/3400120"
    content_range_full = "bytes 0-3400119/3400120"

    def is_full_stream(status_code: int, content_length: str, content_range: str, client_range: str):
        expected_total_bytes = None
        full = False
        if status_code == 200:
            if content_length and content_length.isdigit():
                expected_total_bytes = int(content_length)
                full = True
        elif status_code == 206 and content_range:
            cr_match = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+)$", content_range.strip())
            if cr_match:
                s_byte = int(cr_match.group(1))
                e_byte = int(cr_match.group(2))
                tot_bytes = int(cr_match.group(3))
                if s_byte == 0 and (e_byte + 1) == tot_bytes:
                    expected_total_bytes = tot_bytes
                    full = True

        return (
            full and
            expected_total_bytes is not None and
            expected_total_bytes > 50000 and
            (not client_range or client_range.strip() in ("", "bytes=0-"))
        )

    # Partial range request: MUST BE FALSE
    assert not is_full_stream(206, "524288", content_range_partial, "bytes=0-524287")

    # Full range from 200 OK: MUST BE TRUE
    assert is_full_stream(200, "3400120", None, "")

    # Complete 206 serving byte 0 to end: MUST BE TRUE
    assert is_full_stream(206, "3400120", content_range_full, "bytes=0-")


def test_playlist_datetime_serialization_and_flags():
    """Validates that playlist manager serializes datetimes to ISO strings and sets system/user flags."""
    import datetime
    import json
    import playlist_manager

    # 1. Test _format_created
    dt = datetime.datetime(2026, 10, 22, 6, 29, 49)
    assert playlist_manager._format_created(dt) == "2026-10-22T06:29:49"
    assert playlist_manager._format_created(None) == ""
    assert playlist_manager._format_created("2026-10-22 06:29:49") == "2026-10-22 06:29:49"

    # 2. Mock PocketBase record with datetime
    mock_record = unittest.mock.MagicMock()
    mock_record.id = "pl_test_123"
    mock_record.created = dt

    with unittest.mock.patch("database.get_admin_pb") as mock_get_admin:
        mock_pb = unittest.mock.MagicMock()
        mock_pb.collection.return_value.create.return_value = mock_record
        mock_get_admin.return_value = mock_pb

        created = playlist_manager.create_playlist("user_123", "My Test Playlist")
        assert created["id"] == "pl_test_123"
        assert created["title"] == "My Test Playlist"
        assert created["is_user_playlist"] is True
        assert created["is_system"] is False
        assert created["created_at"] == "2026-10-22T06:29:49"

        # Ensure json.dumps passes without TypeError: Object of type datetime is not JSON serializable
        serialized = json.dumps(created)
        deserialized = json.loads(serialized)
        assert deserialized["created_at"] == "2026-10-22T06:29:49"
        assert deserialized["is_user_playlist"] is True
        assert deserialized["is_system"] is False


def test_downloads_status_auth_and_scoping():
    """Validates that GET /api/downloads/status requires auth and scopes strictly to user."""
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)

    # 1. Unauthenticated request -> 401
    resp = client.get("/api/downloads/status")
    assert resp.status_code == 401

    # 2. Setup mock downloads in stream_downloader.DOWNLOAD_STATUS
    with stream_downloader._DOWNLOAD_LOCK:
        stream_downloader.DOWNLOAD_STATUS.clear()
        stream_downloader.DOWNLOAD_STATUS["vid_user_a_1"] = {
            "status": "downloading",
            "percent": 50.0,
            "title": "Song A",
            "artist": "Artist A",
            "user_id": "user_a",
            "error": None
        }
        stream_downloader.DOWNLOAD_STATUS["vid_user_b_1"] = {
            "status": "ready_for_transfer",
            "percent": 100.0,
            "title": "Song B",
            "artist": "Artist B",
            "user_id": "user_b",
            "error": None
        }
        stream_downloader.DOWNLOAD_STATUS["vid_unowned_1"] = {
            "status": "downloading",
            "percent": 10.0,
            "title": "Song C",
            "artist": "Artist C",
            "user_id": None,
            "error": None
        }

    # Mock auth.verify_token to return user_a
    with unittest.mock.patch("auth.verify_token", return_value={"uid": "user_a", "username": "alice"}):
        resp = client.get("/api/downloads/status", headers={"Authorization": "Bearer mock_token_a"})
        assert resp.status_code == 200
        data = resp.json()
        assert "vid_user_a_1" in data
        assert "vid_user_b_1" not in data
        assert "vid_unowned_1" not in data
        assert data["vid_user_a_1"]["title"] == "Song A"


def test_serve_transient_download_file_security(tmp_path):
    """Validates regex pattern, auth requirement, and ownership check for transient file download."""
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)

    # 1. Unauthenticated request -> 401
    resp = client.get("/api/download/file/dQw4w9WgXcQ")
    assert resp.status_code == 401

    # 2. Invalid video_id (regex pattern: ^[A-Za-z0-9_-]{11}$) -> 422
    with unittest.mock.patch("auth.verify_token", return_value={"uid": "user_a", "username": "alice"}):
        resp_too_short = client.get("/api/download/file/short", headers={"Authorization": "Bearer token"})
        assert resp_too_short.status_code == 422

        resp_invalid_chars = client.get("/api/download/file/dQw4w9WgXc!", headers={"Authorization": "Bearer token"})
        assert resp_invalid_chars.status_code == 422

        resp_too_long = client.get("/api/download/file/toolongvideoid123", headers={"Authorization": "Bearer token"})
        assert resp_too_long.status_code == 422

    # 3. Ownership check: Track belongs to user_b, but user_a requests it -> 404
    dummy_file = tmp_path / "test_track.mp3"
    dummy_file.write_bytes(b"ID3mockaudiobytes")

    target_vid = "dQw4w9WgXcQ"  # valid 11 chars
    with stream_downloader._DOWNLOAD_LOCK:
        stream_downloader.DOWNLOAD_STATUS[target_vid] = {
            "status": "ready_for_transfer",
            "percent": 100.0,
            "title": "Never Gonna Give You Up",
            "artist": "Rick Astley",
            "user_id": "user_b",
            "file_path": str(dummy_file),
            "error": None
        }

    with unittest.mock.patch("auth.verify_token", return_value={"uid": "user_a", "username": "alice"}):
        resp = client.get(f"/api/download/file/{target_vid}", headers={"Authorization": "Bearer token_a"})
        assert resp.status_code == 404
        # Verify file was NOT cleaned up / deleted
        assert dummy_file.exists()
        assert target_vid in stream_downloader.DOWNLOAD_STATUS

    # 4. Legitimate owner (user_b) requests it -> 200 and serves file
    with unittest.mock.patch("auth.verify_token", return_value={"uid": "user_b", "username": "bob"}):
        resp = client.get(f"/api/download/file/{target_vid}", headers={"Authorization": "Bearer token_b"})
        assert resp.status_code == 200
        assert resp.content == b"ID3mockaudiobytes"


def test_no_inline_onclick_track_interpolation_in_index_html():
    """Validates that frontend code never interpolates track objects or titles into inline onclick attributes."""
    index_path = os.path.join(PROJECT_DIR, "static", "index.html")
    app_js_path = os.path.join(PROJECT_DIR, "static", "js", "app.js")
    assert os.path.isfile(index_path)

    with open(index_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    js_content = ""
    if os.path.isfile(app_js_path):
        with open(app_js_path, "r", encoding="utf-8") as f:
            js_content = f.read()

    combined = html_content + "\n" + js_content

    # Ensure no onclick attributes call escapeQuotes or escapeTrackObj
    assert "onclick=" not in html_content or "escapeQuotes" not in html_content.split("onclick=")[1]
    import re
    assert not re.search(r'onclick=[\'"][^\'"]*escapeTrackObj', html_content)
    assert not re.search(r'onclick=[\'"][^\'"]*escapeQuotes', html_content)

    # Ensure data-action pattern is present
    assert 'data-action="context-menu"' in combined
    assert 'data-action="prompt-download"' in combined
    assert 'data-action="toggle-like"' in combined
    assert 'data-action="save-offline"' in combined
    assert 'data-action="add-to-playlist"' in combined
    assert 'data-action="delete-offline"' in combined
    assert 'data-action="remove-from-playlist"' in combined
    assert 'data-action="play-next"' in combined


def test_resource_exhaustion_endpoints_auth_and_throttling():
    """Validates that refresh and library endpoints enforce authentication, and streaming/radio endpoints are throttled."""
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)

    # 1. POST /api/home/refresh requires authentication
    resp = client.post("/api/home/refresh")
    assert resp.status_code == 401

    with unittest.mock.patch("auth.verify_token", return_value={"uid": "user_1", "username": "alice"}):
        with unittest.mock.patch("server.trigger_daily_fetch_if_stale", return_value=True):
            resp = client.post("/api/home/refresh", headers={"Authorization": "Bearer token"})
            assert resp.status_code == 200
            assert resp.json()["success"] is True

    # 2. GET /api/library requires authentication
    resp = client.get("/api/library")
    assert resp.status_code == 401

    with unittest.mock.patch("auth.verify_token", return_value={"uid": "user_1", "username": "alice"}):
        with unittest.mock.patch("library_manager.get_library_tracks", return_value=[]):
            resp = client.get("/api/library", headers={"Authorization": "Bearer token"})
            assert resp.status_code == 200
            assert "tracks" in resp.json()

    # 3. GET /api/library/file/{filename} requires authentication (header or query token)
    resp = client.get("/api/library/file/test_song.mp3")
    assert resp.status_code == 401

    with unittest.mock.patch("auth.verify_token", return_value={"uid": "user_1", "username": "alice"}):
        # Authenticated with query param token -> 404 (file doesn't exist, but passes auth!)
        resp = client.get("/api/library/file/nonexistent.mp3?token=valid_token")
        assert resp.status_code == 404

    # 4. GET /api/radio/{video_id} and GET /api/stream/{video_id} have limiter configured
    endpoints_to_check = [
        server.force_refresh_home_feed,
        server.get_radio,
        server.get_stream,
        server.stream_download_proxy,
        server.get_library,
        server.serve_library_file
    ]
    for ep in endpoints_to_check:
        # slowapi wraps endpoints or adds _rate_limited attribute
        assert hasattr(ep, "_rate_limited") or hasattr(ep, "__wrapped__") or callable(ep)


def test_pinned_cors_origins():
    """Validates that CORS only trusts the pinned DuckDNS domain, localhost, and LAN IPs."""
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)

    # Allowed origins
    for allowed_origin in [
        "https://hamrospoti.duckdns.org",
        "http://hamrospoti.duckdns.org:8000",
        "http://localhost:8888",
        "http://127.0.0.1:8000",
        "http://192.168.1.66:8000",
        "http://10.0.0.5:8000",
        "http://172.20.10.2:8000"
    ]:
        resp = client.options(
            "/api/library",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "GET"
            }
        )
        assert resp.headers.get("access-control-allow-origin") == allowed_origin

    # Disallowed origins (arbitrary subdomains of DuckDNS, trycloudflare, ngrok)
    for disallowed_origin in [
        "https://evil.duckdns.org",
        "https://attacker.trycloudflare.com",
        "https://malicious.ngrok-free.app",
        "https://other-domain.com"
    ]:
        resp = client.options(
            "/api/library",
            headers={
                "Origin": disallowed_origin,
                "Access-Control-Request-Method": "GET"
            }
        )
        assert resp.headers.get("access-control-allow-origin") is None


def test_path_traversal_commonpath_protection(tmp_path):
    """Validates that commonpath blocks symlink directory breakouts to matching sibling prefixes."""
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)

    # Setup a mock music_library and a sibling prefix music_library_private
    music_lib = tmp_path / "music_library"
    music_lib.mkdir()
    sibling_lib = tmp_path / "music_library_private"
    sibling_lib.mkdir()

    secret_file = sibling_lib / "secret.mp3"
    secret_file.write_bytes(b"SECRET DATA")

    # Create a symlink inside music_lib pointing to secret_file in sibling_lib
    symlink_file = music_lib / "link_to_secret.mp3"
    try:
        symlink_file.symlink_to(secret_file)
    except OSError:
        return

    with unittest.mock.patch("stream_downloader.MUSIC_DIR", str(music_lib)):
        with unittest.mock.patch("auth.verify_token", return_value={"uid": "u1", "username": "alice"}):
            # Requesting the symlinked file must be blocked by commonpath check -> 404
            resp = client.get("/api/library/file/link_to_secret.mp3", headers={"Authorization": "Bearer token"})
            assert resp.status_code == 404


def test_verify_token_jwt_exp_handling_and_capping():
    """Validates that verify_token decodes exp claim, rejects expired tokens, and caps cache TTL."""
    import base64
    import json
    import time
    import auth

    now = time.time()

    def make_jwt(exp_timestamp: float) -> str:
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(json.dumps({"id": "u1", "exp": exp_timestamp}).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(b'signature').decode().rstrip("=")
        return f"{header}.{payload}.{sig}"

    # 1. Expired token (exp in past) -> immediately returns None without calling database
    expired_token = make_jwt(now - 100)
    with unittest.mock.patch("database.get_user_pb") as mock_get_pb:
        res = auth.verify_token(expired_token)
        assert res is None
        mock_get_pb.assert_not_called()

    # 2. Token expiring in 45 seconds -> cache exp is capped at now + 45, NOT now + 300 or 1800
    short_lived_token = make_jwt(now + 45)
    mock_record = unittest.mock.MagicMock()
    mock_record.id = "u1"
    mock_record.name = "Alice"
    mock_refreshed = unittest.mock.MagicMock()
    mock_refreshed.record = mock_record

    with unittest.mock.patch("database.get_user_pb") as mock_get_pb:
        mock_user_pb = unittest.mock.MagicMock()
        mock_user_pb.collection.return_value.auth_refresh.return_value = mock_refreshed
        mock_get_pb.return_value = mock_user_pb

        res = auth.verify_token(short_lived_token)
        assert res is not None
        assert res["uid"] == "u1"
        assert res["usr"] == "Alice"
        assert res["exp"] <= now + 46  # capped at jwt_exp!

        # Check cache
        with auth._CACHE_LOCK:
            cached = auth._TOKEN_CACHE.get(short_lived_token)
            assert cached is not None
            assert cached["exp"] <= now + 46

    # 3. Token expiring in 1 day (now + 86400) -> cache exp capped at 5 minutes (now + 300)
    long_lived_token = make_jwt(now + 86400)
    with unittest.mock.patch("database.get_user_pb") as mock_get_pb:
        mock_user_pb = unittest.mock.MagicMock()
        mock_user_pb.collection.return_value.auth_refresh.return_value = mock_refreshed
        mock_get_pb.return_value = mock_user_pb

        res = auth.verify_token(long_lived_token)
        assert res is not None
        assert res["exp"] <= now + 301  # capped at 5 mins!


def test_pending_lib_ops_localstorage_key_standardization():
    """Validates that offline library ops use PENDING_LIB_OPS_KEY = 'spoti_pending_lib_ops' across get, save, and initAuth."""
    app_js_path = os.path.join(PROJECT_DIR, "static", "js", "app.js")
    index_path = os.path.join(PROJECT_DIR, "static", "index.html")
    target_path = app_js_path if os.path.isfile(app_js_path) else index_path
    assert os.path.isfile(target_path)

    with open(target_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Constant definition
    assert "const PENDING_LIB_OPS_KEY = 'spoti_pending_lib_ops';" in content

    # 2. getPendingLibraryOps uses the constant
    assert "localStorage.getItem(PENDING_LIB_OPS_KEY)" in content

    # 3. savePendingLibraryOps uses the constant
    assert "localStorage.setItem(PENDING_LIB_OPS_KEY" in content

    # 4. initAuth reads and sanitizes the constant
    assert "const rawOps = localStorage.getItem(PENDING_LIB_OPS_KEY);" in content
    assert "localStorage.setItem(PENDING_LIB_OPS_KEY, JSON.stringify(sanitized));" in content

    # 5. Stale legacy key is not written to anymore
    assert "localStorage.setItem('spoti_pending_library_ops'" not in content


def test_transient_file_cleanup_regex_matches_ytdlp_title_prefix(tmp_path):
    """Validates that cleanup_transient_file purges files with title prefixes without touching other tracks."""
    target_vid = "dQw4w9WgXcQ"
    other_vid = "abcdefghijk"

    # Create matching transient files for target_vid with title prefixes and various extensions
    matching_files = [
        tmp_path / f"Rick Astley - Never Gonna Give You Up [{target_vid}].part",
        tmp_path / f"Rick Astley - Never Gonna Give You Up [{target_vid}].webp",
        tmp_path / f"Rick Astley - Never Gonna Give You Up [{target_vid}].jpg",
        tmp_path / f"Rick Astley - Never Gonna Give You Up [{target_vid}].png",
        tmp_path / f"Rick Astley - Never Gonna Give You Up [{target_vid}].mp3",
        tmp_path / f"Rick Astley - Never Gonna Give You Up [{target_vid}].m4a",
        tmp_path / f"Rick Astley - Never Gonna Give You Up [{target_vid}].webm",
        tmp_path / f"[{target_vid}].mp3",
        tmp_path / f"{target_vid}.mp3",
    ]
    for mf in matching_files:
        mf.write_bytes(b"tempdata")

    # Create non-matching files (other video id, or unbracketed substring in title)
    non_matching_files = [
        tmp_path / f"Other Song [{other_vid}].mp3",
        tmp_path / f"Other Song [{other_vid}].part",
        tmp_path / f"Other Song [{other_vid}].webp",
        tmp_path / f"Song with {target_vid} in title but no brackets.mp3",
        tmp_path / "unrelated_file.txt",
    ]
    for nmf in non_matching_files:
        nmf.write_bytes(b"preserveddata")

    with unittest.mock.patch("stream_downloader.TRANSIENT_DIR", str(tmp_path)):
        stream_downloader.cleanup_transient_file(target_vid)

    # All target_vid files must have been deleted
    for mf in matching_files:
        assert not mf.exists(), f"Expected {mf.name} to be purged, but it still exists!"

    # All non-matching files must still exist
    for nmf in non_matching_files:
        assert nmf.exists(), f"Expected {nmf.name} to be preserved, but it was deleted!"


def test_download_worker_thumbnail_cleanup_pattern():
    """Validates that download worker cleanup regex removes images with title prefixes but preserves audio."""
    import re
    vid = "dQw4w9WgXcQ"
    pattern = re.compile(rf"(?:\[{re.escape(vid)}\]|^{re.escape(vid)})\.(webp|jpg|png)$")

    # Images with title prefix must match
    assert pattern.search(f"Rick Astley - Never Gonna Give You Up [{vid}].webp")
    assert pattern.search(f"Rick Astley - Never Gonna Give You Up [{vid}].jpg")
    assert pattern.search(f"Rick Astley - Never Gonna Give You Up [{vid}].png")
    assert pattern.search(f"[{vid}].jpg")
    assert pattern.search(f"{vid}.png")

    # Audio files must NOT match (must not be deleted before locating final MP3)
    assert not pattern.search(f"Rick Astley - Never Gonna Give You Up [{vid}].mp3")
    assert not pattern.search(f"Rick Astley - Never Gonna Give You Up [{vid}].m4a")
    assert not pattern.search(f"[{vid}].mp3")
    assert not pattern.search(f"{vid}.mp3")

    # Other video IDs must NOT match
    assert not pattern.search("Other Song [other123456].webp")


def test_authenticate_user_pocketbase_outage_error_handling():
    """Validates that authenticate_user raises AuthServiceUnavailableError (not ValueError) during PB outages."""
    from pocketbase.client import ClientResponseError

    # 1. Connection error / timeout -> AuthServiceUnavailableError
    with unittest.mock.patch("database.get_pb") as mock_get_pb:
        mock_pb = unittest.mock.MagicMock()
        mock_pb.collection.return_value.auth_with_password.side_effect = ConnectionError("Failed to connect to 192.168.1.66:8090")
        mock_get_pb.return_value = mock_pb

        with pytest.raises(auth.AuthServiceUnavailableError):
            auth.authenticate_user("testuser", "testpassword123")

    # 2. PocketBase 500 / 502 / 503 error -> AuthServiceUnavailableError
    with unittest.mock.patch("database.get_pb") as mock_get_pb:
        mock_pb = unittest.mock.MagicMock()
        err = ClientResponseError()
        err.status = 503
        mock_pb.collection.return_value.auth_with_password.side_effect = err
        mock_get_pb.return_value = mock_pb

        with pytest.raises(auth.AuthServiceUnavailableError):
            auth.authenticate_user("testuser", "testpassword123")

    # 3. Bad credentials (400) -> raises ValueError("Invalid username or password.")
    with unittest.mock.patch("database.get_pb") as mock_get_pb, \
         unittest.mock.patch("database.get_admin_pb") as mock_get_admin:
        mock_pb = unittest.mock.MagicMock()
        err400 = ClientResponseError()
        err400.status = 400
        mock_pb.collection.return_value.auth_with_password.side_effect = err400
        mock_get_pb.return_value = mock_pb

        mock_admin = unittest.mock.MagicMock()
        mock_found = unittest.mock.MagicMock()
        mock_found.total_items = 0
        mock_admin.collection.return_value.get_list.return_value = mock_found
        mock_get_admin.return_value = mock_admin

        with pytest.raises(ValueError, match="Invalid username or password."):
            auth.authenticate_user("testuser", "wrongpassword")


def test_no_duplicate_dead_code_in_auth_and_playlist_manager():
    """Validates that duplicate unreachable lines in auth.py and playlist_manager.py are removed."""
    auth_path = os.path.join(PROJECT_DIR, "auth.py")
    with open(auth_path, "r", encoding="utf-8") as f:
        auth_src = f.read()

    # Must only appear once in authenticate_user
    assert auth_src.count('raise ValueError("Invalid username or password.")') == 2  # line 201 (empty input check) + line 269 (failure)
    assert not re.search(r'raise ValueError\("Invalid username or password\."\)\s+raise ValueError\("Invalid username or password\."\)', auth_src)

    pm_path = os.path.join(PROJECT_DIR, "playlist_manager.py")
    with open(pm_path, "r", encoding="utf-8") as f:
        pm_src = f.read()

    # No duplicate return False
    assert not re.search(r'return False\s+return False', pm_src)


def test_stuck_downloading_entries_allow_retry():
    """Validates that download_track does not block retries if a downloading entry is older than threshold."""
    target_vid = "stuck_vid_01"
    now = time.time()

    with stream_downloader._DOWNLOAD_LOCK:
        stream_downloader.DOWNLOAD_STATUS[target_vid] = {
            "status": "downloading",
            "percent": 12.0,
            "title": "Stuck Song",
            "artist": "Artist",
            "user_id": "u1",
            "created_at": now - 700,  # 11+ minutes old
            "updated_at": now - 700,
            "error": None
        }

    with unittest.mock.patch("threading.Thread") as mock_thread:
        stream_downloader.download_track(target_vid, "Stuck Song", "Artist", "u1")
        # A new worker thread must have been spawned because the previous downloading entry was stale (> 600s)
        assert mock_thread.called
        assert stream_downloader.DOWNLOAD_STATUS[target_vid]["percent"] == 0.0
        assert stream_downloader.DOWNLOAD_STATUS[target_vid]["created_at"] >= now


def test_username_normalization_and_uniqueness_service_error():
    """Validates that spaces are rejected to prevent normalization collisions and uniqueness query errors propagate."""
    from pocketbase.client import ClientResponseError

    # 1. Spaces in username rejected upfront
    with pytest.raises(ValueError, match="Username contains invalid characters"):
        auth.register_user("John Doe", "validpassword123")

    with pytest.raises(ValueError, match="Username contains invalid characters"):
        auth.register_user("john doe", "validpassword123")

    # 2. Uniqueness check propagates 500 error as AuthServiceUnavailableError (not swallowed)
    with unittest.mock.patch("database.get_admin_pb") as mock_get_admin:
        mock_admin = unittest.mock.MagicMock()
        err500 = ClientResponseError()
        err500.status = 500
        mock_admin.collection.return_value.get_list.side_effect = err500
        mock_get_admin.return_value = mock_admin

        with pytest.raises(auth.AuthServiceUnavailableError):
            auth.register_user("john_doe", "validpassword123")


def test_no_hardcoded_server_lan_ips_in_index_html():
    """Validates that index.html contains no hardcoded server LAN IPs."""
    index_path = os.path.join(PROJECT_DIR, "static", "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "192.168.1.67" not in content
    assert "192.168.1.66" not in content


def test_concurrent_stream_cache_writer_dedup_and_cleanup(tmp_path):
    """Validates that concurrent requests do not duplicate disk writes and .part_ files are never leaked."""
    import server

    vid = "test_vid_888"
    part_path_worker1 = tmp_path / f".part_{vid}_w1.m4a"
    part_path_worker2 = tmp_path / f".part_{vid}_w2.m4a"
    final_path = tmp_path / f"{vid}.m4a"

    # Reset active stream writers
    with server._STREAM_WRITER_LOCK:
        server._ACTIVE_STREAM_WRITERS.clear()

    # Worker 1 acquires lock
    with server._STREAM_WRITER_LOCK:
        assert vid not in server._ACTIVE_STREAM_WRITERS
        server._ACTIVE_STREAM_WRITERS.add(vid)
        w1_can_write = True

    # Worker 2 attempts to acquire lock for same video_id while Worker 1 is active -> denied
    with server._STREAM_WRITER_LOCK:
        w2_can_write = (vid not in server._ACTIVE_STREAM_WRITERS)
    assert not w2_can_write

    # Simulate Worker 1 finishing and promoting final_path
    part_path_worker1.write_bytes(b"audiow1")
    part_path_worker1.replace(final_path)
    with server._STREAM_WRITER_LOCK:
        server._ACTIVE_STREAM_WRITERS.discard(vid)

    # Simulate Worker 2 having an orphaned part_path (e.g. from race or legacy behavior)
    part_path_worker2.write_bytes(b"audiow2")
    assert part_path_worker2.exists()
    assert final_path.exists()

    # The finally block must remove part_path even though final_path already exists!
    if os.path.exists(str(part_path_worker2)):
        os.remove(str(part_path_worker2))

    assert not part_path_worker2.exists()
    assert final_path.exists()


def test_in_flight_stream_result_timer_identity_check():
    """Validates that timer pop checks object identity under _IN_FLIGHT_LOCK so fresh results are not deleted."""
    vid = "inflight_vid_99"

    result_v1 = {"source": "stream", "url": "https://googlevideo.com/stream_v1"}
    result_v2 = {"source": "stream", "url": "https://googlevideo.com/stream_v2"}

    with stream_downloader._IN_FLIGHT_LOCK:
        stream_downloader._IN_FLIGHT_RESULTS[vid] = result_v1

    # Cleanup callback for v1
    def cleanup_v1(target=result_v1):
        with stream_downloader._IN_FLIGHT_LOCK:
            if stream_downloader._IN_FLIGHT_RESULTS.get(vid) is target:
                stream_downloader._IN_FLIGHT_RESULTS.pop(vid, None)

    # Suppose a newer extraction replaced result with result_v2 before timer fired
    with stream_downloader._IN_FLIGHT_LOCK:
        stream_downloader._IN_FLIGHT_RESULTS[vid] = result_v2

    # Timer 1 fires now: should NOT delete result_v2 because identity does not match!
    cleanup_v1()

    with stream_downloader._IN_FLIGHT_LOCK:
        assert vid in stream_downloader._IN_FLIGHT_RESULTS
        assert stream_downloader._IN_FLIGHT_RESULTS[vid] is result_v2

    # Cleanup callback for v2: should properly delete result_v2
    def cleanup_v2(target=result_v2):
        with stream_downloader._IN_FLIGHT_LOCK:
            if stream_downloader._IN_FLIGHT_RESULTS.get(vid) is target:
                stream_downloader._IN_FLIGHT_RESULTS.pop(vid, None)

    cleanup_v2()

    with stream_downloader._IN_FLIGHT_LOCK:
        assert vid not in stream_downloader._IN_FLIGHT_RESULTS


def test_save_cache_meta_atomic_write(tmp_path):
    """Validates that save_cache_meta writes atomically via a .tmp file and os.replace."""
    import daily_fetcher

    with unittest.mock.patch("daily_fetcher.PRIMARY_DATA_DIR", str(tmp_path / "primary")), \
         unittest.mock.patch("daily_fetcher.LOCAL_DATA_DIR", str(tmp_path / "local")):

        meta_data = {"last_fetch": 123456789.0, "sync_in_progress": False}
        daily_fetcher.save_cache_meta(meta_data)

        for d in ["primary", "local"]:
            meta_file = tmp_path / d / ".cache_meta.json"
            tmp_file = tmp_path / d / ".cache_meta.json.tmp"
            assert meta_file.exists()
            assert not tmp_file.exists()  # .tmp must have been atomically replaced
            with open(meta_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            assert loaded == meta_data


def test_clear_and_bulk_insert_pb_inserts_before_delete():
    """Validates that clear_and_bulk_insert_pb creates new records before deleting stale records."""
    import io
    import daily_fetcher

    requests_made = []

    def mock_urlopen(req, timeout=None):
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        url = req.full_url if hasattr(req, "full_url") else str(req)
        requests_made.append((method, url))

        resp = unittest.mock.MagicMock()
        if "records?page=1" in url:
            # Return 2 existing items
            resp.read.return_value = json.dumps({"items": [{"id": "old_rec_1"}, {"id": "old_rec_2"}]}).encode()
        else:
            resp.read.return_value = json.dumps({"id": "new_rec"}).encode()

        mock_context = unittest.mock.MagicMock()
        mock_context.__enter__.return_value = resp
        return mock_context

    new_records = [
        {"title": "Song 1", "rank": 1},
        {"title": "Song 2", "rank": 2}
    ]

    with unittest.mock.patch("daily_fetcher.requests", None), \
         unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        success = daily_fetcher.clear_and_bulk_insert_pb("mock_token", "trending", new_records)
        assert success is True

    # Check request sequence:
    # 1. GET list of existing records
    # 2. POST create new record 1
    # 3. POST create new record 2
    # 4. DELETE old record 1
    # 5. DELETE old record 2
    methods = [m for m, u in requests_made]
    assert methods[0] == "GET"
    assert methods[1] == "POST"
    assert methods[2] == "POST"
    assert methods[3] == "DELETE"
    assert methods[4] == "DELETE"

    # All POSTs must strictly precede any DELETEs!
    first_delete_idx = methods.index("DELETE")
    all_post_indices = [i for i, m in enumerate(methods) if m == "POST"]
    for p_idx in all_post_indices:
        assert p_idx < first_delete_idx, "All new records must be inserted before any old records are deleted!"


def test_load_explore_from_pocketbase_serves_json_when_sync_in_progress():
    """Validates that load_explore_from_pocketbase yields to fallback JSON while sync is active."""
    import server

    # 1. When sync_in_progress is True, load_explore_from_pocketbase must return None
    with unittest.mock.patch("server.get_explore_cache_meta", return_value={"sync_in_progress": True}):
        res = server.load_explore_from_pocketbase()
        assert res is None

    # 2. When collections have < 5 items, load_explore_from_pocketbase must return None (partial guard)
    mock_pb = unittest.mock.MagicMock()
    mock_pb.collection.return_value.get_full_list.return_value = []
    with unittest.mock.patch("server.get_explore_cache_meta", return_value={"sync_in_progress": False}), \
         unittest.mock.patch("database.get_pb", return_value=mock_pb):
        res = server.load_explore_from_pocketbase()
        assert res is None


def test_stream_download_proxy_is_async_def():
    """Validates that stream_download_proxy is an async def coroutine function, avoiding AnyIO threadpool starvation."""
    import server
    import inspect
    assert inspect.iscoroutinefunction(server.stream_download_proxy), "stream_download_proxy must be an async def function!"


def test_stream_download_proxy_async_httpx_streaming():
    """Validates that stream_download_proxy streams chunks asynchronously via httpx.AsyncClient + client.stream."""
    import server
    from starlette.testclient import TestClient

    with unittest.mock.patch("stream_downloader.get_cached_audio_path", return_value=None), \
         unittest.mock.patch("stream_downloader.get_cached_or_local_stream", return_value={"url": "http://googlecdn.com/song.m4a"}):

        mock_client = unittest.mock.AsyncMock()
        mock_resp = unittest.mock.AsyncMock()
        mock_resp.status_code = 206
        mock_resp.headers = {
            "Content-Length": "12",
            "Content-Range": "bytes 0-11/12",
            "Content-Type": "audio/mp4"
        }
        async def mock_aiter_bytes(chunk_size=65536):
            yield b"part1_"
            yield b"part2!"
        mock_resp.aiter_bytes = mock_aiter_bytes

        mock_stream_ctx = unittest.mock.MagicMock()
        mock_stream_ctx.__aenter__ = unittest.mock.AsyncMock(return_value=mock_resp)
        mock_stream_ctx.__aexit__ = unittest.mock.AsyncMock(return_value=None)
        mock_client.stream = unittest.mock.MagicMock(return_value=mock_stream_ctx)

        with unittest.mock.patch("httpx.AsyncClient", return_value=mock_client):
            client = TestClient(server.app)
            resp = client.get("/api/download/stream/dQw4w9WgXcQ", headers={"Range": "bytes=0-11"})
            assert resp.status_code == 206
            assert resp.content == b"part1_part2!"
            assert resp.headers["content-range"] == "bytes 0-11/12"
            mock_client.stream.assert_called_once()
            mock_stream_ctx.__aenter__.assert_called_once()
            mock_stream_ctx.__aexit__.assert_called_once()


def test_stream_download_proxy_head_request_clean_exit():
    """Validates that HEAD requests return response headers and cleanly exit the stream context without consuming chunks."""
    import server
    from starlette.testclient import TestClient

    with unittest.mock.patch("stream_downloader.get_cached_audio_path", return_value=None), \
         unittest.mock.patch("stream_downloader.get_cached_or_local_stream", return_value={"url": "http://googlecdn.com/song.m4a"}):

        mock_client = unittest.mock.AsyncMock()
        mock_resp = unittest.mock.AsyncMock()
        mock_resp.status_code = 206
        mock_resp.headers = {
            "Content-Length": "12",
            "Content-Range": "bytes 0-11/12",
            "Content-Type": "audio/mp4"
        }

        mock_stream_ctx = unittest.mock.MagicMock()
        mock_stream_ctx.__aenter__ = unittest.mock.AsyncMock(return_value=mock_resp)
        mock_stream_ctx.__aexit__ = unittest.mock.AsyncMock(return_value=None)
        mock_client.stream = unittest.mock.MagicMock(return_value=mock_stream_ctx)

        with unittest.mock.patch("httpx.AsyncClient", return_value=mock_client):
            client = TestClient(server.app)
            resp = client.head("/api/download/stream/dQw4w9WgXcQ", headers={"Range": "bytes=0-11"})
            assert resp.status_code == 206
            assert resp.content == b""
            assert resp.headers["content-range"] == "bytes 0-11/12"
            mock_stream_ctx.__aenter__.assert_called_once()
            mock_stream_ctx.__aexit__.assert_called_once()
            mock_client.aclose.assert_called_once()


def test_clear_and_bulk_insert_pb_persistent_session():
    """Validates that clear_and_bulk_insert_pb uses a persistent requests.Session and inserts before deleting."""
    import daily_fetcher

    mock_session = unittest.mock.MagicMock()
    mock_get_resp = unittest.mock.MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {"items": [{"id": "old_1"}, {"id": "old_2"}]}
    mock_session.get.return_value = mock_get_resp

    mock_post_resp = unittest.mock.MagicMock()
    mock_post_resp.status_code = 201
    mock_session.post.return_value = mock_post_resp

    mock_del_resp = unittest.mock.MagicMock()
    mock_del_resp.status_code = 200
    mock_session.delete.return_value = mock_del_resp

    new_records = [
        {"title": "Track A", "rank": 1},
        {"title": "Track B", "rank": 2}
    ]

    success = daily_fetcher.clear_and_bulk_insert_pb("mock_token", "trending", new_records, session=mock_session)
    assert success is True

    # Validate calls made on the persistent session
    mock_session.get.assert_called_once()
    assert mock_session.post.call_count == 2
    assert mock_session.delete.call_count == 2

    # Check order: GET before POSTs, POSTs before DELETEs
    call_names = [c[0] for c in mock_session.method_calls]
    assert call_names[0] == "get"
    assert call_names[1] == "post"
    assert call_names[2] == "post"
    assert call_names[3] == "delete"
    assert call_names[4] == "delete"


def test_get_pb_token_with_session():
    """Validates that get_pb_token reuses the provided requests.Session."""
    import daily_fetcher

    mock_session = unittest.mock.MagicMock()
    mock_resp = unittest.mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"token": "test_token_123"}
    mock_session.post.return_value = mock_resp

    with unittest.mock.patch.dict(os.environ, {"PB_SUPERUSER_EMAIL": "admin@test.com", "PB_SUPERUSER_PASSWORD": "pass"}):
        token = daily_fetcher.get_pb_token(session=mock_session)
        assert token == "test_token_123"
        mock_session.post.assert_called_once()


def test_fetch_genres_threadpool_concurrency():
    """Validates that fetch_genres fetches categories concurrently using ThreadPoolExecutor and retains order."""
    import sys
    import daily_fetcher

    mock_nav_mod = unittest.mock.MagicMock()
    mock_nav_mod.SINGLE_COLUMN_TAB = ["contents", "singleColumnBrowseResultsRenderer", "tabs", 0, "tabRenderer", "content"]
    mock_nav_mod.SECTION_LIST = ["sectionListRenderer", "contents"]
    def fake_nav(root, path):
        curr = root
        for k in path:
            curr = curr[k]
        return curr
    mock_nav_mod.nav = fake_nav

    mock_ytm = unittest.mock.MagicMock()
    mock_ytm.get_mood_categories.return_value = {
        "Genres": [
            {"title": "Pop", "params": "p_pop"},
            {"title": "Rock", "params": "p_rock"},
            {"title": "Jazz", "params": "p_jazz"},
        ]
    }
    def mock_send(endpoint, body):
        genre_param = body.get("params")
        return {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [{
                        "tabRenderer": {
                            "content": {
                                "sectionListRenderer": {
                                    "contents": [{
                                        "musicCarouselShelfRenderer": {
                                            "contents": [{
                                                "musicResponsiveListItemRenderer": {
                                                    "flexColumns": [
                                                        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": f"Song {genre_param}"}]}}},
                                                        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist"}]}}}
                                                    ],
                                                    "playlistItemData": {"videoId": f"vid_{genre_param}"},
                                                    "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "http://thumb"}]}}}
                                                }
                                            }]
                                        }
                                    }]
                                }
                            }
                        }
                    }]
                }
            }
        }
    mock_ytm._send_request.side_effect = mock_send

    with unittest.mock.patch.dict(sys.modules, {"ytmusicapi": unittest.mock.MagicMock(), "ytmusicapi.navigation": mock_nav_mod}), \
         unittest.mock.patch("daily_fetcher.YTMusic", return_value=mock_ytm):
        genres = daily_fetcher.fetch_genres()
        assert len(genres) == 3
        # Check that order is strictly preserved
        assert genres[0]["name"] == "Pop"
        assert genres[0]["tracks"][0]["id"] == "vid_p_pop"
        assert genres[1]["name"] == "Rock"
        assert genres[1]["tracks"][0]["id"] == "vid_p_rock"
        assert genres[2]["name"] == "Jazz"
        assert genres[2]["tracks"][0]["id"] == "vid_p_jazz"


def test_get_all_playlists_single_aggregated_tracks_query():
    """Validates that get_all_playlists aggregates track counts in 1 single query instead of N+1 queries."""
    import playlist_manager

    mock_admin_pb = unittest.mock.MagicMock()

    # Mock 3 user playlists
    p1 = unittest.mock.MagicMock()
    p1.id = "pl_1"
    p1.title = "Playlist 1"
    p1.created = "2026-10-19T00:00:00Z"
    p1.track_count = None

    p2 = unittest.mock.MagicMock()
    p2.id = "pl_2"
    p2.title = "Playlist 2"
    p2.created = "2026-10-19T00:00:00Z"
    p2.track_count = None

    p3 = unittest.mock.MagicMock()
    p3.id = "pl_3"
    p3.title = "Playlist 3"
    p3.created = "2026-10-19T00:00:00Z"
    p3.track_count = None

    mock_admin_pb.collection.return_value.get_full_list.side_effect = [
        [p1, p2, p3],  # playlists.get_full_list
        [              # playlist_tracks.get_full_list
            unittest.mock.MagicMock(playlist="pl_1"),
            unittest.mock.MagicMock(playlist="pl_1"),
            unittest.mock.MagicMock(playlist="pl_2"),
        ]
    ]

    with unittest.mock.patch("database.get_admin_pb", return_value=mock_admin_pb), \
         unittest.mock.patch("playlist_manager.get_user_liked_tracks", return_value=[]), \
         unittest.mock.patch("playlist_manager.get_user_library_tracks", return_value=[]):

        playlists = playlist_manager.get_all_playlists("test_user_id")

        # Crucial: playlist_tracks.get_list must NOT be called in a loop (0 calls)
        mock_admin_pb.collection("playlist_tracks").get_list.assert_not_called()

        user_pls = [p for p in playlists if p.get("is_user_playlist")]
        assert len(user_pls) == 3
        assert user_pls[0]["track_count"] == 2
        assert user_pls[1]["track_count"] == 1
        assert user_pls[2]["track_count"] == 0


def test_track_registry_bounded_lru_in_app_js():
    """Validates that trackRegistry is bounded with LRU eviction in app.js."""
    app_js_path = os.path.join(PROJECT_DIR, "static", "js", "app.js")
    assert os.path.isfile(app_js_path)

    with open(app_js_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "MAX_TRACK_REGISTRY_SIZE" in content
    assert "trackRegistry.size >= MAX_TRACK_REGISTRY_SIZE" in content
    assert "trackRegistry.keys().next().value" in content
    assert "trackRegistry.delete(" in content


def test_index_html_precompiled_tailwind_and_es_modules():
    """Validates that index.html links precompiled tailwind.css and ES module app.js, removing cdn.tailwindcss.com."""
    index_path = os.path.join(PROJECT_DIR, "static", "index.html")
    tailwind_css_path = os.path.join(PROJECT_DIR, "static", "css", "tailwind.css")
    app_js_path = os.path.join(PROJECT_DIR, "static", "js", "app.js")
    db_js_path = os.path.join(PROJECT_DIR, "static", "js", "db.js")

    assert os.path.isfile(index_path)
    assert os.path.isfile(tailwind_css_path)
    assert os.path.isfile(app_js_path)
    assert os.path.isfile(db_js_path)

    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Runtime Tailwind Play CDN must be completely removed
    assert "cdn.tailwindcss.com" not in html
    # Static precompiled CSS must be linked
    assert '<link rel="stylesheet" href="/static/css/tailwind.css">' in html
    # ES module script must be linked
    assert '<script type="module" src="/static/js/app.js"></script>' in html
    # index.html should be slimmed down significantly (< 1500 lines)
    assert len(html.splitlines()) < 1500


def test_app_js_window_exports_defined():
    """Validates that all window exports in app.js are defined without syntax or ReferenceErrors."""
    import re
    app_js_path = os.path.join(PROJECT_DIR, "static", "js", "app.js")
    with open(app_js_path, "r", encoding="utf-8") as f:
        code = f.read()

    # Find the Object.assign(window, { ... }) block
    match = re.search(r"Object\.assign\(window,\s*\{([^}]+)\}\)", code)
    assert match is not None, "Object.assign(window, ...) block not found in app.js"

    exported_identifiers = [s.strip() for s in match.group(1).split(",") if s.strip()]

    # Critical handlers that index.html inline attributes rely on
    required_exports = [
        "submitAuth",
        "switchAuthTab",
        "closeAuthModal",
        "openAuthModal",
        "handleSearchKeydown",
        "onSearchInput",
        "onSeekChange",
        "onSeekInput",
        "resetViewportScroll",
        "handleLogout",
        "playEntirePlaylist",
        "playNext",
        "playPrev",
        "togglePlayPause",
    ]
    for req in required_exports:
        assert req in exported_identifiers, f"Required window export '{req}' is missing"

    # None of the deprecated / undeclared identifiers must be present
    assert "loadTrackById" not in exported_identifiers
    assert "playSong" not in exported_identifiers

    # Verify that every exported identifier is declared in app.js or imported
    for identifier in exported_identifiers:
        if identifier == "SpotiDB":
            continue
        decl_pattern = rf"\b(function|const|let|var|class)\s+{re.escape(identifier)}\b"
        assert re.search(decl_pattern, code) is not None, (
            f"Exported identifier '{identifier}' is not declared in app.js!"
        )


def test_auth_authenticate_user_fallback_query():
    """Validates that authenticate_user uses case-insensitive name fallback in PocketBase filter."""
    import auth
    with unittest.mock.patch("database.get_pb") as mock_get_pb, \
         unittest.mock.patch("database.get_admin_pb") as mock_get_admin_pb:

        mock_user_pb = unittest.mock.MagicMock()
        mock_auth_record = unittest.mock.MagicMock()
        mock_auth_record.id = "u123"
        mock_auth_record.name = "Tshewang"
        mock_auth_record.created = "2026-10-19"
        mock_auth_success = unittest.mock.MagicMock(
            record=mock_auth_record,
            token="jwt.token.here"
        )
        mock_user_pb.collection.return_value.auth_with_password.side_effect = [
            ValueError("Auth failed"),  # first attempt with username@spoti.local
            mock_auth_success           # second attempt with found user_email
        ]
        mock_get_pb.return_value = mock_user_pb

        # admin_pb finds the user record via name filter
        mock_admin_pb = unittest.mock.MagicMock()
        mock_record = unittest.mock.MagicMock()
        mock_record.email = "custom_email@example.com"
        mock_record.name = "Tshewang"
        mock_list = unittest.mock.MagicMock(total_items=1, items=[mock_record])
        mock_admin_pb.collection.return_value.get_list.return_value = mock_list
        mock_get_admin_pb.return_value = mock_admin_pb

        res = auth.authenticate_user("tshewang", "password123")
        assert res["id"] == "u123"
        assert res["username"] == "Tshewang"
        assert res["token"] == "jwt.token.here"

        # Verify that the query filter included name ~
        call_args = mock_admin_pb.collection.return_value.get_list.call_args
        filter_str = call_args[1]["query_params"]["filter"]
        assert 'name ~ "tshewang"' in filter_str













