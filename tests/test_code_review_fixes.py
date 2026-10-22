import os
import sys
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

