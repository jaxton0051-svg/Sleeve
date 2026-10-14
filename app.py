"""
FastAPI application for music discovery, downloads, Jellyfin, and repair.

The FastAPI entry point uses Jinja templates and static assets for the
responsive library UI. Thumbnails use lazy yt-dlp search and i.ytimg.com.

Run:
    pip install fastapi uvicorn jinja2 yt-dlp requests beautifulsoup4
    uvicorn app:app --host 127.0.0.1 --port 5000
    -> open http://127.0.0.1:5000
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app_paths import DATA_DIR, data_file


# ---------------------------------------------------------------- iTunes HTTP
#
# The public iTunes Search/lookup API is fronted by Akamai and is flaky
# from many residential networks: it returns 503s or hangs-until-timeout
# depending on which edge node and TLS fingerprint you land on. We:
#   * use a desktop-browser User-Agent (default python-requests gets
#     de-prioritised),
#   * mount a urllib3 Retry that auto-retries 429/500/502/503/504 with
#     exponential backoff,
#   * bump the per-call timeout to 25s (was 15s),
#   * cache successful album-track lookups to disk for 14 days so a
#     repeat "Add to cart" survives iTunes being temporarily down.
_ITUNES_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _itunes_session() -> requests.Session:
    """Shared requests.Session tuned for the iTunes Search API."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _ITUNES_UA,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=3,                                  # retry on 5xx
        backoff_factor=0.6,                        # 0.6s, 1.2s, 2.4s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_ITUNES = _itunes_session()
_ITUNES_TIMEOUT = 25  # seconds, used for every itunes.* call below


# (TheAudioDB helpers used to live here; the multi-source aggregator in
# providers.py now owns all per-database calls. See providers.AudioDBProvider.)


import automator  # AUTOMATOR tab: Jellyfin client + per-album worker
import jellyfin_auth
from cookie_cleaner import ensure_cookie_file_valid  # self-healing cookie file

# ---------------------------------------------------------------- paths

PROJECT_DIR = Path(__file__).parent.resolve()
TEMPLATES_DIR = PROJECT_DIR / "templates"
STATIC_DIR = PROJECT_DIR / "static"

COOKIE_FILE = data_file("cookie.txt")                # primary (new convention)
COOKIES_FILE_COMPAT = data_file("cookies.txt")       # compat with older scripts
DOWNLOAD_DIR = DATA_DIR / "downloads"
LEGACY_DOWNLOAD_DIR = PROJECT_DIR / "downloads"
CACHE_FILE = data_file("trending_cache.json")
THUMB_CACHE_FILE = data_file("thumbnails_cache.json")
ALBUM_CACHE_FILE = data_file("albums_cache.json")
# Per-album tracklist cache. Survives iTunes being down: once an album is
# successfully looked up, all future "Add to cart" loads are instant.
ALBUM_TRACKS_CACHE_FILE = data_file("album_tracks_cache.json")
_ALBUM_TRACKS_TTL = timedelta(days=14)

# Optional project-local FFmpeg is supported; system FFmpeg on PATH is preferred.
FFMPEG_DIR = PROJECT_DIR
FFMPEG_EXE = FFMPEG_DIR / "ffmpeg.exe"
FFPROBE_EXE = FFMPEG_DIR / "ffprobe.exe"

DOWNLOAD_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

SUPPORTED_BROWSERS = [
    "chrome", "chromium", "firefox", "edge", "brave",
    "opera", "vivaldi", "safari", "brave-browser",
]

# Discover-search provider settings (user-configurable subset of providers
# queried by /api/search and /api/song/search). When the list is empty or
# contains the literal "all", every enabled provider is used.
DISCOVER_SETTINGS_FILE = data_file("discover_settings.json")
_discover_settings: dict = {"providers": []}
_discover_lock = threading.Lock()


def _load_discover_settings() -> None:
    global _discover_settings
    if DISCOVER_SETTINGS_FILE.exists():
        try:
            _discover_settings = json.loads(
                DISCOVER_SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(_discover_settings, dict):
                _discover_settings = {"providers": []}
        except Exception:
            _discover_settings = {"providers": []}
    else:
        _discover_settings = {"providers": []}


def _save_discover_settings() -> None:
    try:
        DISCOVER_SETTINGS_FILE.write_text(
            json.dumps(_discover_settings, indent=0), encoding="utf-8")
    except Exception:
        pass


def _resolve_discover_providers() -> list[str]:
    """Return the list of provider names the user has selected for
    discover search. Empty list or "all" means every enabled provider.
    """
    requested = _discover_settings.get("providers") or []
    if not requested or "all" in requested:
        return [p.name for p in enabled_providers()]
    return [n for n in requested if n in {p.name for p in enabled_providers()}]


def _resolve_tracklist_providers() -> list[str]:
    """Return enabled providers in the configured album-track fallback order."""
    requested = _discover_settings.get("track_providers") or []
    available = [p.name for p in enabled_providers()]
    if not requested or "all" in requested:
        return available
    return [name for name in requested if name in available]


# ---------------------------------------------------------------- .env loading + providers
#
# Load optional secrets (Spotify Client Credentials, etc.) from .env before
# any provider reads them. The file is optional — missing keys are fine and
# just leave those providers disabled.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env", override=False)
except ImportError:
    pass  # python-dotenv not installed; env vars from the OS still work

import providers as _providers  # multi-source aggregator
from providers import (
    aggregate_search, aggregate_album_tracks, aggregate_song_search,
    enabled_providers, norm,
)


# ---------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_thumb_cache()
    _load_album_tracks_cache()
    _load_discover_settings()
    enabled = [p.name for p in enabled_providers()]
    print(f"[startup] cookie.txt = {COOKIE_FILE} exists={COOKIE_FILE.exists()}")
    print(f"[startup] downloads  = {DOWNLOAD_DIR}")
    print(f"[startup] thumb cache entries = {len(_thumb_cache)}")
    print(f"[startup] album-tracks cache entries = {len(_album_tracks_cache)}")
    print(f"[providers] enabled: {', '.join(enabled) or '(none)'}")
    print(f"[discover] active providers: {', '.join(_resolve_discover_providers()) or '(none)'}")
    yield


app = FastAPI(title="Sleeve", lifespan=lifespan)


@app.middleware("http")
async def require_jellyfin_admin(request: Request, call_next):
    """Require a signed-in Jellyfin administrator for every app route."""
    path = request.url.path
    public_paths = {"/login", "/welcome", "/api/auth/login", "/api/auth/logout", "/logout"}
    if path in public_paths or path.startswith("/static/"):
        return await call_next(request)

    user = jellyfin_auth.get_session(request.cookies.get(jellyfin_auth.SESSION_COOKIE))
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "Sign in with your Jellyfin administrator account to continue."}, status_code=401)
        destination = request.url.path
        if request.url.query:
            destination += "?" + request.url.query
        completed = automator.load_profile().setup_complete
        entrypoint = "/login" if completed else "/welcome"
        return RedirectResponse(entrypoint + "?next=" + quote(destination, safe="/"), status_code=303)

    request.state.jellyfin_user = user
    return await call_next(request)


class _NoCacheStaticFiles(StaticFiles):
    """Static files with no-cache headers so the browser always picks up
    the latest JS / CSS. Without this, after we rename endpoints or fix
    bugs in automator.js, the user keeps hitting the old (cached) script
    and gets 404s on endpoints that exist on the server but not in their
    cached client. The HTML is small enough that re-fetching it on every
    load is fine."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        # StaticFiles returns a FileResponse / Response; both support headers.
        try:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        except Exception:
            pass
        return resp


app.mount("/static", _NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------- trending

_trending_cache: dict = {"at": None, "songs": [], "source": ""}

FALLBACK_SONGS = [
    {"rank": 1, "title": "Ordinary", "artist": "Alex Warren", "artwork": ""},
    {"rank": 2, "title": "Golden", "artist": "HUNTR/X", "artwork": ""},
    {"rank": 3, "title": "APT.", "artist": "ROSÉ & Bruno Mars", "artwork": ""},
    {"rank": 4, "title": "Die With A Smile", "artist": "Lady Gaga & Bruno Mars", "artwork": ""},
    {"rank": 5, "title": "Espresso", "artist": "Sabrina Carpenter", "artwork": ""},
]


def _fetch_billboard() -> list[dict]:
    url = "https://www.billboard.com/charts/hot-100/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    songs: list[dict] = []
    for li in soup.select("li.o-chart-results-list__item"):
        h3 = li.select_one("h3.c-title")
        if not h3:
            continue
        title = h3.get_text(strip=True)
        if not title or len(title) > 120:
            continue
        span = li.select_one("span.c-label")
        artist = span.get_text(strip=True) if span else ""
        if not artist or artist.isdigit():
            continue
        if any(s["title"] == title for s in songs):
            continue
        songs.append({"rank": len(songs) + 1, "title": title, "artist": artist, "artwork": ""})
        if len(songs) >= 10:
            break
    if len(songs) < 10:
        raise RuntimeError(f"Billboard parse yielded only {len(songs)} songs (markup changed?)")
    return songs


def _fetch_itunes_top_songs() -> list[dict]:
    """Read the public top-songs feed, including its cover art."""
    url = "https://itunes.apple.com/us/rss/topsongs/limit=10/json"
    r = _ITUNES.get(url, timeout=_ITUNES_TIMEOUT)
    r.raise_for_status()
    entries = r.json()["feed"]["entry"]
    songs = []
    for i, e in enumerate(entries, 1):
        images = e.get("im:image") or []
        artwork = images[-1].get("label", "") if images else ""
        artwork = artwork.replace("100x100bb", "300x300bb").replace("100x100", "300x300")
        songs.append({
            "rank": i,
            "title": e["im:name"]["label"],
            "artist": e["im:artist"]["label"],
            "artwork": artwork,
        })
    return songs


def get_trending(force: bool = False) -> dict:
    global _trending_cache
    if not force and _trending_cache["at"]:
        if (datetime.now() - _trending_cache["at"] < timedelta(hours=1)
                and all("artwork" in song for song in _trending_cache["songs"][:10])):
            return {"songs": _trending_cache["songs"], "source": _trending_cache["source"],
                    "updated_at": _trending_cache["at"].isoformat()}
    if not force and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            at = datetime.fromisoformat(data["updated_at"])
            if (datetime.now() - at < timedelta(hours=1) and data.get("songs")
                    and all("artwork" in song for song in data["songs"][:10])):
                _trending_cache = {"at": at, "songs": data["songs"], "source": data["source"]}
                return data
        except Exception:
            pass

    songs, source = [], "fallback-sample"
    try:
        songs = _fetch_itunes_top_songs()
        source = "iTunes Top Songs"
    except Exception as e:
        print(f"[trending] iTunes failed ({e}), trying Billboard fallback...")
        try:
            songs = _fetch_billboard()
            source = "Billboard Hot 100 (artwork unavailable)"
        except Exception as e2:
            print(f"[trending] Billboard also failed ({e2}), using built-in sample.")
            songs = list(FALLBACK_SONGS)

    at = datetime.now()
    _trending_cache = {"at": at, "songs": songs, "source": source}
    try:
        CACHE_FILE.write_text(json.dumps(
            {"songs": songs, "source": source, "updated_at": at.isoformat()}),
            encoding="utf-8")
    except Exception:
        pass
    return {"songs": songs, "source": source, "updated_at": at.isoformat()}


# ---------------------------------------------------------------- albums

_albums_cache: dict = {"at": None, "albums": [], "source": ""}
_genre_albums_cache: dict[str, dict] = {}
ALBUM_GENRES = {
    "all": None,
    "pop": 14,
    "country": 6,
    "rock": 21,
    "hip-hop": 18,
    "k-pop": 51,
    "alternative": 20,
}
ALBUM_GENRE_LABELS = {
    "all": "All genres",
    "pop": "Pop",
    "country": "Country",
    "rock": "Rock",
    "hip-hop": "Hip-Hop",
    "k-pop": "K-pop",
    "alternative": "Alternative",
}

FALLBACK_ALBUMS = [
    {"rank": 1, "title": "Sample Album", "artist": "Demo Artist", "artwork": ""},
]


def _fetch_itunes_albums(genre_id: int | None = None) -> list[dict]:
    """Apple RSS Top Albums — no key needed; optionally scoped to a genre."""
    genre_segment = f"/genre={genre_id}" if genre_id is not None else ""
    url = f"https://itunes.apple.com/us/rss/topalbums/limit=50{genre_segment}/json"
    r = _ITUNES.get(url, timeout=_ITUNES_TIMEOUT)
    r.raise_for_status()
    entries = r.json()["feed"]["entry"]
    albums = []
    for i, e in enumerate(entries, 1):
        # iTunes returns im:image as a list, smallest → largest. Take last (biggest).
        images = e.get("im:image", []) or []
        artwork = ""
        if images:
            artwork = images[-1].get("label", "")
            # bump 100x100bb → 600x600bb for crisper covers
            artwork = artwork.replace("100x100bb", "600x600bb").replace("100x100", "600x600")
        albums.append({
            "id": (e.get("id") or {}).get("attributes", {}).get("im:id", ""),
            "rank": i,
            "title": e["im:name"]["label"],
            "artist": e["im:artist"]["label"],
            "artwork": artwork,
        })
    return albums


def get_albums(force: bool = False, genre: str = "all") -> dict:
    """Return {albums, source, updated_at}. Cached 1h on disk + memory."""
    genre = (genre or "all").strip().lower()
    if genre not in ALBUM_GENRES:
        raise ValueError("Unknown album genre.")
    if genre != "all":
        cached = _genre_albums_cache.get(genre)
        if not force and cached and datetime.now() - cached["at"] < timedelta(hours=1):
            return {key: value for key, value in cached.items() if key != "at"}
        albums = _fetch_itunes_albums(ALBUM_GENRES[genre])
        result = {
            "albums": albums,
            "source": f"iTunes Top Albums · {ALBUM_GENRE_LABELS[genre]}",
            "updated_at": datetime.now().isoformat(),
        }
        _genre_albums_cache[genre] = {**result, "at": datetime.now()}
        return result

    global _albums_cache
    if not force and _albums_cache["at"]:
        if datetime.now() - _albums_cache["at"] < timedelta(hours=1):
            return {"albums": _albums_cache["albums"], "source": _albums_cache["source"],
                    "updated_at": _albums_cache["at"].isoformat()}
    if not force and ALBUM_CACHE_FILE.exists():
        try:
            data = json.loads(ALBUM_CACHE_FILE.read_text(encoding="utf-8"))
            at = datetime.fromisoformat(data["updated_at"])
            if datetime.now() - at < timedelta(hours=1) and data.get("albums"):
                _albums_cache = {"at": at, "albums": data["albums"], "source": data["source"]}
                return data
        except Exception:
            pass

    albums, source = [], "fallback-sample"
    try:
        albums = _fetch_itunes_albums()
        source = "iTunes Top Albums"
    except Exception as e:
        print(f"[albums] iTunes failed ({e}), using built-in sample.")
        albums = list(FALLBACK_ALBUMS)

    at = datetime.now()
    _albums_cache = {"at": at, "albums": albums, "source": source}
    try:
        ALBUM_CACHE_FILE.write_text(json.dumps(
            {"albums": albums, "source": source, "updated_at": at.isoformat()}),
            encoding="utf-8")
    except Exception:
        pass
    return {"albums": albums, "source": source, "updated_at": at.isoformat()}


def _annotate_albums_on_server(albums: list[dict]) -> list[dict]:
    """Mark album and track matches against the configured Jellyfin library."""
    if not albums:
        return albums
    profile = automator.load_profile()
    if not profile.is_configured():
        for album in albums:
            album["alreadyOnServer"] = False
            for track in album.get("tracks") or []:
                track["trackAlreadyOnServer"] = False
        return albums
    try:
        client = automator.JellyfinClient(profile.jellyfin_url, profile.jellyfin_api_key)
        return automator.mark_already_on_server(albums, client)
    except Exception as exc:
        print(f"[discover] Jellyfin library match failed: {type(exc).__name__}: {exc}")
        for album in albums:
            album["alreadyOnServer"] = False
            for track in album.get("tracks") or []:
                track["trackAlreadyOnServer"] = False
        return albums


def _annotate_tracks_on_server(tracks: list[dict]) -> list[dict]:
    """Annotate provider track results using Automator's Jellyfin matching."""
    if not tracks:
        return tracks
    profile = automator.load_profile()
    if not profile.is_configured():
        for track in tracks:
            track["trackAlreadyOnServer"] = False
        return tracks
    try:
        client = automator.JellyfinClient(profile.jellyfin_url, profile.jellyfin_api_key)
        server_tracks = client.get_recent_tracks(limit=5000)
        signatures = {
            automator._track_signature(track.get("artist", ""), track.get("title", ""),
                                       int(track.get("duration_ms") or 0))
            for track in server_tracks
        }
        title_artist_signatures = {
            (automator.canonical(track.get("artist", "")), automator.canonical(track.get("title", "")))
            for track in server_tracks
        }
        for track in tracks:
            duration_ms = int(track.get("duration_ms") or 0)
            signature = automator._track_signature(track.get("artist", ""), track.get("title", ""), duration_ms)
            title_artist = (automator.canonical(track.get("artist", "")), automator.canonical(track.get("title", "")))
            track["trackAlreadyOnServer"] = signature in signatures or (
                not duration_ms and title_artist in title_artist_signatures
            )
    except Exception as exc:
        print(f"[discover] Jellyfin track match failed: {type(exc).__name__}: {exc}")
        for track in tracks:
            track["trackAlreadyOnServer"] = False
    return tracks


def _itunes_album_for_song_title(query: str) -> dict | None:
    """Find the top exact iTunes song match and return its parent album card."""
    query = (query or "").strip()
    if not query:
        return None
    try:
        response = _itunes_session_safe().get(
            "https://itunes.apple.com/search",
            params={"term": query, "media": "music", "entity": "song", "limit": 25},
            timeout=8,
        )
        response.raise_for_status()
        matches = response.json().get("results") or []
        exact = next((item for item in matches
                      if norm(item.get("trackName", "")) == norm(query)
                      and item.get("collectionId") and item.get("collectionName")), None)
        if not exact:
            return None
        collection_id = exact["collectionId"]
        title = exact.get("collectionName") or ""
        artist = exact.get("artistName") or ""
        artwork = exact.get("artworkUrl100") or ""
        artwork = re.sub(r"\d{2,4}x\d{2,4}bb", "600x600bb", artwork)
        return {
            "id": int(collection_id),
            "title": title,
            "artist": artist,
            "artwork": artwork,
            "track_count": exact.get("trackCount") or 0,
            "release_date": (exact.get("releaseDate") or "")[:10],
            "genre": exact.get("primaryGenreName") or "",
            "source": "itunes",
            "source_id": str(collection_id),
            "source_url": f"https://music.apple.com/us/album/{_providers.slugify(title)}/{collection_id}",
            "matched_song": exact.get("trackName") or query,
        }
    except Exception as exc:
        print(f"[automator] iTunes song-to-album lookup failed: {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------- iTunes search

def _bump_artwork(url: str, size: int = 300) -> str:
    """iTunes artwork comes in 100x100bb / 170x170bb / 60x60bb variants.
    Swap to a larger size for sharper cards. Default 300 keeps payloads small."""
    if not url:
        return ""
    import re as _re
    return _re.sub(r"\d{2,4}x\d{2,4}bb", f"{size}x{size}bb", url)


def itunes_search(query: str, kind: str = "all", limit: int = 25) -> dict:
    """Multi-provider search, fronted by the aggregator. See providers.py
    for which databases are queried and how results are deduped.

    kind: "song" | "album" | "all" — we currently only fan out for
    "album" and "all" (the aggregator's search is album-oriented). For
    "song" callers, we keep returning an empty `songs` list and let the
    UI fall back to the dedicated /api/song/search route.
    """
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty query", "albums": [], "songs": []}

    kind = (kind or "all").lower()
    if kind not in ("song", "album", "all"):
        kind = "all"

    if kind in ("album", "all"):
        agg = aggregate_search(q, kind="album", limit=limit,
                                providers=_resolve_discover_providers())
        return {
            "ok": agg.get("ok", False),
            "albums": agg.get("albums", []),
            "songs": agg.get("songs", []),
            "query": q,
            "kind": kind,
            "provider_summary": agg.get("provider_summary", {}),
            "error": agg.get("error", ""),
        }

    return {"ok": True, "albums": [], "songs": [],
            "query": q, "kind": kind,
            "provider_summary": {},
            "error": "song search via /api/song/search"}


def itunes_album_tracks(collection_id,
                         title_hint: str = "",
                         artist_hint: str = "") -> dict:
    """Look up all tracks for an album by id + optional title/artist hints.

    collection_id may be:
      * a numeric iTunes collectionId (e.g. 1122782080) -> forwarded to the
        iTunes provider
      * a synthetic string like 'deezer:301663' or 'musicbrainz:c1b...'
        from the aggregator's search -> we DON'T forward it to iTunes,
        we just rely on title+artist to look up the album elsewhere
      * any other string -> treated as opaque; we try the cache first
        then ask the aggregator to find the album by title+artist only

    Thin wrapper around providers.aggregate_album_tracks that:
      * routes the iTunes collectionId to the iTunes provider
      * passes the artist+title hints to every other provider so they
        can find the same album by name
      * serves cached results when fresh (14-day TTL)
    """
    if not collection_id and not (title_hint and artist_hint):
        return {"ok": False, "error": "missing id or title+artist",
                "album": {}, "tracks": []}

    cid_str = str(collection_id).strip() if collection_id else ""
    itunes_cid: int | None = None
    if cid_str and cid_str.lower().startswith("itunes:"):
        cid_str = cid_str.split(":", 1)[1]
    try:
        itunes_cid = int(cid_str) if cid_str else None
    except ValueError:
        itunes_cid = None  # non-numeric id (e.g. musicbrainz:abc-...) -> skip iTunes

    # Cache key: prefer numeric iTunes id; otherwise hash by name.
    cache_key: int | None = itunes_cid
    track_providers = _resolve_tracklist_providers()
    if cache_key is not None:
        cached = _album_tracks_cached_get(cache_key, track_providers)
        if cached:
            return _with_workflow_track_ids(cached, collection_id)
    if title_hint and artist_hint:
        cached = _album_tracks_cached_get_name(title_hint, artist_hint, track_providers)
        if cached:
            return _with_workflow_track_ids(cached, collection_id)

    agg = _aggregate_album_tracks_by_priority(
        collection_id=str(itunes_cid) if itunes_cid is not None else None,
        title=title_hint,
        artist=artist_hint,
        timeout_per_provider=8.0,
        providers=track_providers,
    )

    # If the aggregator didn't return ok but we have title/artist
    # hints, try once more with a name-only aggregator call. Useful when
    # iTunes is rate-limiting and we don't have the collectionId.
    if not agg.get("ok") and title_hint and artist_hint:
        agg = _aggregate_album_tracks_by_priority(
            collection_id=None,
            title=title_hint,
            artist=artist_hint,
            timeout_per_provider=8.0,
            providers=track_providers,
        )

    if not agg.get("ok"):
        return {"ok": False,
                "error": agg.get("error", "all providers failed"),
                "album": {}, "tracks": []}

    payload = {
        "ok": True,
        "album": agg.get("album", {}),
        "tracks": agg.get("tracks", []),
        "source": agg.get("source", ""),
        "providers": agg.get("providers", {}),
        "dedup": agg.get("dedup") or {},
    }
    payload = _with_workflow_track_ids(payload, collection_id)
    if cache_key is not None:
        _album_tracks_cached_put(cache_key, payload, track_providers)
    if title_hint and artist_hint:
        _album_tracks_cached_put_name(title_hint, artist_hint, payload, track_providers)
    return payload


def _aggregate_album_tracks_by_priority(*, providers: list[str], **kwargs) -> dict:
    """Try tracklist providers in order and return the first usable result.

    Tracklists are alternatives, not independent songs to concatenate. Using
    one complete provider result prevents slight timing/artist differences
    across catalogs from surfacing as duplicate tracks.
    """
    failures = []
    for provider_name in providers:
        result = aggregate_album_tracks(providers=[provider_name], **kwargs)
        if result.get("ok") and result.get("tracks"):
            return result
        failures.append({"provider": provider_name,
                         "error": result.get("error") or "no tracks returned"})
    return {"ok": False,
            "error": "No selected provider returned an album tracklist.",
            "details": failures,
            "album": {}, "tracks": [], "providers": {}, "source": ""}


def _with_workflow_track_ids(payload: dict, collection_id) -> dict:
    """Give each album track a unique key for lyric review and job choices.

    Provider results expose ``source_id`` but often omit ``id`` entirely.
    Include the album and position so repeated songs across albums stay
    independent. Preserve provider IDs in ``source_id``.
    """
    album_id = (payload.get("album") or {}).get("id") or collection_id
    return {
        **payload,
        "tracks": [
            {**track, "id": f"{album_id}:{index}"}
            for index, track in enumerate(payload.get("tracks") or [], 1)
        ],
    }


def itunes_song_album(track_id: int = 0, *, title: str = "",
                       artist: str = "") -> dict:
    """Look up a song's album + siblings.

    Two modes:
      * iTunes-native: pass track_id (numeric). The aggregator forwards it
        to the iTunes provider's /lookup endpoint.
      * Name-based: pass title + artist. We run a song search and use
        the first hit's album.
    """
    if track_id:
        try:
            cid = int(track_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad track_id"}
        # Need the album metadata (title+artist) so we can pass to fallback
        # providers. Ask iTunes for the metadata-only lookup first.
        try:
            r = _ITUNES_SESSION = _itunes_session_safe()
            r = r.get("https://itunes.apple.com/lookup",
                      params={"id": cid}, timeout=8)
            data = r.json()
            results = data.get("results") or []
        except Exception:
            results = []
        if results:
            meta = results[0]
            title = meta.get("trackName") or title
            artist = meta.get("artistName") or artist
            col_id = meta.get("collectionId")
            if col_id:
                return itunes_album_tracks(col_id, title_hint=meta.get("collectionName") or "",
                                            artist_hint=meta.get("artistName") or "")
        return {"ok": False,
                "error": "iTunes lookup failed to resolve that track id",
                "album": {}, "tracks": []}

    if title and artist:
        # The chart page gives us only a title and artist. Resolve that exact
        # track through Apple's Search API first so the parent album ID comes
        # from song metadata instead of treating the song title as an album.
        try:
            response = _itunes_session_safe().get(
                "https://itunes.apple.com/search",
                params={"term": f"{title} {artist}", "media": "music",
                        "entity": "song", "limit": 25},
                timeout=8,
            )
            response.raise_for_status()
            results = response.json().get("results") or []
            wanted_title, wanted_artist = norm(title), norm(artist)
            exact = next((item for item in results
                          if norm(item.get("trackName", "")) == wanted_title
                          and norm(item.get("artistName", "")) == wanted_artist
                          and item.get("collectionId")), None)
            if exact:
                return itunes_album_tracks(
                    exact["collectionId"],
                    title_hint=exact.get("collectionName") or "",
                    artist_hint=exact.get("artistName") or artist,
                )
        except Exception:
            pass  # Preserve the multi-provider fallback below.

        # Fall back to provider song search when iTunes has no exact match.
        s = aggregate_song_search(f"{title} {artist}", limit=5)
        songs = (s.get("songs") or [])
        if not songs:
            return {"ok": False,
                    "error": "no song found",
                    "album": {}, "tracks": []}
        first = songs[0]
        source_track_id = str(first.get("source_id") or "")
        if first.get("source") == "itunes" and source_track_id.isdigit():
            return itunes_song_album(int(source_track_id), title=title, artist=artist)
        # We need an album id; song search may not include one. Use the
        # album lookup directly with title+artist.
        agg = _aggregate_album_tracks_by_priority(
            collection_id=None,
            title=first.get("title", ""),
            artist=first.get("artist", ""),
            timeout_per_provider=8.0,
            providers=_resolve_tracklist_providers(),
        )
        if agg.get("ok"):
            return {"ok": True,
                    "album": agg.get("album", {}),
                    "tracks": agg.get("tracks", []),
                    "source": agg.get("source", ""),
                    "providers": agg.get("providers", {})}
        return {"ok": False, "error": agg.get("error", "no album"),
                "album": {}, "tracks": []}

    return {"ok": False, "error": "need track_id or title+artist",
            "album": {}, "tracks": []}


def _itunes_session_safe():
    """Reuse the shared _ITUNES session if present; build a fresh one
    with the same tuning otherwise. Keeps tests / direct imports happy."""
    if "_ITUNES" in globals() and _ITUNES is not None:
        return _ITUNES
    from providers import ITunesProvider
    return ITunesProvider().s


# ---------------------------------------------------------------- cookies

def cookie_status() -> dict:
    exists = COOKIE_FILE.exists()
    size = COOKIE_FILE.stat().st_size if exists else 0
    count = 0
    if exists and size:
        try:
            count = sum(1 for line in COOKIE_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                        if line and not line.startswith("#"))
        except Exception:
            pass
    return {"exists": exists, "path": str(COOKIE_FILE), "size": size,
            "count": count, "compat_exists": COOKIES_FILE_COMPAT.exists()}


def export_cookies(browser: str) -> dict:
    browser = (browser or "").strip().lower()
    if browser not in SUPPORTED_BROWSERS:
        return {"ok": False, "error": f"Pick one of: {', '.join(SUPPORTED_BROWSERS)}"}
    try:
        from yt_dlp import cookies as yt_cookies
    except ImportError as e:
        return {"ok": False, "error": f"yt-dlp not installed: {e}"}
    try:
        jar = yt_cookies.extract_cookies_from_browser(browser)
    except Exception as e:
        return {"ok": False, "error":
                f"{type(e).__name__}: {str(e)[:300]} (Tip: fully CLOSE {browser} first — "
                f"Windows locks its cookie DB while running.)"}
    count = len(list(jar))
    try:
        jar.save(str(COOKIE_FILE), ignore_discard=True, ignore_expires=True)
        shutil.copyfile(COOKIE_FILE, COOKIES_FILE_COMPAT)
    except Exception as e:
        return {"ok": False, "error": f"Write failed: {e}"}
    return {"ok": True, "count": count, "path": str(COOKIE_FILE),
            "browser": browser, **cookie_status()}


def _yt_opts_for_download() -> dict:
    """Pick cookies and available FFmpeg tools for yt-dlp."""
    extra: dict = {}
    ffmpeg = shutil.which("ffmpeg") or (str(FFMPEG_EXE) if FFMPEG_EXE.exists() else None)
    ffprobe = shutil.which("ffprobe") or (str(FFPROBE_EXE) if FFPROBE_EXE.exists() else None)
    if ffmpeg:
        extra["ffmpeg_location"] = ffmpeg
    if ffprobe:
        extra["ffprobe_location"] = ffprobe
    # Cookie auth — primary file (new convention), fall back to legacy.
    # Self-heal in place before handing to yt-dlp so split/NUL-padded entries
    # don't poison the whole file. ensure_cookie_file_valid is cached by
    # mtime so the cost is one stat per download.
    if COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 0:
        try:
            ensure_cookie_file_valid(COOKIE_FILE)
        except Exception as e:
            print(f"[yt_opts] cookie self-heal skipped: {e}")
        extra["cookiefile"] = str(COOKIE_FILE)
    elif COOKIES_FILE_COMPAT.exists() and COOKIES_FILE_COMPAT.stat().st_size > 0:
        try:
            ensure_cookie_file_valid(COOKIES_FILE_COMPAT)
        except Exception:
            pass
        extra["cookiefile"] = str(COOKIES_FILE_COMPAT)

    # Anti-bot: try several YouTube player clients. Different clients expose
    # different format tables; falling back across them rescues the
    # "Requested format is not available" / "Sign in to confirm you're not a bot"
    # errors that some videos hit on the web client alone.
    extra["extractor_args"] = {
        "youtube": {
            "player_client": ["ios", "web", "web_safari", "mweb", "tv", "tv_embedded", "android"],
            "skip": ["translated_subs", "dash", "live_chat"],
        }
    }
    # Don't let YouTube hang a single format attempt forever — fail fast and
    # move to the next fallback. Default would block for ~3 minutes on a stalled
    # format list request.
    extra["socket_timeout"] = 20
    return extra


# ---------------------------------------------------------------- thumbnails

_thumb_cache: dict[str, str] = {}     # query -> video_id
_thumb_cache_lock = threading.Lock()


# ---------------------------------------------------------------- album tracks cache
#
# {str(collection_id): {"at": iso, "data": {...ok-payload...}}}
# In-memory mirror is the source of truth during the process lifetime;
# ALBUM_TRACKS_CACHE_FILE is the persisted copy so it survives restarts.
_album_tracks_cache: dict = {}
_album_tracks_lock = threading.Lock()


def _load_album_tracks_cache() -> None:
    global _album_tracks_cache
    if ALBUM_TRACKS_CACHE_FILE.exists():
        try:
            _album_tracks_cache = json.loads(ALBUM_TRACKS_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _album_tracks_cache = {}
    else:
        _album_tracks_cache = {}


def _save_album_tracks_cache() -> None:
    try:
        ALBUM_TRACKS_CACHE_FILE.write_text(
            json.dumps(_album_tracks_cache, indent=0), encoding="utf-8")
    except Exception:
        pass


def _tracklist_cache_key(provider_names: list[str]) -> str:
    return "tracklist-v2::" + ",".join(provider_names)


def _album_tracks_cached_get(cid: int, provider_names: list[str]) -> dict | None:
    """Return a fresh-enough cached payload, or None if stale/missing."""
    key = f"{_tracklist_cache_key(provider_names)}::{int(cid)}"
    with _album_tracks_lock:
        entry = _album_tracks_cache.get(key)
    if not entry:
        return None
    try:
        at = datetime.fromisoformat(entry["at"])
    except Exception:
        return None
    if datetime.now() - at > _ALBUM_TRACKS_TTL:
        return None
    return entry.get("data")


def _album_tracks_cached_put(cid: int, data: dict, provider_names: list[str]) -> None:
    """Persist a successful lookup result."""
    if not data or not data.get("ok"):
        return
    with _album_tracks_lock:
        key = f"{_tracklist_cache_key(provider_names)}::{int(cid)}"
        _album_tracks_cache[key] = {
            "at": datetime.now().isoformat(), "data": data}
    _save_album_tracks_cache()


def _name_cache_key(title: str, artist: str, provider_names: list[str]) -> str:
    """Stable cache key for a (title, artist) pair — uses providers.norm()
    so 'Don't Panic' from MusicBrainz and 'Don't Panic' from AudioDB
    collide on the same key.
    """
    return f"{_tracklist_cache_key(provider_names)}::name::{norm(title)}::{norm(artist)}"


def _album_tracks_cached_get_name(title: str, artist: str, provider_names: list[str]) -> dict | None:
    key = _name_cache_key(title, artist, provider_names)
    with _album_tracks_lock:
        entry = _album_tracks_cache.get(key)
    if not entry:
        return None
    try:
        at = datetime.fromisoformat(entry["at"])
    except Exception:
        return None
    if datetime.now() - at > _ALBUM_TRACKS_TTL:
        return None
    return entry.get("data")


def _album_tracks_cached_put_name(title: str, artist: str, data: dict,
                                  provider_names: list[str]) -> None:
    if not data or not data.get("ok"):
        return
    if not (title and artist):
        return
    key = _name_cache_key(title, artist, provider_names)
    with _album_tracks_lock:
        _album_tracks_cache[key] = {"at": datetime.now().isoformat(), "data": data}
    _save_album_tracks_cache()


def _load_thumb_cache() -> None:
    global _thumb_cache
    if THUMB_CACHE_FILE.exists():
        try:
            _thumb_cache = json.loads(THUMB_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _thumb_cache = {}
    else:
        _thumb_cache = {}


def _save_thumb_cache() -> None:
    try:
        THUMB_CACHE_FILE.write_text(json.dumps(_thumb_cache, indent=0), encoding="utf-8")
    except Exception:
        pass


def _normalize_query(q: str) -> str:
    return " ".join(q.lower().split())


SEARXNG_URL = os.getenv("SEARXNG_URL", "").strip().rstrip("/")


def _extract_video_id_from_url(url: str) -> str | None:
    """Pull the YouTube video id out of a watch / shorts / youtu.be URL."""
    if not url:
        return None
    import re
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else None


def _searxng_search(query: str, max_results: int = 5) -> list[dict]:
    """Call local SearXNG and return list of {title, url, thumbnail, video_id}.
    Returns [] on any failure — caller should fall back to yt-dlp."""
    if not SEARXNG_URL:
        return []
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
        print(f"[thumbnail] SearXNG failed ({e}); trying yt-dlp")
        return []

    out = []
    for item in (data.get("results") or [])[:max_results]:
        url = item.get("url") or ""
        # only YouTube URLs
        if not re.search(r"youtube\.com/(watch|playlist)|youtu\.be/|youtube\.com/shorts/", url):
            continue
        vid = _extract_video_id_from_url(url)
        out.append({
            "title": item.get("title") or "",
            "url": url,
            "thumbnail": item.get("thumbnail") or "",
            "video_id": vid,
        })
    return out


def lookup_thumbnail(query: str) -> dict:
    """Return {ok, thumb_url, maxres_url, video_id, source} for a song query.

    Path:
      1. In-memory cache (so we never re-search the same query twice)
      2. SearXNG — fastest, returns pre-extracted thumbnail URLs
      3. yt-dlp (full extraction, NOT extract_flat) — slower but gives us the
         thumbnail metadata directly from YouTube
      4. yt-dlp (extract_flat fallback) — last resort, we synthesize the i.ytimg.com URL
    """
    import re
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty query"}

    key = _normalize_query(q)
    with _thumb_cache_lock:
        cached = _thumb_cache.get(key)
    if cached:
        c = cached if isinstance(cached, dict) else {"video_id": cached}
        vid = c.get("video_id")
        return {"ok": True, "source": "cache",
                "video_id": vid,
                "thumb_url": c.get("thumb_url") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "maxres_url": c.get("maxres_url") or f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"}

    # 1. SearXNG
    searx_results = _searxng_search(q)
    for r in searx_results:
        # Prefer a direct thumbnail URL from SearXNG
        if r.get("thumbnail") and r.get("video_id"):
            cached_entry = {
                "video_id": r["video_id"],
                "thumb_url": r["thumbnail"],
                "maxres_url": f"https://i.ytimg.com/vi/{r['video_id']}/maxresdefault.jpg",
            }
            with _thumb_cache_lock:
                _thumb_cache[key] = cached_entry
                _save_thumb_cache()
            return {"ok": True, "source": "searxng",
                    "video_id": r["video_id"],
                    "thumb_url": cached_entry["thumb_url"],
                    "maxres_url": cached_entry["maxres_url"]}

    # 2. yt-dlp full extraction (gets us real thumbnail metadata)
    try:
        import yt_dlp
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "writethumbnail": False,
        }
        opts.update(_yt_opts_for_download())
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{q}", download=False)
        entries = (info or {}).get("entries") or []
        if not entries:
            return {"ok": False, "error": "no YouTube results"}
        first = entries[0]
        video_id = first.get("id")
        if not video_id:
            return {"ok": False, "error": "no video id"}

        # yt-dlp returns thumbnails as list of {url, width, height, ...}
        thumb_url = ""
        maxres_url = ""
        thumbs = first.get("thumbnails") or []
        if thumbs:
            # Prefer the largest
            sorted_thumbs = sorted(thumbs, key=lambda t: t.get("width", 0) * t.get("height", 0), reverse=True)
            thumb_url = sorted_thumbs[0].get("url", "")
            if len(sorted_thumbs) > 1:
                # Find a high-res one
                for t in sorted_thumbs:
                    if (t.get("width") or 0) >= 1280:
                        maxres_url = t["url"]
                        break
                else:
                    maxres_url = thumb_url

        # If yt-dlp didn't give us a usable thumb, fall back to i.ytimg.com
        if not thumb_url:
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        if not maxres_url:
            maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"

        cached_entry = {"video_id": video_id, "thumb_url": thumb_url, "maxres_url": maxres_url}
        with _thumb_cache_lock:
            _thumb_cache[key] = cached_entry
            _save_thumb_cache()
        return {"ok": True, "source": "ytdlp-full", "video_id": video_id,
                "thumb_url": thumb_url, "maxres_url": maxres_url}
    except Exception as e:
        # 3. Last resort: extract_flat + synthesize URL
        try:
            import yt_dlp
            opts = {
                "quiet": True, "no_warnings": True,
                "extract_flat": True, "skip_download": True, "noplaylist": True,
            }
            opts.update(_yt_opts_for_download())
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{q}", download=False)
            entries = (info or {}).get("entries") or []
            if entries and entries[0].get("id"):
                vid = entries[0]["id"]
                cached_entry = {"video_id": vid,
                                "thumb_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                                "maxres_url": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"}
                with _thumb_cache_lock:
                    _thumb_cache[key] = cached_entry
                    _save_thumb_cache()
                return {"ok": True, "source": "ytdlp-flat", "video_id": vid,
                        "thumb_url": cached_entry["thumb_url"],
                        "maxres_url": cached_entry["maxres_url"]}
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ---------------------------------------------------------------- download jobs

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _download_mp3_job(job_id: str, query_or_url: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
    try:
        import yt_dlp
        url = query_or_url.strip()
        # If user passed a URL, try it directly. Otherwise build a list of candidates
        # from ytsearch so we can fall through if the first hit is PO-token-blocked.
        candidate_urls: list[str] = []
        if url.startswith("http"):
            candidate_urls = [url]
        else:
            with _jobs_lock:
                job["status"] = "searching"
            search_opts = {"quiet": True, "no_warnings": True,
                           "extract_flat": True, "skip_download": True,
                           "noplaylist": True}
            search_opts.update(_yt_opts_for_download())
            # ytsearch3 — try a few candidates, pick the first that downloads.
            # 3 is enough for most songs; more = slower failure on PO-token-blocked ones.
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                info = ydl.extract_info(f"ytsearch3:{url}", download=False)
            entries = (info or {}).get("entries") or []
            if not entries:
                raise RuntimeError("No YouTube results found.")
            for e in entries:
                vid = e.get("id")
                if vid:
                    candidate_urls.append(f"https://www.youtube.com/watch?v={vid}")
            with _jobs_lock:
                job["title"] = entries[0].get("title", url)

        base_opts = {
            "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True, "no_warnings": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata"},
            ],
        }
        base_opts.update(_yt_opts_for_download())
        for rt in ("deno", "node", "bun"):
            if shutil.which(rt):
                base_opts["js_runtimes"] = {rt: {}}
                break

        # Try these formats in order per candidate URL. Two formats covers the
        # vast majority of cases; more options = slower failure on stubborn videos.
        format_attempts = [
            "bestaudio[acodec!=none]/bestaudio/best",
            "bestaudio/best",
        ]
        last_err = None
        title = ""
        chosen_url = ""
        before = {p.name for p in DOWNLOAD_DIR.glob("*") if p.is_file()}
        for cand_url in candidate_urls:
            # Check for cancellation between candidates
            with _jobs_lock:
                if job.get("cancel_requested"):
                    with _jobs_lock:
                        job["status"] = "cancelled"
                        job["title"] = (job.get("title") or "") + " (stopped)"
                    return
                job["status"] = "downloading"
                job["url"] = cand_url
            for fmt in format_attempts:
                try:
                    opts = dict(base_opts)
                    opts["format"] = fmt
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(cand_url, download=True)
                        title = (info or {}).get("title") or cand_url
                    chosen_url = cand_url
                    last_err = None
                    break
                except Exception as e:
                    msg = str(e)
                    last_err = e
                    if "format is not available" in msg.lower():
                        print(f"[download] format '{fmt}' failed for {cand_url} — trying next")
                        continue
                    raise
            if last_err is None:
                break  # success
            else:
                print(f"[download] candidate {cand_url} exhausted formats, trying next result")
        if last_err is not None and not chosen_url:
            raise last_err

        after = [p for p in DOWNLOAD_DIR.glob("*")
                 if p.is_file() and p.name not in before and p.suffix.lower() == ".mp3"]
        if not after:
            after = sorted(DOWNLOAD_DIR.glob("*.mp3"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:1]
        with _jobs_lock:
            job["status"] = "done"
            job["title"] = title
            job["file"] = after[0].name if after else ""
    except Exception as e:
        with _jobs_lock:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()


def _download_album_job(job_id: str, album: dict) -> None:
    """Fetch the album's tracklist from iTunes, then download each song individually
    as a separate MP3 in ./downloads/<Artist>/<Album>/<Track>.mp3.

    Progress is reported via job['progress'] = {done, total, current_track} so the
    frontend can show "3 / 12: Come Together".
    """
    with _jobs_lock:
        job = _jobs[job_id]
    title_album = album.get("title", "")
    artist_album = album.get("artist", "")
    album_id = album.get("id")

    # 1. Resolve the tracklist via iTunes lookup
    with _jobs_lock:
        job["status"] = "loading tracks"
        job["title"] = f"{title_album} — {artist_album}"

    tracks = []
    try:
        if album_id:
            lookup = itunes_album_tracks(int(album_id))
            if lookup.get("ok"):
                tracks = lookup.get("tracks") or []
    except Exception as e:
        print(f"[album] iTunes lookup failed: {e}")

    if not tracks:
        # No tracklist available — fall back to downloading one big "full album" video
        _download_single_album_video(job_id, title_album, artist_album)
        return

    total = len(tracks)
    completed = 0
    failed = []

    with _jobs_lock:
        job["progress"] = {"done": 0, "total": total, "current": ""}

    # 2. Iterate over each track and download
    for t in tracks:
        # Check for cancellation between tracks
        with _jobs_lock:
            if job.get("cancel_requested"):
                with _jobs_lock:
                    job["status"] = "cancelled"
                    job["title"] = f"{title_album} — {artist_album} (stopped at {completed}/{total})"
                return

        track_title = t.get("title", "")
        track_artist = t.get("artist") or artist_album
        if not track_title:
            completed += 1
            continue

        with _jobs_lock:
            job["status"] = "downloading"
            job["title"] = f"{title_album} — {artist_album}"
            job["progress"] = {"done": completed, "total": total,
                               "current": f"{track_artist} — {track_title}"}

        try:
            _download_single_track(
                job_id=job_id,
                title=track_title,
                artist=track_artist,
                album=title_album,
                album_artist=artist_album,
                track_number=t.get("track_number") or 0,
            )
        except Exception as e:
            print(f"[album] track '{track_title}' failed: {e}")
            failed.append(track_title)
        finally:
            completed += 1
            with _jobs_lock:
                job["progress"] = {"done": completed, "total": total,
                                   "current": "", "failed": failed}

    with _jobs_lock:
        if failed and completed == len(failed):
            job["status"] = "error"
            job["error"] = f"All {len(failed)} tracks failed: {failed[0]}"
        elif failed:
            job["status"] = "done"
            job["title"] = f"{title_album} — {completed}/{total} tracks downloaded ({len(failed)} failed)"
        else:
            job["status"] = "done"
            job["title"] = f"{title_album} — all {total} tracks downloaded"


def _download_single_track(job_id: str, title: str, artist: str,
                            album: str, album_artist: str, track_number: int) -> None:
    """Search YouTube for the track and download as MP3 into the album folder."""
    import yt_dlp
    query = f"{artist} - {title} audio"

    # Search candidates
    search_opts = {"quiet": True, "no_warnings": True,
                   "extract_flat": True, "skip_download": True,
                   "noplaylist": True}
    search_opts.update(_yt_opts_for_download())
    with yt_dlp.YoutubeDL(search_opts) as ydl:
        info = ydl.extract_info(f"ytsearch2:{query}", download=False)
    entries = (info or {}).get("entries") or []
    if not entries:
        raise RuntimeError(f"No YouTube results for {query}")
    candidate_urls = [f"https://www.youtube.com/watch?v={e['id']}"
                      for e in entries if e.get("id")]

    # Folder layout: ./downloads/<AlbumArtist>/<Album>/<TrackNum> <Track>.mp3
    safe_artist = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', album_artist).strip() or "Unknown Artist"
    safe_album = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', album).strip() or "Unknown Album"
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip() or "track"
    prefix = f"{track_number:02d} - " if track_number else ""
    outtmpl = str(DOWNLOAD_DIR / safe_artist / safe_album / f"{prefix}{safe_title}.%(ext)s")

    base_opts = {
        "format": "bestaudio[acodec!=none]/bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True, "no_warnings": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
        ],
    }
    base_opts.update(_yt_opts_for_download())
    for rt in ("deno", "node", "bun"):
        if shutil.which(rt):
            base_opts["js_runtimes"] = {rt: {}}
            break

    format_attempts = [
        "bestaudio[acodec!=none]/bestaudio/best",
        "bestaudio/best",
    ]
    last_err = None
    for cand_url in candidate_urls:
        for fmt in format_attempts:
            try:
                opts = dict(base_opts)
                opts["format"] = fmt
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([cand_url])
                last_err = None
                return
            except Exception as e:
                msg = str(e)
                last_err = e
                if "format is not available" in msg.lower():
                    print(f"[track] format '{fmt}' failed for {cand_url} — next")
                    continue
                raise
        print(f"[track] candidate {cand_url} exhausted formats — next result")
    if last_err:
        raise last_err


def _download_single_album_video(job_id: str, title_album: str, artist_album: str) -> None:
    """Fallback when no tracklist is available: download one YouTube 'full album' video."""
    import yt_dlp
    with _jobs_lock:
        job = _jobs[job_id]
    try:
        query = f"{artist_album} {title_album} full album"
        with _jobs_lock:
            job["status"] = "searching"
        search_opts = {"quiet": True, "no_warnings": True,
                       "extract_flat": True, "skip_download": True,
                       "noplaylist": True}
        search_opts.update(_yt_opts_for_download())
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(f"ytsearch2:{query}", download=False)
        entries = (info or {}).get("entries") or []
        if not entries:
            raise RuntimeError(f"No YouTube results for '{title_album}'")
        url = f"https://www.youtube.com/watch?v={entries[0]['id']}"

        base_opts = {
            "format": "bestaudio[acodec!=none]/bestaudio/best",
            "outtmpl": str(DOWNLOAD_DIR / artist_album / f"{title_album} - %(title)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True, "no_warnings": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata"},
            ],
        }
        base_opts.update(_yt_opts_for_download())
        for rt in ("deno", "node", "bun"):
            if shutil.which(rt):
                base_opts["js_runtimes"] = {rt: {}}
                break

        for fmt in ("bestaudio[acodec!=none]/bestaudio/best", "bestaudio/best"):
            try:
                opts = dict(base_opts)
                opts["format"] = fmt
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                break
            except Exception as e:
                if "format is not available" not in str(e).lower():
                    raise
                continue

        with _jobs_lock:
            job["status"] = "done"
            job["title"] = f"{title_album} — {artist_album} (single-file fallback)"
    except Exception as e:
        with _jobs_lock:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()


# ---------------------------------------------------------------- routes

@app.get("/")
def index_root(request: Request):
    """Send signed-in users to their library; middleware handles first run."""
    return RedirectResponse(url="/gallery", status_code=303)


@app.get("/welcome")
def welcome_page(request: Request, next: str = Query("/gallery")):
    user = jellyfin_auth.get_session(request.cookies.get(jellyfin_auth.SESSION_COOKIE))
    if user:
        return RedirectResponse("/gallery", status_code=303)
    profile = automator.load_profile()
    if profile.setup_complete:
        return RedirectResponse("/login", status_code=303)
    parsed_next = urlsplit(next)
    safe_next = (next if next.startswith("/") and not next.startswith("//")
                 and "\\" not in next and not parsed_next.scheme and not parsed_next.netloc
                 else "/gallery")
    return templates.TemplateResponse(request, "welcome.html", {"next_path": safe_next})


@app.get("/login")
def login_page(request: Request, next: str = Query("/gallery")):
    user = jellyfin_auth.get_session(request.cookies.get(jellyfin_auth.SESSION_COOKIE))
    if user:
        return RedirectResponse("/gallery", status_code=303)
    profile = automator.load_profile()
    parsed_next = urlsplit(next)
    safe_next = (next if next.startswith("/") and not next.startswith("//")
                 and "\\" not in next and not parsed_next.scheme and not parsed_next.netloc
                 else "/gallery")
    return templates.TemplateResponse(request, "login.html", {
        "server_url": jellyfin_auth.configured_server_url(profile.jellyfin_url),
        "next_path": safe_next,
    })


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    client_key = request.client.host if request.client else "unknown"
    if jellyfin_auth.login_is_limited(client_key):
        return JSONResponse({"ok": False, "error": "Too many attempts. Wait a minute, then try again."}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    profile = automator.load_profile()
    server_url = jellyfin_auth.configured_server_url(profile.jellyfin_url)
    if not server_url:
        try:
            server_url = jellyfin_auth.validate_server_url(str(body.get("server_url") or ""))
        except jellyfin_auth.JellyfinLoginError as exc:
            jellyfin_auth.record_failed_login(client_key)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        user = jellyfin_auth.authenticate_admin(
            server_url,
            str(body.get("username") or ""),
            str(body.get("password") or ""),
        )
    except jellyfin_auth.JellyfinLoginError as exc:
        jellyfin_auth.record_failed_login(client_key)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)

    if profile.jellyfin_url != server_url or not profile.setup_complete:
        profile.jellyfin_url = server_url
        profile.setup_complete = True
        try:
            automator.save_profile(profile)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"Could not save the Jellyfin address in the Automator profile database: {exc}"}, status_code=500)
    jellyfin_auth.clear_failed_logins(client_key)
    session_id = jellyfin_auth.create_session(user)
    response = JSONResponse({"ok": True, "username": user["username"]})
    secure_cookie = request.url.scheme == "https" or os.environ.get("SLEEVE_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
    response.set_cookie(
        jellyfin_auth.SESSION_COOKIE,
        session_id,
        max_age=jellyfin_auth.SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )
    return response


@app.get("/logout")
def logout(request: Request):
    jellyfin_auth.delete_session(request.cookies.get(jellyfin_auth.SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(jellyfin_auth.SESSION_COOKIE, path="/")
    return response


@app.get("/gallery")
def index_gallery(request: Request):
    """Library — the connected Jellyfin music collection."""
    return templates.TemplateResponse(request, "index.html", {"active_tab": "gallery"})


@app.get("/discover")
def index_discover(request: Request):
    """Discover trending releases and search enabled music providers."""
    return templates.TemplateResponse(request, "index.html", {"active_tab": "discover"})


@app.get("/repair")
def index_repair(request: Request):
    """REPAIR tab — scan music folder for broken .lrc files and re-fetch from LRCLIB."""
    return templates.TemplateResponse(request, "index.html", {"active_tab": "repair"})


@app.get("/api/trending")
def api_trending(refresh: int = Query(0, ge=0, le=1)):
    try:
        data = get_trending(force=bool(refresh))
        data["songs"] = _annotate_tracks_on_server(data.get("songs") or [])
        return {"ok": True, **data}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/albums")
def api_albums(refresh: int = Query(0, ge=0, le=1), genre: str = Query("all")):
    try:
        data = get_albums(force=bool(refresh), genre=genre)
        data["albums"] = _annotate_albums_on_server(data.get("albums") or [])
        return {"ok": True, **data}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/album-download")
async def api_album_download(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    album = body or {}
    if not album.get("title") or not album.get("artist"):
        return JSONResponse({"ok": False, "error": "Album needs title and artist."}, status_code=400)
    job_id = uuid.uuid4().hex[:8]
    label = f"{album.get('title')} — {album.get('artist')}"
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "queued",
            "title": label,
            "query": label,
            "kind": "album",
            "file": "", "error": "", "started": datetime.now().isoformat(),
        }
    threading.Thread(target=_download_album_job, args=(job_id, album), daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/thumbnail")
def api_thumbnail(q: str = Query("", description="Song title + artist")):
    return lookup_thumbnail(q)


@app.get("/api/search")
def api_search(q: str = Query("", description="Search query"),
               kind: str = Query("all", description="song|album|all"),
               limit: int = Query(25, ge=1, le=50)):
    data = itunes_search(q, kind=kind, limit=limit)
    if kind in {"album", "all"}:
        data["albums"] = _annotate_albums_on_server(data.get("albums") or [])
    if kind in {"song", "all"}:
        data["songs"] = _annotate_tracks_on_server(data.get("songs") or [])
    return data


@app.get("/api/album/{collection_id}/tracks")
def api_album_tracks(collection_id: str,
                     title: str = Query("", description="Optional album title hint"),
                     artist: str = Query("", description="Optional album artist hint")):
    return itunes_album_tracks(collection_id, title_hint=title, artist_hint=artist)


@app.get("/api/song/{track_id}/album")
def api_song_album(track_id: int,
                   title: str = Query("", description="Optional title hint (helps when track_id is unknown to providers other than iTunes)"),
                   artist: str = Query("", description="Optional artist hint")):
    result = itunes_song_album(track_id, title=title, artist=artist)
    if result.get("ok"):
        album = result.get("album") or {}
        album["tracks"] = result.get("tracks") or []
        _annotate_albums_on_server([album])
        result["album"] = album
        result["tracks"] = album["tracks"]
    return result


@app.get("/api/song/search")
def api_song_search(q: str = Query(..., min_length=1),
                    limit: int = Query(15, ge=1, le=50)):
    """Song search across all enabled providers.

    Each result carries `album_id` (provider-native) so the UI can drive
    the existing "Add to cart" flow via /api/automator/album/{id}/tracks.
    """
    result = aggregate_song_search(q, limit=limit,
                                   providers=_resolve_discover_providers())
    result["songs"] = _annotate_tracks_on_server(result.get("songs") or [])
    return result


@app.get("/api/cookies")
def api_cookies():
    return {"ok": True, "browsers": SUPPORTED_BROWSERS, **cookie_status()}


# ---------------------------------------------------------------- REPAIR

@app.get("/api/repair/scan")
def api_repair_scan():
    """Scan the configured music folder for broken .lrc files.

    Returns:
        ok: bool
        music_folder: the configured path (or empty if not configured)
        entries: list of {mp3, lrc, mp3_relpath, lrc_relpath, status,
                           reason, title, artist, size_mp3, size_lrc,
                           mtime_lrc}
        counts: {healthy, broken, no_lrc, total}
    """
    p = automator.load_profile()
    folder = p.music_folder or ""
    if not folder:
        return {"ok": False, "error": "music folder not configured "
                                     "(Automator → Edit settings)",
                "music_folder": "", "entries": [], "counts": {}}
    try:
        entries = automator.scan_music_folder(folder)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "music_folder": folder, "entries": [], "counts": {}}
    counts = {"healthy": 0, "broken": 0, "no_lrc": 0, "total": 0}
    for e in entries:
        s = e.get("status")
        if s in counts:
            counts[s] += 1
        counts["total"] += 1
    # Sort: broken first, then no_lrc, then healthy.
    order = {"broken": 0, "no_lrc": 1, "healthy": 2, "error": 3}
    entries.sort(key=lambda e: (order.get(e.get("status"), 9),
                                e.get("mp3_relpath") or ""))
    return {"ok": True, "music_folder": folder,
            "entries": entries, "counts": counts}


@app.post("/api/repair/fix")
async def api_repair_fix(request: Request):
    """Re-fetch lyrics from LRCLIB for a batch of MP3 paths."""
    body = await request.json() if request.headers.get(
        "content-type", "").startswith("application/json") else {}
    paths = body.get("paths") if isinstance(body, dict) else None
    if not isinstance(paths, list) or not paths:
        return {"ok": False, "error": "paths must be a non-empty list",
                "results": []}
    results = []
    for p in paths:
        if not isinstance(p, str):
            continue
        try:
            r = automator.repair_lrc_file(p)
        except Exception as e:
            r = {"ok": False, "error": f"{type(e).__name__}: {e}",
                 "mp3": p}
        results.append(r)
    ok_count = sum(1 for r in results if r.get("ok"))
    return {"ok": True,
            "summary": {"total": len(results), "ok": ok_count,
                        "failed": len(results) - ok_count},
            "results": results}


@app.post("/api/repair/upload")
async def api_repair_upload(request: Request):
    """Manually write a .lrc file for an MP3. The user provides the
    text from one of three paths: paste, upload (file content read by
    the client), or manual_search (LRCLIB hit chosen in the modal).
    """
    body = await request.json() if request.headers.get(
        "content-type", "").startswith("application/json") else {}
    mp3_path = (body.get("mp3_path") or "").strip() if isinstance(body, dict) else ""
    lrc_text = (body.get("lrc_text") or "").strip() if isinstance(body, dict) else ""
    source = (body.get("source") or "manual").strip() if isinstance(body, dict) else "manual"
    if source not in ("paste", "upload", "manual_search", "auto_lrclib"):
        source = "manual"
    if not mp3_path:
        return {"ok": False, "error": "mp3_path required"}
    if not lrc_text:
        return {"ok": False, "error": "lrc_text required"}
    try:
        r = automator.write_lrc_text(mp3_path, lrc_text, source=source)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "write failed")}
    # Re-classify with the same heuristic the scan uses; should now be healthy.
    r["healthy"] = not automator._lrc_is_broken(lrc_text)
    return r


@app.post("/api/repair/manual_search")
async def api_repair_manual_search(request: Request):
    """Proxy to LRCLIB /api/search. Returns candidate hits for the user
    to pick from in the Manual lyrics modal."""
    body = await request.json() if request.headers.get(
        "content-type", "").startswith("application/json") else {}
    q = (body.get("q") or "").strip() if isinstance(body, dict) else ""
    try:
        limit = int(body.get("limit") or 20) if isinstance(body, dict) else 20
    except (TypeError, ValueError):
        limit = 20
    if not q:
        return {"ok": False, "error": "q required", "results": []}
    r = automator._lrclib_search(q, limit=limit)
    return r


@app.get("/api/repair/dedupe")
def api_repair_dedupe():
    """Scan the configured music folder for within-album duplicate songs."""
    p = automator.load_profile()
    folder = p.music_folder or ""
    if not folder:
        return {"ok": False, "error": "music folder not configured",
                "albums": [], "counts": {}}
    try:
        albums = automator.dedupe_album(folder)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "albums": [], "counts": {}}
    total_groups = sum(len(a.get("groups", [])) for a in albums)
    total_songs = sum(len(g.get("songs", []))
                      for a in albums for g in a.get("groups", []))
    return {
        "ok": True,
        "music_folder": folder,
        "send2trash_available": automator.send2trash_available(),
        "albums": albums,
        "counts": {"albums_with_dupes": len(albums),
                   "groups": total_groups,
                   "songs": total_songs},
    }


@app.post("/api/repair/dedupe/action")
async def api_repair_dedupe_action(request: Request):
    """Apply a dedupe action (delete | skip) over a batch of MP3 paths."""
    body = await request.json() if request.headers.get(
        "content-type", "").startswith("application/json") else {}
    action = (body.get("action") or "").strip() if isinstance(body, dict) else ""
    paths = body.get("paths") if isinstance(body, dict) else None
    if action not in ("delete", "skip"):
        return {"ok": False, "error": "action must be 'delete' or 'skip'"}
    if not isinstance(paths, list) or not paths:
        return {"ok": False, "error": "paths must be a non-empty list"}
    p = automator.load_profile()
    folder = p.music_folder or ""
    try:
        r = automator.dedupe_action(action, paths, folder)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return r


# ---------------------------------------------------------------- Renamer


@app.get("/api/repair/rename/scan")
def api_repair_rename_scan():
    """Walk the configured music folder and return:

    * ``misnamed`` — files whose basename doesn't match the
      ID3-derived ``"<track> - <title>.mp3"``.
    * ``collisions`` — album folders with duplicate / out-of-range
      track numbers, each with a proposed sequential renumbering.

    Pure read; safe to call repeatedly.
    """
    p = automator.load_profile()
    folder = p.music_folder or ""
    if not folder:
        return {"ok": False, "error": "music folder not configured",
                "music_folder": "", "misnamed": [], "collisions": [],
                "counts": {}}
    try:
        r = automator.scan_renames(folder)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "music_folder": folder, "misnamed": [], "collisions": [],
                "counts": {}}
    return r


@app.post("/api/repair/rename/apply")
async def api_repair_rename_apply(request: Request):
    """Apply a batch of rename actions.

    Body: ``{"actions": [{"mp3": "<abs path>",
                          "new_basename": "<basename.mp3>"}, ...]}``

    Path containment is enforced server-side; sibling ``.lrc`` files
    are renamed alongside their MP3. The user's UI is responsible for
    only sending approved actions.
    """
    body = await request.json() if request.headers.get(
        "content-type", "").startswith("application/json") else {}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be an object",
                "summary": {"renamed": 0, "failed": 0}, "results": []}
    actions = body.get("actions")
    if not isinstance(actions, list):
        return {"ok": False, "error": "actions must be a list",
                "summary": {"renamed": 0, "failed": 0}, "results": []}
    if not actions:
        return {"ok": False, "error": "actions list is empty",
                "summary": {"renamed": 0, "failed": 0}, "results": []}
    p = automator.load_profile()
    folder = p.music_folder or ""
    try:
        r = automator.apply_renames(actions, folder)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "summary": {"renamed": 0, "failed": 0}, "results": []}
    return r


# ---------------------------------------------------------------- discover settings


@app.get("/api/settings/discover")
def api_settings_discover_get():
    """Return provider choices for discovery and ordered album track lookup."""
    available = [p.name for p in enabled_providers()]
    with _discover_lock:
        requested = list(_discover_settings.get("providers") or [])
        track_requested = list(_discover_settings.get("track_providers") or [])
    is_all = (not requested) or ("all" in requested)
    is_all_tracks = (not track_requested) or ("all" in track_requested)
    return {
        "ok": True,
        "providers": ["all"] if is_all else requested,
        "available": available,
        "active": _resolve_discover_providers(),
        "track_providers": ["all"] if is_all_tracks else track_requested,
        "active_track_providers": _resolve_tracklist_providers(),
    }


@app.post("/api/settings/discover")
async def api_settings_discover_save(request: Request):
    body = await request.json() if request.headers.get(
        "content-type", "").startswith("application/json") else {}
    raw = body.get("providers") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return {"ok": False, "error": "providers must be a list"}
    track_raw = body.get("track_providers") if isinstance(body, dict) else None
    if track_raw is not None and not isinstance(track_raw, list):
        return {"ok": False, "error": "track_providers must be a list"}
    available = {p.name for p in enabled_providers()}
    cleaned: list[str] = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        e = entry.strip().lower()
        if not e or e in seen:
            continue
        seen.add(e)
        if e == "all" or e in available:
            cleaned.append(e)
    if "all" in cleaned or not cleaned:
        cleaned = ["all"]
    if track_raw is None:
        with _discover_lock:
            cleaned_track = list(_discover_settings.get("track_providers") or ["all"])
    else:
        cleaned_track = []
        seen_track = set()
        for entry in track_raw:
            if not isinstance(entry, str):
                continue
            name = entry.strip().lower()
            if not name or name in seen_track:
                continue
            seen_track.add(name)
            if name == "all" or name in available:
                cleaned_track.append(name)
        if "all" in cleaned_track or not cleaned_track:
            cleaned_track = ["all"]
    with _discover_lock:
        _discover_settings["providers"] = cleaned
        _discover_settings["track_providers"] = cleaned_track
        _save_discover_settings()
    return {
        "ok": True,
        "providers": cleaned,
        "active": _resolve_discover_providers(),
        "track_providers": cleaned_track,
        "active_track_providers": _resolve_tracklist_providers(),
    }


@app.post("/api/cookies")
async def api_cookies_save(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    browser = (body or {}).get("browser", "")
    result = export_cookies(browser)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/cookies/clean")
def api_cookies_clean():
    """Force a clean of the primary cookie file (rejoin split lines, strip
    NUL bytes, drop rows that can't be parsed). Idempotent."""
    from cookie_cleaner import clean_cookie_file, reset_cache
    reset_cache(COOKIE_FILE)
    res = clean_cookie_file(COOKIE_FILE)
    res["status_after"] = cookie_status()
    return res


@app.post("/api/download")
async def api_download(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    q = ((body or {}).get("query") or (body or {}).get("url") or "").strip()
    if not q:
        return JSONResponse({"ok": False, "error": "Give a song name or YouTube URL."}, status_code=400)
    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "queued", "title": q, "query": q,
            "file": "", "error": "", "started": datetime.now().isoformat(),
        }
    threading.Thread(target=_download_mp3_job, args=(job_id, q), daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/download/{job_id}")
def api_download_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "Unknown job."}, status_code=404)
    return {"ok": True, "job": job}


@app.get("/api/files")
def api_files():
    paths: dict[str, Path] = {}
    for root in (LEGACY_DOWNLOAD_DIR, DOWNLOAD_DIR):
        for path in root.rglob("*.mp3"):
            if path.is_file():
                # Prefer the app-data copy when a legacy filename collides.
                paths[path.relative_to(root).as_posix()] = path
    files = sorted(paths)
    return {"ok": True, "files": files}


# ---------------------------------------------------------------- jobs list

ACTIVE_STATUSES = {"queued", "searching", "loading tracks", "downloading"}


@app.get("/api/jobs")
def api_jobs():
    """List jobs: active first, then the last 8 completed/failed."""
    now = datetime.now()
    active = []
    recent = []
    with _jobs_lock:
        for jid, job in _jobs.items():
            entry = {"id": jid, **job}
            if job.get("status") in ACTIVE_STATUSES or job.get("cancel_requested"):
                active.append(entry)
            else:
                # only show recent (~last hour)
                started = job.get("started")
                if started:
                    try:
                        started_dt = datetime.fromisoformat(started)
                        if now - started_dt > timedelta(hours=1):
                            continue
                    except Exception:
                        pass
                recent.append(entry)
    # newest first
    active.sort(key=lambda j: j.get("started") or "", reverse=False)
    recent.sort(key=lambda j: j.get("started") or "", reverse=True)
    return {"ok": True, "active": active, "recent": recent[:8]}


@app.post("/api/jobs/{job_id}/cancel")
def api_jobs_cancel(job_id: str):
    """Request cancellation. The download loop will check this flag and abort
    between major steps. Active yt-dlp downloads cannot be interrupted cleanly,
    but the loop will stop moving to the next track / candidate."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return JSONResponse({"ok": False, "error": "Unknown job."}, status_code=404)
        if job.get("status") not in ACTIVE_STATUSES and not job.get("cancel_requested"):
            return {"ok": False, "error": "Job is already finished."}
        job["cancel_requested"] = True
        job["status"] = "cancelling"
    return {"ok": True, "job_id": job_id}


@app.get("/downloads/{name:path}")
def serve_download(name: str):
    # Allow album subfolders while keeping every resolved target inside the
    # app's download directory.
    if not name or any(part in {".", ".."} or part.startswith(".")
                       for part in Path(name).parts):
        raise HTTPException(status_code=400, detail="bad name")
    roots = (DOWNLOAD_DIR.resolve(), LEGACY_DOWNLOAD_DIR.resolve())
    target = next(((root / name).resolve() for root in roots
                   if (root / name).resolve().is_relative_to(root)
                   and (root / name).is_file()), None)
    if target is None:
        if any((root / name).resolve().is_relative_to(root) for root in roots):
            raise HTTPException(status_code=404, detail="not found")
        raise HTTPException(status_code=400, detail="bad name")
    return FileResponse(target, filename=target.name, media_type="audio/mpeg")


# ---------------------------------------------------------------- AUTOMATOR


@app.get("/automator")
def index_automator(request: Request):
    """AUTOMATOR tab \u2014 Jellyfin library automation."""
    return templates.TemplateResponse(request, "index.html", {"active_tab": "automator"})


def _automator_profile_view() -> dict:
    return automator.load_profile().public_view()


@app.get("/api/automator/profile")
def api_automator_profile():
    return {"ok": True, **_automator_profile_view()}


@app.post("/api/automator/profile")
async def api_automator_profile_save(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    body = body or {}
    p = automator.load_profile()
    p.server_name = str(body.get("server_name") or p.server_name).strip()
    p.jellyfin_url = str(body.get("jellyfin_url") or p.jellyfin_url).strip()
    # api_key: only update if user supplied something non-empty
    new_key = str(body.get("jellyfin_api_key") or "").strip()
    if new_key and not new_key.startswith("**"):
        p.jellyfin_api_key = new_key
    p.music_folder = str(body.get("music_folder") or p.music_folder).strip()
    p.whisper_model = str(body.get("whisper_model") or p.whisper_model).strip() or "turbo"
    p.whisper_device = str(body.get("whisper_device") or p.whisper_device).strip() or "auto"
    bk = body.get("backup") or {}
    p.backup.enabled = bool(bk.get("enabled", False))
    p.backup.mode = str(bk.get("mode") or p.backup.mode).strip() or "skip"
    p.backup.path = str(bk.get("path") or p.backup.path).strip()
    try:
        automator.save_profile(p)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"save failed: {e}"}, status_code=500)
    return {"ok": True, **_automator_profile_view()}


@app.post("/api/automator/test-connection")
def api_automator_test_connection():
    p = automator.load_profile()
    if not p.jellyfin_url:
        return {"ok": False, "error": "Jellyfin URL is empty \u2014 set it in Settings first."}
    if not p.jellyfin_api_key:
        return {"ok": False, "error": "API key is empty \u2014 set it in Settings first."}
    client = automator.JellyfinClient(p.jellyfin_url, p.jellyfin_api_key)
    return client.test_connection()


@app.get("/api/automator/library/recent")
def api_automator_library_recent(limit: int = Query(24, ge=1, le=50)):
    p = automator.load_profile()
    if not p.is_configured():
        return {"ok": False, "error": "profile not configured", "albums": []}
    client = automator.JellyfinClient(p.jellyfin_url, p.jellyfin_api_key)
    albums = client.get_recent_albums(limit=limit)
    return {"ok": True, "albums": albums}


@app.get("/api/automator/library/albums")
def api_automator_library_albums(limit: int = Query(200, ge=1, le=5000),
                                 start_index: int = Query(0, ge=0),
                                 sort_by: str = Query("SortName")):
    """Gallery: paginated list of ALL albums in Jellyfin (sorted by SortName by
    default). Returns {ok, albums, total, start_index, limit}.

    `total` is Jellyfin's TotalRecordCount — the frontend uses it to know when
    to stop paginating.
    """
    p = automator.load_profile()
    if not p.is_configured():
        return {"ok": False, "error": "profile not configured", "albums": [], "total": 0}
    client = automator.JellyfinClient(p.jellyfin_url, p.jellyfin_api_key)
    albums, total = client.get_all_albums(limit=limit, start_index=start_index,
                                           sort_by=sort_by)
    return {"ok": True, "albums": albums, "total": total,
            "start_index": start_index, "limit": limit}


@app.get("/api/automator/library/albums/{album_id}/tracks")
def api_automator_library_album_tracks(album_id: str):
    """Gallery: fetch the tracks of one Jellyfin album (cart-shaped)."""
    p = automator.load_profile()
    if not p.is_configured():
        return {"ok": False, "error": "profile not configured", "tracks": []}
    client = automator.JellyfinClient(p.jellyfin_url, p.jellyfin_api_key)
    tracks = client.get_album_tracks(album_id)
    return {"ok": True, "tracks": tracks, "album_id": album_id}


@app.get("/api/automator/search/albums")
def api_automator_search_albums(q: str = Query("", description="Search query"),
                                 limit: int = Query(25, ge=1, le=50)):
    res = itunes_search(q, kind="album", limit=limit)
    albums = res.get("albums") or []
    song_album = _itunes_album_for_song_title(q)
    if song_album:
        duplicate = next((album for album in albums
                          if (str(album.get("id")) == str(song_album["id"]))
                          or (norm(album.get("title", "")) == norm(song_album["title"])
                              and norm(album.get("artist", "")) == norm(song_album["artist"]))), None)
        if duplicate:
            duplicate["matched_song"] = song_album["matched_song"]
        else:
            albums.insert(0, song_album)
    p = automator.load_profile()
    if p.is_configured():
        client = automator.JellyfinClient(p.jellyfin_url, p.jellyfin_api_key)
        try:
            albums = automator.mark_already_on_server(albums, client)
        except Exception:
            for a in albums:
                a["alreadyOnServer"] = False
    else:
        for a in albums:
            a["alreadyOnServer"] = False
    res["albums"] = albums
    res["ok"] = bool(res.get("ok") or albums)
    return res


@app.get("/api/automator/album/{collection_id}/tracks")
def api_automator_album_tracks(collection_id: str,
                                title: str = Query("", description="Hint: album title, used to fall back to other providers when iTunes is down"),
                                artist: str = Query("", description="Hint: album artist, used to fall back to other providers when iTunes is down")):
    res = itunes_album_tracks(collection_id, title_hint=title, artist_hint=artist)
    if not res.get("ok"):
        return res
    album = res.get("album") or {}
    album["tracks"] = res.get("tracks") or []
    _annotate_albums_on_server([album])
    res["album"] = album
    res["tracks"] = album["tracks"]
    return res


@app.post("/api/automator/cart/process")
async def api_automator_cart_process(request: Request):
    """Spawn one automator_job per submitted album. Body: {albums:[...], backup:bool, track_lyrics:{track_id:{source,lrc_text,language}}}"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    body = body or {}
    albums_in = body.get("albums") or []
    backup = bool(body.get("backup", False))
    track_lyrics = body.get("track_lyrics") or {}
    if not albums_in:
        return JSONResponse({"ok": False, "error": "No albums in cart."}, status_code=400)
    p = automator.load_profile()
    if not p.is_configured():
        return JSONResponse({"ok": False, "error": "AUTOMATOR not configured \u2014 open Settings."}, status_code=400)

    job_ids = []
    for album in albums_in:
        if not album.get("id") or not album.get("title") or not album.get("artist"):
            continue
        if not album.get("tracks"):
            continue
        job_id = uuid.uuid4().hex[:8]
        with _jobs_lock:
            _jobs[job_id] = {
                "status": "queued",
                "title": f"{album.get('title')} \u2014 {album.get('artist')}",
                "query": f"{album.get('title')} \u2014 {album.get('artist')}",
                "kind": "automator",
                "album_id": album.get("id"),
                "album_title": album.get("title"),
                "album_artist": album.get("artist"),
                "backup": backup,
                "whisper_model": p.whisper_model,
                "whisper_device": p.whisper_device,
                "music_folder": p.music_folder,
                # A2: stash full album + per-track snapshot so retry-failed can respawn.
                "album_payload": dict(album),
                "album_files_snapshot": [],
                "track_lyrics": track_lyrics if isinstance(track_lyrics, dict) else {},
                "file": "", "error": "",
                "files": [],
                "progress": {"done": 0, "total": len(album.get("tracks") or []), "current": ""},
                "started": datetime.now().isoformat(),
            }
        threading.Thread(target=automator.automator_album_job,
                         args=(job_id, album, backup, _jobs, _jobs_lock),
                         daemon=True).start()
        job_ids.append(job_id)
    if not job_ids:
        return JSONResponse({"ok": False, "error": "No valid albums to process."}, status_code=400)
    return {"ok": True, "job_ids": job_ids}


@app.post("/api/automator/youtube/single")
async def api_automator_youtube_single(request: Request):
    """R4: turn a YouTube URL (or video ID or query) into a synthetic single-track
    album for the cart. Body: {url: "..."}.

    Returns: {ok, album:{id,title,artist,kind:"single",tracks:[...],...}}
    Errors: {ok:false, error:"..."} on 400 with a user-readable message.
    """
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    body = body or {}
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "URL is required."}, status_code=400)
    try:
        album = automator.resolve_youtube_single(url)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"},
            status_code=500,
        )
    return {"ok": True, "album": album}


@app.post("/api/automator/lookup-lyrics")
async def api_automator_lookup_lyrics(request: Request):
    """Start a background lookup of synced lyrics for each track via LRCLIB.

    Body: {tracks:[{id,title,artist,album,duration_ms}, ...]}
    Returns immediately with: {ok, lookup_id, total}

    The client then polls /api/automator/lookup-lyrics/status/<lookup_id> to
    see progress and collect final results.
    """
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    body = body or {}
    tracks = body.get("tracks") or []
    if not tracks:
        return {"ok": False, "error": "No tracks provided."}
    if len(tracks) > 100:
        return {"ok": False, "error": "Too many tracks (limit 100)."}

    lookup_id = uuid.uuid4().hex[:8]
    now_iso = datetime.now().isoformat(timespec="seconds")
    with _jobs_lock:
        _jobs[lookup_id] = {
            "kind": "lyrics-lookup",
            "status": "queued",
            "done": 0,
            "total": len(tracks),
            "current": "",
            "results": [None] * len(tracks),
            "error": "",
            "track_order": [str(t.get("id") or "") for t in tracks],
            "started": now_iso,
            "finished": "",
            "title": f"Looking up lyrics for {len(tracks)} track(s)",
            "progress": {"done": 0, "total": len(tracks), "current": ""},
        }
    import threading as _t
    _t.Thread(target=automator.lyrics_lookup_job_worker,
              args=(lookup_id, tracks, _jobs, _jobs_lock),
              daemon=True).start()
    return {"ok": True, "lookup_id": lookup_id, "total": len(tracks)}


@app.get("/api/automator/lookup-lyrics/status/{lookup_id}")
def api_automator_lookup_lyrics_status(lookup_id: str):
    """Poll endpoint for an in-flight or recently-finished lyrics lookup job.

    While running: {ok, status:"running", done, total, current}
    When done:     {ok, status:"done", done, total, current:"",
                    results:[ordered list of lookup results]}
    On error:      {ok, status:"error", error:"..."}
    """
    # Opportunistic cleanup of stale lookup state.
    try:
        automator.cleanup_lookup_state(_jobs, _jobs_lock)
    except Exception:
        pass
    with _jobs_lock:
        st = _jobs.get(lookup_id)
    if not st or st.get("kind") != "lyrics-lookup":
        return JSONResponse({"ok": False, "error": "Unknown lookup id."},
                            status_code=404)
    status = st.get("status")
    payload = {
        "ok": True,
        "lookup_id": lookup_id,
        "status": status,
        "done": st.get("done", 0),
        "total": st.get("total", 0),
        "current": st.get("current", "") or "",
        "error": st.get("error", "") or "",
    }
    if status == "done":
        results = list(st.get("results") or [])
        payload["results"] = [r for r in results if r is not None]
    return payload


@app.get("/api/automator/jobs")
def api_automator_jobs():
    """Same shape as /api/jobs but only kind='automator'."""
    now = datetime.now()
    active, recent = [], []
    with _jobs_lock:
        for jid, job in _jobs.items():
            if job.get("kind") != "automator":
                continue
            entry = {"id": jid, **job}
            st = job.get("status")
            if st in ACTIVE_STATUSES or st in automator.ACTIVE_AUTOMATOR_STATUSES or job.get("cancel_requested"):
                active.append(entry)
            else:
                started = job.get("started")
                if started:
                    try:
                        started_dt = datetime.fromisoformat(started)
                        if now - started_dt > timedelta(hours=2):
                            continue
                    except Exception:
                        pass
                recent.append(entry)
    active.sort(key=lambda j: j.get("started") or "")
    recent.sort(key=lambda j: j.get("started") or "", reverse=True)
    return {"ok": True, "active": active, "recent": recent[:12]}


@app.get("/api/automator/jobs/{job_id}")
def api_automator_job_get(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get("kind") != "automator":
        return JSONResponse({"ok": False, "error": "Unknown automator job."}, status_code=404)
    return {"ok": True, "job": {"id": job_id, **job}}


@app.post("/api/automator/jobs/{job_id}/retry-failed")
def api_automator_job_retry_failed(job_id: str):
    """A2: spawn a new job that retries only the tracks that failed in the
    original job. Skips already-done / already-skipped tracks.
    Returns {ok, retry_job_id, failed_count}."""
    with _jobs_lock:
        original = _jobs.get(job_id)
        if not original or original.get("kind") != "automator":
            return JSONResponse({"ok": False, "error": "Unknown automator job."}, status_code=404)
        album_payload = original.get("album_payload")
        backup = bool(original.get("backup", False))
        if not album_payload:
            return JSONResponse({"ok": False, "error": "Original job payload not retained; cannot retry."}, status_code=400)
        # Identify which track indices failed in the original job.
        files = original.get("files") or []
        failed_indices = [i for i, f in enumerate(files) if f.get("status") == "failed"]
        if not failed_indices:
            return {"ok": False, "error": "No failed tracks to retry.", "failed_count": 0}
        # Build the per-track "files" snapshot for the new job, copying the
        # statuses so only_statuses=['failed'] keeps them. For non-failed
        # tracks we mark them as 'skipped' immediately so progress counts right.
        tracks = album_payload.get("tracks") or []
        new_files = []
        for i, _t in enumerate(tracks):
            if i in failed_indices:
                # Reset failed tracks back to pending so the worker re-runs them.
                new_files.append({"status": "pending", "error": ""})
            else:
                # Carry over the original terminal status (done / skipped / etc.)
                orig = files[i] if i < len(files) else {}
                new_files.append({
                    "status": orig.get("status", "skipped"),
                    "error": orig.get("error", ""),
                    "mp3_path": orig.get("mp3_path", ""),
                    "lyrics_source": orig.get("lyrics_source", ""),
                    "log": orig.get("log", []),
                })

    retry_id = uuid.uuid4().hex[:8]
    now_iso = datetime.now().isoformat()
    with _jobs_lock:
        _jobs[retry_id] = {
            "status": "queued",
            "title": f"{album_payload.get('title')} — {album_payload.get('artist')} (retry)",
            "query": f"{album_payload.get('title')} — {album_payload.get('artist')}",
            "kind": "automator",
            "album_id": album_payload.get("id"),
            "album_title": album_payload.get("title"),
            "album_artist": album_payload.get("artist"),
            "album_payload": dict(album_payload),
            "album_files_snapshot": list(new_files),
            "backup": backup,
            "whisper_model": original.get("whisper_model", "small.en"),
            "whisper_device": original.get("whisper_device", "auto"),
            "music_folder": original.get("music_folder", ""),
            "track_lyrics": original.get("track_lyrics", {}) or {},
            "file": "", "error": "",
            "files": list(new_files),
            "progress": {"done": 0, "total": len(tracks), "current": ""},
            "started": now_iso,
            "retried_from": job_id,
        }
    threading.Thread(target=automator.automator_album_job,
                     args=(retry_id, album_payload, backup, _jobs, _jobs_lock, ["failed"]),
                     daemon=True).start()
    return {"ok": True, "retry_job_id": retry_id, "failed_count": len(failed_indices)}


@app.post("/api/automator/jobs/{job_id}/cancel")
def api_automator_job_cancel(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.get("kind") != "automator":
            return JSONResponse({"ok": False, "error": "Unknown automator job."}, status_code=404)
        active = ACTIVE_STATUSES | automator.ACTIVE_AUTOMATOR_STATUSES
        if job.get("status") not in active and not job.get("cancel_requested"):
            return {"ok": False, "error": "Job is already finished."}
        job["cancel_requested"] = True
        job["status"] = "cancelling"
    return {"ok": True, "job_id": job_id}



# ---------------------------------------------------------------- startup

if __name__ == "__main__":
    import uvicorn
    print(f"Cookie file: {COOKIE_FILE} exists={COOKIE_FILE.exists()}")
    print(f"Downloads : {DOWNLOAD_DIR}")
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=False, log_level="info")
