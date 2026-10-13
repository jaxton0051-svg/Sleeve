"""
yt_tools.py
-----------
Search  : YouTube via SearXNG
Download: YouTube via yt-dlp
  • Single   → downloads/
  • Playlist → downloads/<playlist name>/

Requirements: pip install yt-dlp requests Pillow curl_cffi
              ffmpeg on PATH (for mp3 conversion)
"""

import re
import os
import sys
import shutil
import shutil
import requests
import yt_dlp
from pathlib import Path
from app_paths import data_file
from typing import Literal

from dl_log import already_downloaded, log_download, get_entry

# Windows console can't encode fancy unicode by default — don't let it crash us.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# YouTube now requires a JavaScript runtime for extraction (signature
# deciphering + n-challenge). Without one, downloads fail with HTTP 403
# partway through. Pick whichever of deno/node/bun is on PATH; yt-dlp
# falls back to this list when its built-in deno default is missing.
def _detect_js_runtimes() -> list[str]:
    """Return ['deno','node','bun'] ordered by preference, dropping missing ones."""
    found: list[str] = []
    for runtime in ("deno", "node", "bun"):
        path = shutil.which(runtime)
        if path:
            found.append(runtime)
    return found


JS_RUNTIMES = _detect_js_runtimes()
if JS_RUNTIMES:
    print(f"  [yt_tools] JS runtime(s) available for YouTube: {', '.join(JS_RUNTIMES)}")
else:
    print("  [yt_tools] WARNING: no JS runtime (deno/node/bun) found — YouTube "
          "downloads will hit HTTP 403. Install deno from https://deno.land/.")


# Browser-cookie source: when set in app_settings as `yt_cookies_source`,
# yt-dlp will pull cookies directly from that browser's cookie database
# (no manual cookies.txt needed). Default is "none" — users opt in via
# Settings. yt-dlp accepts: chrome, chromium, firefox, edge, brave, opera,
# vivaldi, safari, whale, brave-browser.
_BROWSER_COOKIE_SOURCES = (
    "none", "chrome", "chromium", "firefox", "edge", "brave",
    "opera", "vivaldi", "safari", "whale", "brave-browser",
)


def _yt_cookies_source() -> str:
    """Read the user's 'yt_cookies_source' preference. Returns 'none' on
    any failure (missing jfile, unset setting, invalid value)."""
    try:
        from jfile import settings_get
        val = settings_get("yt_cookies_source", "none")
        val = (val or "none").strip().lower()
        if val not in _BROWSER_COOKIE_SOURCES:
            return "none"
        return val
    except Exception:
        return "none"


def refresh_cookies_file(source: str | None = None) -> dict:
    """Pull cookies from the configured browser and write them to
    cookies.txt in Netscape format. yt-dlp then reads the file via
    `cookiefile`, which is more transparent than `cookiesfrombrowser`
    (file is inspectable, editable, works across yt-dlp versions, and
    survives the browser being closed).

    Returns {ok, count, path, source, error}.
    """
    src = (source or _yt_cookies_source()).strip().lower()
    if not src or src == "none":
        return {"ok": False, "count": 0, "path": str(COOKIES_FILE),
                "source": src, "error": "yt_cookies_source is 'none' — pick a browser in Settings."}
    if src not in _BROWSER_COOKIE_SOURCES:
        return {"ok": False, "count": 0, "path": str(COOKIES_FILE),
                "source": src, "error": f"Unknown browser source: {src!r}"}

    try:
        from yt_dlp import cookies as yt_cookies
    except ImportError as e:
        return {"ok": False, "count": 0, "path": str(COOKIES_FILE),
                "source": src, "error": f"yt-dlp cookies module unavailable: {e}"}

    try:
        jar = yt_cookies.extract_cookies_from_browser(src)
    except Exception as e:
        return {"ok": False, "count": 0, "path": str(COOKIES_FILE),
                "source": src,
                "error": f"{type(e).__name__}: {str(e)[:300]}  "
                         f"(Tip: fully close {src} before refreshing — Windows "
                         f"locks the cookie DB while the browser is running.)"}

    count = len(list(jar))
    try:
        # MozillaCookieJar.save writes the standard Netscape HTTP Cookie File
        # format that yt-dlp / curl / requests all understand.
        jar.save(str(COOKIES_FILE), ignore_discard=True, ignore_expires=True)
    except Exception as e:
        return {"ok": False, "count": count, "path": str(COOKIES_FILE),
                "source": src, "error": f"write failed: {type(e).__name__}: {e}"}

    return {"ok": True, "count": count, "path": str(COOKIES_FILE),
            "source": src, "error": None}


def auto_refresh_cookies() -> None:
    """If the user has a browser source configured, refresh cookies.txt
    on import. Silently no-op if 'none'. Errors are logged, not raised —
    cookies.txt might already exist and be usable."""
    src = _yt_cookies_source()
    if not src or src == "none":
        return
    result = refresh_cookies_file(src)
    if result["ok"]:
        print(f"  [yt_tools] Refreshed {result['count']} cookies from {src} → {result['path']}")
    else:
        print(f"  [yt_tools] Cookie refresh failed: {result['error']}")


def _detect_installed_browsers() -> list[str]:
    """Quick check for which supported browsers are likely installed.
    Used by /api/settings/preferences so the UI can suggest only valid
    options. Best-effort — yt-dlp itself does a more thorough check."""
    if sys.platform != "win32":
        # yt-dlp handles other platforms; we only sniff Windows here.
        return []
    local = os.environ.get("LOCALAPPDATA", "")
    prog = os.environ.get("PROGRAMFILES", "")
    prog86 = os.environ.get("PROGRAMFILES(X86)", "")
    candidates: list[tuple[str, list[str]]] = [
        ("chrome", [
            rf"{local}\Google\Chrome\Application\chrome.exe",
            rf"{prog}\Google\Chrome\Application\chrome.exe",
        ]),
        ("edge", [
            rf"{prog86}\Microsoft\Edge\Application\msedge.exe",
            rf"{prog}\Microsoft\Edge\Application\msedge.exe",
        ]),
        ("brave", [
            rf"{local}\BraveSoftware\Brave-Browser\Application\brave.exe",
        ]),
        ("chromium", [
            rf"{prog}\Chromium\Application\chrome.exe",
        ]),
        ("opera", [
            rf"{local}\Programs\Opera\opera.exe",
        ]),
        ("vivaldi", [
            rf"{local}\Vivaldi\Application\vivaldi.exe",
        ]),
    ]
    found: list[str] = []
    for name, paths in candidates:
        for p in paths:
            if p and Path(p).exists():
                found.append(name)
                break
    return found

# Prefer system FFmpeg; an optional project-local copy remains supported.
PROJECT_DIR = Path(__file__).parent.resolve()
_ffmpeg_path = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
FFMPEG_DIR = str(Path(_ffmpeg_path).parent) if _ffmpeg_path else str(PROJECT_DIR)
if FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

# Browser-extracted cookies are written to the per-user app-data directory.
COOKIES_FILE = data_file("cookies.txt")

# musicmeta is BETA — imported lazily; missing deps won't break anything
_musicmeta_enabled = True   # default on; toggled via main.py settings

# ── Config ────────────────────────────────────────────────────────────────────

SEARXNG_URL = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
OUTPUT_DIR  = data_file("downloads", migrate_legacy=False)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Cookie auth helps yt-dlp get past YouTube bot checks.
# A silent logger keeps yt-dlp's noisy ERROR lines off the console.
class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


_SILENT_LOGGER = _SilentLogger()

COOKIE_FILE_CANDIDATES = [
    Path(os.getenv("YT_DLP_COOKIE_FILE", "")).expanduser() if os.getenv("YT_DLP_COOKIE_FILE") else None,
    COOKIES_FILE,
]


def _pick_cookie_auth():
    """Silently use a cookie file if one exists."""
    for cookie_file in COOKIE_FILE_CANDIDATES:
        if cookie_file and cookie_file.exists():
            return {"cookiefile": str(cookie_file)}
    return None


_COOKIE_AUTH = _pick_cookie_auth()


# ── Impersonation ─────────────────────────────────────────────────────────────

# Preference list: (client, version, os, os_version)
# Based on actual curl_cffi 0.14.0 supported targets.
_IMPERSONATE_CANDIDATES = [
    # Edge on Windows (only 101 and 99 available)
    ("edge", "101", "windows", "10"),
    ("edge", "99",  "windows", "10"),
    # Firefox on macOS (135 and 133 available)
    ("firefox", "135", "macos", "14"),
    ("firefox", "133", "macos", "14"),
    # Safari on macOS (good non-Chrome fallback)
    ("safari", "26.0",  "macos", "26"),
    ("safari", "18.4",  "macos", "15"),
    ("safari", "18.0",  "macos", "15"),
    ("safari", "17.0",  "macos", "14"),
    ("safari", "15.5",  "macos", "14"),
    ("safari", "15.3",  "macos", "14"),
]


def _get_available_targets() -> set[tuple]:
    """
    Ask yt-dlp which impersonate targets are actually available.
    Returns a set of (client, version, os, os_version) tuples, all lowercase.
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateRequestHandler  # noqa: F401
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
    """
    Returns an ImpersonateTarget object or None.
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except ImportError:
        return None

    available = _get_available_targets()
    if not available:
        return None

    for client, version, os_, os_ver in _IMPERSONATE_CANDIDATES:
        if (client, version, os_, os_ver) in available:
            return ImpersonateTarget(client=client, version=version, os=os_, os_version=os_ver)

    # Last resort: any non-Chrome target that actually exists
    for client, version, os_, os_ver in sorted(available):
        if client in ("edge", "firefox", "safari"):
            return ImpersonateTarget(client=client, version=version, os=os_, os_version=os_ver)

    return None


_IMPERSONATE = _pick_impersonate_target()


# ── Progress hook ─────────────────────────────────────────────────────────────

def _make_progress_hook(callback=None):
    def _hook(d: dict) -> None:
        if callback is not None:
            callback(d)
        name   = Path(d.get("filename", "?")).name
        status = d.get("status")
        if status == "downloading":
            pct   = d.get("_percent_str",  "? %").strip()
            speed = d.get("_speed_str",    "?/s").strip()
            eta   = d.get("_eta_str",       "?s").strip()
            print(f"\r  ↓ {name[:52]:<52} {pct:>6}  {speed:>10}  eta {eta}",
                  end="", flush=True)
        elif status == "finished":
            print(f"\n  ✓ {name}")
        elif status == "error":
            print(f"\n  ✗ Error: {name}")
    return _hook


def _progress_hook(d: dict) -> None:
    _make_progress_hook()(d)


# ── Search ────────────────────────────────────────────────────────────────────

def search_youtube(query: str, max_results: int = 8) -> list[dict]:
    """
    Search YouTube via SearXNG, falling back to yt-dlp's built-in search.
    Returns list of { title, url, thumbnail, duration, channel, source }.
    """
    if not SEARXNG_URL:
        return _search_ytdlp(query, max_results)
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "categories": "videos",
                    "engines": "youtube", "format": "json"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ✗ SearXNG search failed ({e}); trying yt-dlp search")
        return _search_ytdlp(query, max_results)

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
    if not results:
        print("  ✗ SearXNG returned nothing; trying yt-dlp search")
        return _search_ytdlp(query, max_results)
    return results


def _search_ytdlp(query: str, max_results: int = 8) -> list[dict]:
    """Search YouTube via yt-dlp's ytsearch extraction."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": _SILENT_LOGGER,
        "extract_flat": True,
        "skip_download": True,
    }
    try:
        info = extract_info_safe(f"ytsearch{max_results}:{query}", opts)
    except Exception as e:
        print(f"  ✗ yt-dlp search failed: {e}")
        return []

    results = []
    for entry in info.get("entries", [])[:max_results]:
        if not entry or not entry.get("id"):
            continue
        duration = ""
        if entry.get("duration"):
            minutes, seconds = divmod(int(entry["duration"]), 60)
            duration = f"{minutes}:{seconds:02d}"
        results.append({
            "title":     entry.get("title") or "Unknown",
            "url":       f"https://www.youtube.com/watch?v={entry['id']}",
            "thumbnail": entry.get("thumbnail") or "",
            "duration":  duration,
            "channel":   entry.get("channel") or entry.get("uploader") or "",
            "source":    "youtube",
        })
    return results


def _with_auth(opts: dict) -> dict:
    """Return a copy of opts with cookie/impersonate auth merged in."""
    merged = dict(opts)
    if _COOKIE_AUTH:
        merged.update(_COOKIE_AUTH)
    if _IMPERSONATE:
        merged["impersonate"] = _IMPERSONATE
    return merged


def extract_info_safe(url: str, base_opts: dict, download: bool = False):
    """
    Try yt-dlp with cookie/impersonate auth; if that fails (e.g. stale
    cookies cause 'Requested format is not available'), retry without auth.
    """
    if _COOKIE_AUTH or _IMPERSONATE:
        try:
            with yt_dlp.YoutubeDL(_with_auth(base_opts)) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception:
            pass
    with yt_dlp.YoutubeDL(base_opts) as ydl:
        return ydl.extract_info(url, download=download)


def get_yt_info(url: str) -> dict:
    """
    Return metadata dict for a YouTube URL without downloading.
    Keys: title, thumbnail, duration, channel, is_playlist, playlist_title.
    """
    opts = {
        "quiet":                True,
        "no_warnings":          True,
        "logger":               _SILENT_LOGGER,
        "extract_flat":         "in_playlist",
        "socket_timeout":       15,
    }

    try:
        info = extract_info_safe(url, opts)
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
    progress_callback=None,
) -> None:
    """
    Download a YouTube video or playlist as MP3 or MP4.

    Output paths:
      single   → <output_dir>/<title>.<ext>
      playlist → <output_dir>/<playlist name>/<index> - <title>.<ext>

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
            info = extract_info_safe(url, peek_opts)
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
        "progress_hooks":       [_make_progress_hook(progress_callback)],
        "socket_timeout":       30,
        "retries":              3,
        "fragment_retries":     3,
        "ffmpeg_location":      FFMPEG_DIR,
    }
    if JS_RUNTIMES:
        # Tell yt-dlp which JS runtimes it can use. Without this YouTube
        # extraction fails with HTTP 403 because signature deciphering
        # can't run.
        # yt-dlp expects a dict of {runtime_name: {config}}, not a list.
        base["js_runtimes"] = {runtime: {} for runtime in JS_RUNTIMES}

    # Browser cookies: yt-dlp reads cookies.txt (auto-refreshed on server
    # startup from the configured browser). This is more transparent than
    # `cookiesfrombrowser` — the file is inspectable, editable, and yt-dlp
    # only sees the YouTube-relevant subset that was last written.
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
        base["cookiefile"] = str(COOKIES_FILE)
    if mode == "playlist":
        # Keep the file naming untouched and write playlist structure into metadata.
        base["parse_metadata"] = [
            "playlist_title:series",
            "playlist_title:album",
            "playlist_index:episode_number",
            ":(?P<season_number>1)",
            ":(?P<season>Season 1)",
        ]

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
        return {p for p in dest_dir.rglob("*") if p.is_file() and not p.suffix.lower() in {".part", ".ytdl", ".tmp"}}

    def _partial_files() -> list[Path]:
        return [p for p in dest_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".part", ".ytdl", ".tmp"}]

    before = _existing_files()

    _done = {"finished": False}

    def _completion_hook(d):
        if d.get("status") == "finished":
            _done["finished"] = True
        if progress_callback is not None:
            progress_callback(d)

    # Capture yt-dlp's own log output so we can include it in error messages
    # when no output file is produced.
    class _CapturingLogger:
        def __init__(self) -> None:
            self.messages: list[tuple[str, str]] = []
        def debug(self, msg: str) -> None: self.messages.append(("debug", msg))
        def info(self, msg: str) -> None: pass  # info is chatty; only keep warnings/errors
        def warning(self, msg: str) -> None: self.messages.append(("warning", msg))
        def error(self, msg: str) -> None: self.messages.append(("error", msg))

    run_opts = dict(opts)
    run_opts["progress_hooks"] = [_make_progress_hook(_completion_hook)]

    def _attempt(opts_dict: dict, log_capture: _CapturingLogger) -> None:
        opts_with_logger = dict(opts_dict)
        opts_with_logger["logger"] = log_capture
        with yt_dlp.YoutubeDL(opts_with_logger) as ydl:
            ydl.download([url])

    captured_auth: list[tuple[str, str]] = []
    captured_noauth: list[tuple[str, str]] = []
    attempt_history: list[str] = []

    if _COOKIE_AUTH or _IMPERSONATE:
        cap1 = _CapturingLogger()
        try:
            _attempt(_with_auth(run_opts), cap1)
            captured_auth = cap1.messages
            attempt_history.append(f"with auth: finished={_done['finished']}, exception=None")
        except Exception as e:
            captured_auth = cap1.messages
            attempt_history.append(f"with auth: exception ({type(e).__name__}: {str(e)[:120]})")
            _done["finished"] = False
            cap2 = _CapturingLogger()
            try:
                _attempt(run_opts, cap2)
                captured_noauth = cap2.messages
                attempt_history.append(f"without auth: finished={_done['finished']}, exception=None")
            except Exception as e2:
                captured_noauth = cap2.messages
                attempt_history.append(f"without auth: exception ({type(e2).__name__}: {str(e2)[:120]})")
        else:
            if not _done["finished"]:
                _done["finished"] = False
                cap2 = _CapturingLogger()
                try:
                    _attempt(run_opts, cap2)
                    captured_noauth = cap2.messages
                    attempt_history.append(f"retry no auth: finished={_done['finished']}, exception=None")
                except Exception as e2:
                    captured_noauth = cap2.messages
                    attempt_history.append(f"retry no auth: exception ({type(e2).__name__}: {str(e2)[:120]})")
    else:
        cap = _CapturingLogger()
        try:
            _attempt(run_opts, cap)
            captured_noauth = cap.messages
            attempt_history.append(f"no auth: finished={_done['finished']}, exception=None")
        except Exception as e:
            captured_noauth = cap.messages
            attempt_history.append(f"no auth: exception ({type(e).__name__}: {str(e)[:120]})")

    new_files = [p for p in (_existing_files() - before) if p.suffix.lower() in {".mp4", ".mp3", ".m4a", ".webm", ".mkv"}]
    if not new_files:
        # Build a useful diagnostic — the bare "no output file" message is
        # useless for debugging. Surface *why* we got nothing.
        parts: list[str] = []
        if not _done["finished"]:
            parts.append("yt-dlp never reached the 'finished' state — the download "
                         "was interrupted before completion.")
        partial = _partial_files()
        if partial:
            names = ", ".join(p.name for p in partial[:5])
            sizes = ", ".join(f"{p.stat().st_size // 1024}KB" for p in partial[:5])
            parts.append(f"Partial downloads left behind: {names} ({sizes}). "
                         "These indicate the transfer started but never finalized. "
                         "Delete them and retry, or pass force=True.")
        all_captured = list(captured_auth) + list(captured_noauth)
        # Keep only warnings + errors from yt-dlp — info/debug are too noisy
        relevant = [(lvl, msg) for lvl, msg in all_captured if lvl in ("warning", "error")]
        if relevant:
            tail = relevant[-4:]
            formatted = "\n".join(f"  [{lvl}] {msg.strip()}" for lvl, msg in tail)
            parts.append(f"yt-dlp output (last {len(tail)} warning/error lines):\n{formatted}")
        if attempt_history:
            parts.append("Attempt history: " + " | ".join(attempt_history))
        if not parts:
            parts.append("No yt-dlp output captured and no partial files found. "
                         "Check disk space, permissions on ./downloads, and that "
                         "ffmpeg.exe is present at the project root.")
        msg = f"Download produced no output file for: {url}\n\n" + "\n\n".join(parts)
        raise RuntimeError(msg)

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
