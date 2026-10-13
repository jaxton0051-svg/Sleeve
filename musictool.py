"""
yt_tools.py
-----------
Search  : YouTube via SearXNG
Download: YouTube via yt-dlp
  • Single   -> downloads/
  • Playlist -> downloads/<playlist name>/

Requirements: pip install yt-dlp requests Pillow curl_cffi
              ffmpeg on PATH (for mp3 conversion)
"""

import re
import os
import sys
import shutil
import requests
import yt_dlp
from pathlib import Path
from app_paths import DATA_DIR, data_file
from typing import Literal

from dl_log import already_downloaded, log_download, get_entry

# Windows console can't encode fancy unicode by default — don't let it crash us.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Add project-local ffmpeg to PATH and locate it for yt-dlp
PROJECT_DIR = Path(__file__).parent.resolve()
ROOT_DIR = Path.cwd().resolve()
_ffmpeg_dirs = [str(PROJECT_DIR), str(ROOT_DIR)]
_ffmpeg_exe = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
for d in _ffmpeg_dirs:
    if d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
for d in _ffmpeg_dirs:
    p = Path(d) / "ffmpeg.exe"
    if p.exists() and not _ffmpeg_exe:
        _ffmpeg_exe = str(p)
        break

# musicmeta is BETA — imported lazily; missing deps won't break anything
_musicmeta_enabled = True   # default on; toggled via main.py settings

# ── Config ────────────────────────────────────────────────────────────────────

SEARXNG_URL = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
OUTPUT_DIR  = DATA_DIR / "downloads"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_COOKIES_FILE = data_file("cookies.txt")


class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


_SILENT_LOGGER = _SilentLogger()


# Use cookies.txt directly if it exists — no browser probing (avoids DPAPI noise).
_HAVE_COOKIES = _COOKIES_FILE.exists() and _COOKIES_FILE.stat().st_size > 0


# ── Impersonation ─────────────────────────────────────────────────────────────

def _current_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"

def _current_os_version() -> str:
    if sys.platform == "win32":
        return "10"
    if sys.platform == "darwin":
        return "14"
    return "unknown"

_IMPERSONATE_CANDIDATES = [
    ("firefox", "135", "windows", "10"),
    ("firefox", "133", "windows", "10"),
    ("edge", "101", "windows", "10"),
    ("edge", "99",  "windows", "10"),
    ("firefox", "135", "macos", "14"),
    ("firefox", "133", "macos", "14"),
    ("safari", "18.4",  "macos", "15"),
    ("safari", "18.0",  "macos", "15"),
    ("safari", "17.0",  "macos", "14"),
]


def _get_available_targets() -> set[tuple]:
    try:
        from yt_dlp.networking.impersonate import ImpersonateRequestHandler
    except ImportError:
        return set()
    available = set()
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            handler = ydl._request_director.handlers.get("CurlCFFI")
            if handler and hasattr(handler, "supported_targets"):
                for t in handler.supported_targets:
                    available.add((
                        (getattr(t, "client", "") or "").lower(),
                        (getattr(t, "version", "") or "").lower(),
                        (getattr(t, "os", "") or "").lower(),
                        (getattr(t, "os_version", "") or "").lower(),
                    ))
    except Exception:
        pass
    return available


def _pick_impersonate_target():
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except ImportError:
        return None

    available = _get_available_targets()
    if not available:
        return None

    cur_os = _current_os()

    # Filter candidates to current OS only
    candidates = [c for c in _IMPERSONATE_CANDIDATES if c[2] == cur_os]

    for client, version, os_, os_ver in candidates:
        if (client, version, os_, os_ver) in available:
            return ImpersonateTarget(client=client, version=version, os=os_, os_version=os_ver)

    # Fallback: any non-Chrome target on the current OS
    for client, version, os_, os_ver in sorted(available):
        if os_ == cur_os and client in ("edge", "firefox", "safari"):
            return ImpersonateTarget(client=client, version=version, os=os_, os_version=os_ver)

    return None


_IMPERSONATE = _pick_impersonate_target()


def _opts_with_auth(base: dict) -> dict:
    """Add cookies file and impersonation to a yt-dlp opts dict."""
    if _HAVE_COOKIES:
        base["cookiefile"] = str(_COOKIES_FILE)
    if _IMPERSONATE:
        base["impersonate"] = _IMPERSONATE
    return base


# ── Progress hook ─────────────────────────────────────────────────────────────

def _progress_hook(d: dict) -> None:
    name   = Path(d.get("filename", "?")).name
    status = d.get("status")
    if status == "downloading":
        pct   = d.get("_percent_str",  "? %").strip()
        speed = d.get("_speed_str",    "?/s").strip()
        eta   = d.get("_eta_str",       "?s").strip()
        print(f"\r  {name[:52]:<52} {pct:>6}  {speed:>10}  eta {eta}",
              end="", flush=True)
    elif status == "finished":
        print(f"\n  {name}")
    elif status == "error":
        print(f"\n  Error: {name}")


# ── Search ────────────────────────────────────────────────────────────────────

def search_youtube(query: str, max_results: int = 8) -> list[dict]:
    """
    Search YouTube via SearXNG.
    Returns list of { title, url, thumbnail, duration, channel, source }.
    """
    if not SEARXNG_URL:
        return []
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "categories": "videos",
                    "engines": "youtube", "format": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  Search error: {e}")
        return []

    results = []
    for item in data.get("results", [])[:max_results]:
        url = item.get("url") or ""
        if not re.search(r"youtube\.com/(watch|playlist)|youtu\.be/", url):
            continue
        results.append({
            "title":     item.get("title") or "Unknown",
            "url":       url,
            "thumbnail": item.get("thumbnail") or "",
            "duration":  item.get("length") or "",
            "channel":   item.get("channel") or "",
            "source":    "youtube",
        })
    return results


def get_yt_info(url: str) -> dict:
    """
    Return metadata dict for a YouTube URL without downloading.
    Keys: title, thumbnail, duration, channel, is_playlist, playlist_title.
    """
    base_opts = {
        "quiet":                True,
        "no_warnings":          True,
        "logger":               _SILENT_LOGGER,
        "extract_flat":         "in_playlist",
        "socket_timeout":       15,
    }

    def _extract(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        if _HAVE_COOKIES or _IMPERSONATE:
            try:
                info = _extract(_opts_with_auth(dict(base_opts)))
            except Exception:
                info = _extract(base_opts)
        else:
            info = _extract(base_opts)
        is_pl = info.get("_type") == "playlist"
        return {
            "title":          info.get("title", url),
            "thumbnail":      info.get("thumbnail", ""),
            "duration":       info.get("duration_string", ""),
            "channel":        info.get("channel", info.get("uploader", "")),
            "is_playlist":    is_pl,
            "playlist_title": info.get("title", "") if is_pl else "",
        }
    except Exception:
        return {"title": url, "thumbnail": "", "duration": "",
                "channel": "", "is_playlist": False, "playlist_title": ""}


def _safe_folder_name(name: str) -> str:
    """Strip characters that are invalid in folder names."""
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip(". ")[:80] or "playlist"


# ── Download ──────────────────────────────────────────────────────────────────

def download_youtube(
    url:           str,
    media:         Literal["mp3", "mp4"] = "mp4",
    mode:          Literal["single", "playlist"] = "single",
    quality:       str = "best",
    output_dir:    Path | str = OUTPUT_DIR,
    audio_bitrate: str = "192",
    force:         bool = False,
    musicmeta:     bool | None = None,
    artist_hint:   str = "",
) -> None:
    """
    Download a YouTube video or playlist as MP3 or MP4.

    Output paths:
      single   -> <output_dir>/<title>.<ext>
      playlist -> <output_dir>/<playlist name>/<index> - <title>.<ext>

    Skips if already logged unless force=True.
    If media="mp3" and musicmeta is enabled, enriches tags via MusicBrainz.
    """
    output_dir = Path(output_dir)

    # ── Log check ──────────────────────────────────────────────────────────────
    if not force and already_downloaded(url):
        entry = get_entry(url)
        print(f"  Already downloaded on {entry['downloaded']}  ->  {entry['title']}")
        return

    # ── Resolve output folder ──────────────────────────────────────────────────
    if mode == "playlist":
        peek_opts = {
            "quiet":              True,
            "no_warnings":        True,
            "logger":             _SILENT_LOGGER,
            "extract_flat":       True,
            "socket_timeout":     15,
        }
        try:
            if _HAVE_COOKIES or _IMPERSONATE:
                try:
                    with yt_dlp.YoutubeDL(_opts_with_auth(peek_opts)) as ydl:
                        info = ydl.extract_info(url, download=False)
                except Exception:
                    with yt_dlp.YoutubeDL(peek_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
            else:
                with yt_dlp.YoutubeDL(peek_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            pl_name = _safe_folder_name(info.get("title", "playlist"))
        except Exception:
            pl_name = "playlist"
        dest_dir = output_dir / pl_name
        template = str(dest_dir / "%(playlist_index)s - %(title)s.%(ext)s")
    else:
        dest_dir = output_dir
        template = str(dest_dir / "%(title)s.%(ext)s")

    dest_dir.mkdir(parents=True, exist_ok=True)

    # ── yt-dlp options ─────────────────────────────────────────────────────────
    quality_map = {
        "best": "bestvideo+bestaudio/best",
        "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "720":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "480":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    }

    base = {
        "outtmpl":              template,
        "noplaylist":           mode == "single",
        "ignoreerrors":         True,
        "quiet":                True,
        "no_warnings":          True,
        "logger":               _SILENT_LOGGER,
        "progress_hooks":       [_progress_hook],
        "socket_timeout":       30,
        "retries":              3,
        "fragment_retries":     3,
        "ffmpeg_location":      _ffmpeg_exe or str(PROJECT_DIR),
    }
    # Same JS runtime hint as yt_tools — without it YouTube returns 403.
    try:
        from yt_tools import JS_RUNTIMES
        if JS_RUNTIMES:
            base["js_runtimes"] = {runtime: {} for runtime in JS_RUNTIMES}
    except Exception:
        pass
    # Same cookies.txt handling as yt_tools.
    try:
        from yt_tools import COOKIES_FILE
        if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
            base["cookiefile"] = str(COOKIES_FILE)
    except Exception:
        pass

    if media == "mp3":
        opts = {**base,
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio",
                 "preferredcodec": "mp3",
                 "preferredquality": audio_bitrate},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True,
        }
    else:
        opts = {**base,
            "format": quality_map.get(quality, quality_map["best"]),
            "merge_output_format": "mp4",
            "postprocessors": [{"key": "FFmpegMetadata"}],
        }

    # ── Run ────────────────────────────────────────────────────────────────────
    yt_info = get_yt_info(url)
    title   = yt_info["title"]
    channel = yt_info.get("channel", "")

    def _existing_files() -> set[Path]:
        return {p for p in dest_dir.rglob("*") if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".tmp"}}

    before = _existing_files()

    _done = {"finished": False}

    def _completion_hook(d):
        if d.get("status") == "finished":
            _done["finished"] = True
        _progress_hook(d)

    run_opts = dict(opts)
    run_opts["progress_hooks"] = [_completion_hook]

    def _attempt(opts_dict: dict) -> None:
        with yt_dlp.YoutubeDL(opts_dict) as ydl:
            ydl.download([url])

    if _HAVE_COOKIES or _IMPERSONATE:
        try:
            _attempt(_opts_with_auth(run_opts))
        except Exception:
            _done["finished"] = False
            _attempt(run_opts)
        else:
            if not _done["finished"]:
                _done["finished"] = False
                _attempt(run_opts)
    else:
        _attempt(run_opts)

    new_files = [p for p in (_existing_files() - before) if p.suffix.lower() in {".mp4", ".mp3", ".m4a", ".webm", ".mkv"}]
    if not new_files:
        raise RuntimeError(f"Download produced no output file for: {url}")

    print(f"\n  Saved to: {dest_dir.resolve()}")
    log_download(key=url, title=title, source="youtube",
                 media=media, path=str(dest_dir))

    # ── MusicBrainz metadata enrichment (MP3 only) ─────────────────────────────
    use_meta = _musicmeta_enabled if musicmeta is None else musicmeta
    if media == "mp3" and use_meta:
        try:
            from musicmeta import enrich_mp3, enrich_folder
            print()
            if mode == "playlist":
                enrich_folder(dest_dir, artist_hint=artist_hint)
            else:
                mp3s = sorted(dest_dir.glob("*.mp3"), key=lambda f: f.stat().st_mtime,
                              reverse=True)
                if mp3s:
                    enrich_mp3(mp3s[0], title=title, artist=artist_hint or channel)
        except ImportError:
            print("  [musicmeta] mutagen not installed — pip install mutagen")
