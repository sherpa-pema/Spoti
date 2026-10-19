# Spoti 🎵

A lightweight, modern Spotify-inspired Progressive Web App (PWA) and streaming music server.

## Features
- **YouTube Music Streaming**: Instant search and dynamic playback powered by `ytmusicapi` and `yt-dlp`.
- **PocketBase Central Backend**: Multi-tenant user accounts, personal playlists, and liked tracks backed by PocketBase.
- **Client-Side Offline Storage (IndexedDB)**: Direct device-side MP3 caching for 100% offline playback with zero server disk bloat.
- **Progressive Web App (PWA)**: Fullscreen mobile experience with custom safe-area handling, media session controls, and background playback.
- **Ultra-Lightweight**: Designed to run smoothly on low-power devices (including iOS on an iPhone 7 mini-server).

## Quick Start
```bash
pip install -r requirements.txt
python server.py
```
Default URL: `http://localhost:8000`
