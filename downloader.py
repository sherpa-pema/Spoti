"""
Legacy downloader module.
All audio streaming and transient transcoding is now handled by stream_downloader.py.
"""
from stream_downloader import find_ffmpeg

__all__ = ["find_ffmpeg"]
