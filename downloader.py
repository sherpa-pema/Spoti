import os
import sys
import shutil
import threading
from typing import Callable, Optional, Dict, Any

# Ensure local cloned repository is accessible
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

try:
    import yt_dlp
except ImportError:
    # Fallback to local yt_dlp directory
    sys.path.insert(0, os.path.join(current_dir, "yt_dlp"))
    import yt_dlp


class DownloadCancelled(Exception):
    """Raised when the user cancels an ongoing download."""
    pass


def find_ffmpeg() -> Optional[str]:
    """Finds ffmpeg executable in PATH, local directory, or winget package directories."""
    # 1. Check system PATH
    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path:
        return os.path.dirname(ffmpeg_in_path)

    # 2. Check current directory or ./bin/ or ./ffmpeg/
    for sub in [".", "bin", "ffmpeg"]:
        candidate = os.path.join(current_dir, sub, "ffmpeg.exe")
        if os.path.isfile(candidate):
            return os.path.dirname(os.path.abspath(candidate))

    # 3. Check Windows WinGet local packages directory
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_pkgs = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_pkgs):
            for root, dirs, files in os.walk(winget_pkgs):
                if "ffmpeg.exe" in files:
                    return root

    # 4. Check Program Files
    for pf in ["ProgramFiles", "ProgramFiles(x86)"]:
        base = os.environ.get(pf, "")
        if base and os.path.isdir(base):
            for sub in ["ffmpeg", "yt-dlp"]:
                cand_dir = os.path.join(base, sub, "bin")
                if os.path.isfile(os.path.join(cand_dir, "ffmpeg.exe")):
                    return cand_dir

    return None


def format_duration(seconds: Optional[int]) -> str:
    """Formats duration in seconds into a friendly HH:MM:SS or MM:SS string."""
    if not seconds or seconds < 0:
        return "Unknown length"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_bytes(byte_count: Optional[float]) -> str:
    """Formats byte counts into human-readable strings (e.g. 15.4 MB)."""
    if not byte_count or byte_count <= 0:
        return "Unknown size"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if byte_count < 1024.0:
            return f"{byte_count:.1f} {unit}"
        byte_count /= 1024.0
    return f"{byte_count:.2f} TB"


def get_default_download_dir() -> str:
    """Returns the user's standard Downloads folder."""
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        downloads = os.path.join(user_profile, "Downloads")
        if os.path.isdir(downloads):
            return downloads
    return os.path.expanduser("~/Downloads")


def extract_media_info(url: str) -> Dict[str, Any]:
    """Fetches video/playlist metadata without downloading media."""
    ffmpeg_dir = find_ffmpeg()
    ydl_opts: Dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
    }
    if ffmpeg_dir:
        ydl_opts['ffmpeg_location'] = ffmpeg_dir

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("No media information could be extracted from this URL.")

        # Check if playlist
        if '_type' in info and info['_type'] == 'playlist':
            entries = list(info.get('entries', []))
            first_entry = entries[0] if entries else {}
            return {
                'is_playlist': True,
                'title': info.get('title', 'Unknown Playlist'),
                'uploader': info.get('uploader') or info.get('channel') or 'Various Artists',
                'duration_str': f"{len(entries)} items",
                'thumbnail': info.get('thumbnails', [{}])[-1].get('url') if info.get('thumbnails') else first_entry.get('thumbnail'),
                'count': len(entries),
                'webpage_url': info.get('webpage_url', url),
            }
        
        return {
            'is_playlist': False,
            'title': info.get('title', 'Unknown Title'),
            'uploader': info.get('uploader') or info.get('channel') or 'Unknown Uploader',
            'duration_str': format_duration(info.get('duration')),
            'thumbnail': info.get('thumbnail'),
            'webpage_url': info.get('webpage_url', url),
        }


class DownloaderThread(threading.Thread):
    def __init__(
        self,
        url: str,
        output_dir: str,
        format_choice: str,
        progress_callback: Callable[[Dict[str, Any]], None],
        done_callback: Callable[[bool, str], None],
    ):
        super().__init__(daemon=True)
        self.url = url
        self.output_dir = output_dir
        self.format_choice = format_choice
        self.progress_callback = progress_callback
        self.done_callback = done_callback
        self.is_cancelled = False

    def cancel(self):
        self.is_cancelled = True

    def _hook(self, d: Dict[str, Any]):
        if self.is_cancelled:
            raise DownloadCancelled("Download was cancelled.")

        status = d.get('status')
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            
            percent = 0.0
            if total > 0:
                percent = min(1.0, max(0.0, downloaded / total))

            speed = d.get('speed')
            speed_str = f"{format_bytes(speed)}/s" if speed else "Calculating..."
            
            eta = d.get('eta')
            eta_str = format_duration(eta) if eta is not None else "--:--"

            self.progress_callback({
                'phase': 'downloading',
                'percent': percent,
                'downloaded_bytes': downloaded,
                'total_bytes': total,
                'downloaded_str': format_bytes(downloaded),
                'total_str': format_bytes(total),
                'speed_str': speed_str,
                'eta_str': eta_str,
                'filename': os.path.basename(d.get('filename', '')),
            })
        elif status == 'finished':
            self.progress_callback({
                'phase': 'processing',
                'percent': 1.0,
                'message': 'Download complete! Merging/Post-processing...',
            })

    def run(self):
        ffmpeg_dir = find_ffmpeg()
        os.makedirs(self.output_dir, exist_ok=True)

        ydl_opts: Dict[str, Any] = {
            'outtmpl': os.path.join(self.output_dir, '%(title)s [%(id)s].%(ext)s'),
            'progress_hooks': [self._hook],
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
        }

        if ffmpeg_dir:
            ydl_opts['ffmpeg_location'] = ffmpeg_dir

        # Configure format based on user choice
        if self.format_choice == "Best Video (Auto)":
            ydl_opts['format'] = 'bestvideo+bestaudio/best'
            ydl_opts['merge_output_format'] = 'mp4'
        elif self.format_choice == "4K (2160p)":
            ydl_opts['format'] = 'bestvideo[height<=2160]+bestaudio/best[height<=2160]'
            ydl_opts['merge_output_format'] = 'mp4'
        elif self.format_choice == "1440p (2K)":
            ydl_opts['format'] = 'bestvideo[height<=1440]+bestaudio/best[height<=1440]'
            ydl_opts['merge_output_format'] = 'mp4'
        elif self.format_choice == "1080p (Full HD)":
            ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
            ydl_opts['merge_output_format'] = 'mp4'
        elif self.format_choice == "720p (HD)":
            ydl_opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
            ydl_opts['merge_output_format'] = 'mp4'
        elif self.format_choice == "480p (SD)":
            ydl_opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]'
            ydl_opts['merge_output_format'] = 'mp4'
        elif self.format_choice == "Audio - MP3 (High Quality)":
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        elif self.format_choice == "Audio - M4A":
            ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
            }]
        elif self.format_choice == "Audio - WAV":
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
            }]
        elif self.format_choice == "Audio - FLAC":
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'flac',
            }]
        else:
            ydl_opts['format'] = 'bestvideo+bestaudio/best'

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.url])
            self.done_callback(True, "Download and processing completed successfully!")
        except DownloadCancelled:
            self.done_callback(False, "Download was cancelled.")
        except Exception as e:
            self.done_callback(False, f"Error: {str(e)}")
