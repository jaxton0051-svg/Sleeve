"""providers.py — Multi-source music database aggregator.

Five providers, one protocol, one aggregator. See app.py for the routes
that wire this in. Designed to never block the caller longer than
~timeout_per_provider * (1 + parallel_wait).

Public surface used by app.py:
    from providers import (
        AlbumSummary, Track, AlbumPayload,
        aggregate_search, aggregate_album_tracks,
        enabled_providers,
    )

Public surface for diagnostics:
    PROVIDER_ORDER     — canonical priority for tie-breaking
    PROVIDER_TIE_TOLERANCE — ± tracks that count as a "tie"
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Optional, Iterable, Callable

import requests


# ---------------------------------------------------------------- constants

ITUNES_BASE = "https://itunes.apple.com"
MB_BASE = "https://musicbrainz.org/ws/2"
DEEZER_BASE = "https://api.deezer.com"
ADB_BASE = "https://www.theaudiodb.com/api/v1/json"
SPOTIFY_BASE = "https://api.spotify.com/v1"
SPOTIFY_AUTH = "https://accounts.spotify.com/api/token"

ADB_KEY = "3"  # working public test key (key "2" returns 404)

# Required by MusicBrainz — descriptive UA including app + contact.
MB_USER_AGENT = (
    "AAABestMusic/1.0 "
    "(+https://github.com/Jaxton/AAA-Best-Music-App; "
    "contact: jaxton@example.local)"
)

# Providers in priority order for tie-breaking.
PROVIDER_ORDER = ("itunes", "musicbrainz", "deezer", "audiodb", "spotify")
PROVIDER_PRIORITY = {n: i for i, n in enumerate(PROVIDER_ORDER)}

# Within ±N tracks the aggregator treats two providers as tied; then
# priority (above) decides.
PROVIDER_TIE_TOLERANCE = 2

DEFAULT_TIMEOUT = 8.0
DEFAULT_PARALLEL = 4


# ---------------------------------------------------------------- dataclasses


@dataclass
class AlbumSummary:
    """A single album result, as returned by a provider's search."""
    source: str            # "itunes" | "musicbrainz" | "deezer" | ...
    source_id: str         # provider-native id (string form)
    title: str
    artist: str
    artwork: str = ""
    track_count: Optional[int] = None
    release_date: str = ""
    genre: str = ""
    source_url: str = ""

    def norm_key(self) -> tuple[str, str]:
        return (norm(s) for s in (self.title, self.artist))


@dataclass
class Track:
    """A single track. Source attribution is required."""
    source: str
    source_id: str
    title: str
    artist: str
    duration_ms: int = 0
    track_number: int = 0
    disc_number: int = 1
    source_url: str = ""

    def norm_key(self) -> tuple[int, int, str]:
        return (self.disc_number, self.track_number, norm(self.title))


@dataclass
class AlbumPayload:
    """A complete album: metadata + ordered tracklist."""
    source: str                  # provider that supplied it (tracklist winner)
    source_id: str
    album: AlbumSummary
    tracks: list[Track] = field(default_factory=list)
    source_url: str = ""

    @property
    def track_count(self) -> int:
        return len(self.tracks)


def norm(s: str) -> str:
    """Normalize a title/artist for fuzzy dedup across providers.

    MusicBrainz uses U+2019 (right single quote), iTunes/AudioDB usually
    use U+0027 (ASCII apostrophe); AudioDB and iTunes sometimes use
    em-dashes, MB uses en-dashes, etc. We canonicalize the common
    variants so the same album matches across sources.
    """
    s = (s or "").strip().lower()
    # canonicalize punctuation variants to ASCII
    s = (s.replace("\u2019", "'")    # right single quote
           .replace("\u2018", "'")    # left single quote
           .replace("\u02bc", "'")    # modifier letter apostrophe
           .replace("\u2032", "'")    # prime
           .replace("\u201c", '"')    # left double quote
           .replace("\u201d", '"')    # right double quote
           .replace("\u2013", "-")    # en dash
           .replace("\u2014", "-")    # em dash
           .replace("\u2212", "-")    # minus sign
           .replace("\u00a0", " ")    # non-breaking space
           .replace("\u202f", " ")    # narrow no-break space
           .replace("\u2009", " ")    # thin space
           .replace("\u3000", " "))   # ideographic space
    # drop parenthetical edition tags like "(US Reissue)" or "[Deluxe]"
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)
    # collapse runs of whitespace/hyphens/underscores
    s = re.sub(r"[\s\-_]+", " ", s).strip()
    return s


# ---------------------------------------------------------------- provider protocol (duck-typed)


class AlbumProvider:
    """Base for every concrete provider. Subclasses set `name` and
    override `search_albums` / `get_album_tracks`. Use `enabled` to gate
    providers that need credentials or are disabled by config.
    """
    name: str = "base"
    enabled: bool = True

    def search_albums(self, query: str, limit: int = 25,
                      timeout: float = DEFAULT_TIMEOUT) -> list[AlbumSummary]:
        raise NotImplementedError

    def search_songs(self, query: str, limit: int = 25,
                     timeout: float = DEFAULT_TIMEOUT) -> list[Track]:
        """Optional — providers that support song search override this."""
        return []

    def get_album_tracks(self, *, collection_id: Optional[str] = None,
                         title: str = "", artist: str = "",
                         timeout: float = DEFAULT_TIMEOUT) -> Optional[AlbumPayload]:
        raise NotImplementedError


# ---------------------------------------------------------------- iTunes

class ITunesProvider(AlbumProvider):
    name = "itunes"
    enabled = True

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict, timeout: float) -> Optional[dict]:
        try:
            r = self.s.get(f"{ITUNES_BASE}{path}", params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    @staticmethod
    def _bump_artwork(url: str, size: int = 300) -> str:
        if not url:
            return ""
        return re.sub(r"\d{2,4}x\d{2,4}bb", f"{size}x{size}bb", url)

    def search_albums(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._get("/search",
                         {"term": query, "media": "music",
                          "entity": "album", "limit": max(1, min(limit, 50))},
                         timeout)
        out: list[AlbumSummary] = []
        for item in (data or {}).get("results") or []:
            cid = item.get("collectionId")
            if not cid:
                continue
            title = item.get("collectionName") or item.get("collectionCensoredName") or ""
            artist = item.get("artistName") or ""
            out.append(AlbumSummary(
                source="itunes", source_id=str(cid),
                title=title, artist=artist,
                artwork=self._bump_artwork(item.get("artworkUrl100", ""), 300),
                track_count=item.get("trackCount") or 0,
                release_date=(item.get("releaseDate") or "")[:10],
                genre=item.get("primaryGenreName", "") or "",
                source_url=f"https://music.apple.com/us/album/{slugify(title)}/{cid}",
            ))
        return out

    def search_songs(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._get("/search",
                         {"term": query, "media": "music",
                          "entity": "song", "limit": max(1, min(limit, 50))},
                         timeout)
        out: list[Track] = []
        for item in (data or {}).get("results") or []:
            out.append(Track(
                source="itunes", source_id=str(item.get("trackId") or ""),
                title=item.get("trackName") or "",
                artist=item.get("artistName") or "",
                duration_ms=item.get("trackTimeMillis") or 0,
                track_number=item.get("trackNumber") or 0,
                disc_number=item.get("discNumber") or 1,
                source_url=f"https://music.apple.com/us/album/{slugify(item.get('collectionName') or '')}/{item.get('collectionId') or ''}",
            ))
        return out

    def get_album_tracks(self, *, collection_id=None, title="", artist="",
                         timeout=DEFAULT_TIMEOUT):
        if not collection_id:
            return None
        # Try full lookup first; if it 5xxs/times-out, try the bare lookup
        # which often still works for metadata.
        full = self._get("/lookup",
                         {"id": int(collection_id), "entity": "song"},
                         timeout)
        results = (full or {}).get("results") or []
        meta = results[0] if results else None
        if not meta:
            bare = self._get("/lookup", {"id": int(collection_id)}, timeout)
            results = (bare or {}).get("results") or []
            meta = results[0] if results else None
        if not meta:
            return None

        tracks: list[Track] = []
        for item in results:
            if item.get("wrapperType") != "track":
                continue
            tn = item.get("trackId")
            tracks.append(Track(
                source="itunes", source_id=str(tn or ""),
                title=item.get("trackName") or "",
                artist=item.get("artistName") or "",
                duration_ms=item.get("trackTimeMillis") or 0,
                track_number=item.get("trackNumber") or 0,
                disc_number=item.get("discNumber") or 1,
                source_url=f"https://music.apple.com/us/song/{slugify(item.get('trackName') or '')}/{tn}" if tn else "",
            ))
        if not tracks and not meta:
            return None

        cid = meta.get("collectionId") or collection_id
        a_title = meta.get("collectionName") or title
        a_artist = meta.get("artistName") or artist
        album = AlbumSummary(
            source="itunes", source_id=str(cid),
            title=a_title or "", artist=a_artist or "",
            artwork=self._bump_artwork(meta.get("artworkUrl100", ""), 300),
            track_count=meta.get("trackCount") or 0,
            release_date=(meta.get("releaseDate") or "")[:10],
            genre=meta.get("primaryGenreName", "") or "",
            source_url=f"https://music.apple.com/us/album/{slugify(a_title or '')}/{cid}",
        )
        return AlbumPayload(source="itunes", source_id=str(cid),
                            album=album, tracks=tracks,
                            source_url=album.source_url)


# ---------------------------------------------------------------- MusicBrainz

class MusicBrainzProvider(AlbumProvider):
    name = "musicbrainz"
    enabled = True

    def __init__(self) -> None:
        # Process-wide gate: 1 req/sec max.
        self._lock = threading.Lock()
        self._last_call_ts = 0.0
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": MB_USER_AGENT,
            "Accept": "application/json",
        })

    def _throttled_get(self, path: str, params: dict,
                       timeout: float) -> Optional[dict]:
        with self._lock:
            wait = 1.0 - (time.monotonic() - self._last_call_ts)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(f"{MB_BASE}{path}", params=params, timeout=timeout)
                self._last_call_ts = time.monotonic()
                if r.status_code == 503 or r.status_code == 429:
                    return None
                r.raise_for_status()
                return r.json()
            except Exception:
                self._last_call_ts = time.monotonic()
                return None

    def search_albums(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._throttled_get(
            "/release/",
            {"query": query, "fmt": "json", "limit": max(1, min(limit, 50))},
            timeout,
        )
        out: list[AlbumSummary] = []
        for item in (data or {}).get("releases") or []:
            rid = item.get("id")
            if not rid:
                continue
            title = item.get("title") or ""
            artist = ((item.get("artist-credit") or [{}])[0].get("name")
                      or item.get("artist") or {}).get("name", "")
            # fall back to top-level name field
            if not artist and "artist-credit" not in item:
                artist = item.get("artist-name") or ""
            # date is "YYYY", "YYYY-MM", or "YYYY-MM-DD"
            date = item.get("date") or ""
            cover = ""
            try:
                # /ws/2 doesn't return artwork; we leave the cover blank
                # (the aggregator will pick the provider with the best
                # artwork as the album-level source_url metadata).
                cover = ""
            except Exception:
                pass
            out.append(AlbumSummary(
                source="musicbrainz", source_id=str(rid),
                title=title, artist=artist,
                artwork=cover,
                track_count=item.get("track-count") or item.get("media-count"),
                release_date=date,
                genre="",
                source_url=f"https://musicbrainz.org/release/{rid}",
            ))
        return out

    def search_songs(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._throttled_get(
            "/recording/",
            {"query": query, "fmt": "json", "limit": max(1, min(limit, 50))},
            timeout,
        )
        out: list[Track] = []
        for item in (data or {}).get("recordings") or []:
            dur_ms = int((item.get("length") or 0))  # MB returns ms already
            artist = ((item.get("artist-credit") or [{}])[0].get("name")
                      or item.get("artist") or {}).get("name", "")
            releases = item.get("releases") or []
            first_release = releases[0] if releases else {}
            out.append(Track(
                source="musicbrainz",
                source_id=str(item.get("id") or ""),
                title=item.get("title") or "",
                artist=artist,
                duration_ms=dur_ms,
                track_number=0,
                disc_number=1,
                source_url=f"https://musicbrainz.org/recording/{item.get('id')}",
            ))
        return out

    def get_album_tracks(self, *, collection_id=None, title="", artist="",
                         timeout=DEFAULT_TIMEOUT):
        # Prefer exact release id when caller has one.
        if collection_id:
            return self._by_release_id(collection_id, timeout)
        if not (title and artist):
            return None
        # Search by name and pick the best match.
        data = self._throttled_get(
            "/release/",
            {"query": f'release:"{title}" AND artist:"{artist}"',
             "fmt": "json", "limit": 5},
            timeout,
        )
        releases = (data or {}).get("releases") or []
        if not releases:
            return None
        target_title = norm(title)
        target_artist = norm(artist)
        best = None
        for r in releases:
            if norm(r.get("title") or "") == target_title:
                # Compare artist name
                rc = (r.get("artist-credit") or [{}])[0].get("name", "")
                if norm(rc) == target_artist:
                    best = r
                    break
        if not best:
            best = releases[0]
        return self._by_release_id(str(best.get("id")), timeout)

    def _by_release_id(self, rid: str, timeout: float) -> Optional[AlbumPayload]:
        data = self._throttled_get(
            f"/release/{rid}",
            {"inc": "recordings+artist-credits", "fmt": "json"},
            timeout,
        )
        if not data:
            return None
        title = data.get("title") or ""
        ac = (data.get("artist-credit") or [{}])[0].get("name") or ""
        date = data.get("date") or ""
        tracks: list[Track] = []
        for media in data.get("media") or []:
            disc = int(media.get("position") or 1)
            for t in media.get("tracks") or []:
                tn = int(t.get("position") or 0)
                dur_ms = int(t.get("length") or 0)
                tracks.append(Track(
                    source="musicbrainz", source_id=str(t.get("id") or ""),
                    title=t.get("title") or "",
                    artist=ac,
                    duration_ms=dur_ms,
                    track_number=tn,
                    disc_number=disc,
                    source_url=f"https://musicbrainz.org/recording/{t.get('id')}" if t.get("id") else "",
                ))
        album = AlbumSummary(
            source="musicbrainz", source_id=str(rid),
            title=title, artist=ac,
            artwork="",
            track_count=len(tracks),
            release_date=date,
            genre="",
            source_url=f"https://musicbrainz.org/release/{rid}",
        )
        return AlbumPayload(source="musicbrainz", source_id=str(rid),
                            album=album, tracks=tracks,
                            source_url=album.source_url)


# ---------------------------------------------------------------- Deezer

class DeezerProvider(AlbumProvider):
    name = "deezer"
    enabled = True

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })

    def _get(self, path: str, params: Optional[dict], timeout: float) -> Optional[dict]:
        try:
            r = self.s.get(f"{DEEZER_BASE}{path}", params=params or {}, timeout=timeout)
            if r.status_code != 200:
                return None
            d = r.json()
            # Deezer returns {"error": {...}} on misses
            if isinstance(d, dict) and "error" in d:
                return None
            return d
        except Exception:
            return None

    @staticmethod
    def _cover(url: str, size: str = "big") -> str:
        if not url:
            return ""
        # Deezer serves /image.jpg variants: cover, cover_small, ... but
        # always 1200x1200 on the no-suffix URL; we just keep it.
        return url

    def search_albums(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._get("/search/album", {"q": query, "limit": max(1, min(limit, 50))}, timeout)
        out: list[AlbumSummary] = []
        for item in (data or {}).get("data") or []:
            aid = str(item.get("id") or "")
            if not aid:
                continue
            artist = ((item.get("artist") or {}).get("name")) or ""
            out.append(AlbumSummary(
                source="deezer", source_id=aid,
                title=item.get("title") or "",
                artist=artist,
                artwork=self._cover(((item.get("cover_xl") or item.get("cover_big") or item.get("cover") or ""))),
                track_count=(item.get("nb_tracks") or 0),
                release_date=item.get("release_date") or "",
                genre="",
                source_url=(item.get("link") or f"https://www.deezer.com/album/{aid}"),
            ))
        return out

    def search_songs(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._get("/search", {"q": query, "limit": max(1, min(limit, 50))}, timeout)
        out: list[Track] = []
        for item in (data or {}).get("data") or []:
            out.append(Track(
                source="deezer",
                source_id=str(item.get("id") or ""),
                title=item.get("title") or "",
                artist=((item.get("artist") or {}).get("name") or ""),
                duration_ms=int(item.get("duration") or 0) * 1000,
                track_number=item.get("track_position") or 0,
                disc_number=item.get("disk_number") or 1,
                source_url=(item.get("link") or ""),
            ))
        return out

    def get_album_tracks(self, *, collection_id=None, title="", artist="",
                         timeout=DEFAULT_TIMEOUT):
        if collection_id:
            return self._by_id(collection_id, timeout)
        if not (title and artist):
            return None
        data = self._get("/search/album", {"q": f"{artist} {title}", "limit": 5}, timeout)
        for item in (data or {}).get("data") or []:
            target_title = norm(title)
            target_artist = norm(artist)
            item_title = norm(item.get("title") or "")
            item_artist = norm(((item.get("artist") or {}).get("name") or ""))
            if item_title == target_title and item_artist == target_artist:
                return self._by_id(str(item.get("id")), timeout)
        # First hit as fallback
        items = (data or {}).get("data") or []
        if items:
            return self._by_id(str(items[0].get("id")), timeout)
        return None

    def _by_id(self, aid: str, timeout: float) -> Optional[AlbumPayload]:
        data = self._get(f"/album/{aid}", None, timeout)
        if not data:
            return None
        tracks: list[Track] = []
        for t in (data.get("tracks", {}).get("data") or []):
            tracks.append(Track(
                source="deezer",
                source_id=str(t.get("id") or ""),
                title=t.get("title") or "",
                artist=((t.get("artist") or {}).get("name") or ""),
                duration_ms=int(t.get("duration") or 0) * 1000,
                track_number=0,    # filled from /album/tracks if we wanted to paginate; keep 0
                disc_number=1,
                source_url=(t.get("link") or ""),
            ))
        # Deezer's /album/{id} only returns first 40-ish tracks. For full
        # coverage, paginate the tracks endpoint.
        # https://api.deezer.com/album/{id}/tracks returns all.
        tracks_data = self._get(f"/album/{aid}/tracks", {"limit": 200}, timeout) or {}
        full_tracks = tracks_data.get("data") or []
        if full_tracks:
            tracks = []
            for t in full_tracks:
                tracks.append(Track(
                    source="deezer",
                    source_id=str(t.get("id") or ""),
                    title=t.get("title") or "",
                    artist=((t.get("artist") or {}).get("name") or ""),
                    duration_ms=int(t.get("duration") or 0) * 1000,
                    track_number=0,
                    disc_number=1,
                    source_url=(t.get("link") or ""),
                ))

        artist = ((data.get("artist") or {}).get("name") or "")
        album = AlbumSummary(
            source="deezer", source_id=str(aid),
            title=data.get("title") or "",
            artist=artist,
            artwork=self._cover(data.get("cover_xl") or data.get("cover_big") or data.get("cover") or ""),
            track_count=data.get("nb_tracks") or len(tracks),
            release_date=data.get("release_date") or "",
            genre="",
            source_url=(data.get("link") or f"https://www.deezer.com/album/{aid}"),
        )
        return AlbumPayload(source="deezer", source_id=str(aid),
                            album=album, tracks=tracks,
                            source_url=album.source_url)


# ---------------------------------------------------------------- TheAudioDB

class AudioDBProvider(AlbumProvider):
    name = "audiodb"
    enabled = True

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict, timeout: float) -> Optional[dict]:
        try:
            r = self.s.get(f"{ADB_BASE}/{ADB_KEY}/{path}", params=params or {}, timeout=timeout)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None

    def search_albums(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._get("searchalbum.php", {"s": query}, timeout)
        rows = (data or {}).get("album") or []
        out: list[AlbumSummary] = []
        for i, row in enumerate(rows[:limit], 1):
            aid = str(row.get("idAlbum") or "")
            if not aid:
                continue
            out.append(AlbumSummary(
                source="audiodb", source_id=aid,
                title=(row.get("strAlbum") or "").strip(),
                artist=(row.get("strArtist") or query).strip(),
                artwork=(row.get("strAlbumThumbHQ") or row.get("strAlbumThumb") or ""),
                track_count=0,
                release_date=str(row.get("intYearReleased") or ""),
                genre=(row.get("strGenre") or ""),
                source_url=f"https://www.theaudiodb.com/album/{aid}",
            ))
        return out

    def get_album_tracks(self, *, collection_id=None, title="", artist="",
                         timeout=DEFAULT_TIMEOUT):
        if not (title or artist or collection_id):
            return None
        # collection_id here = adb album id (we always pass via title/artist
        # unless the upstream caller supplies one). Look up by name first
        # to get the adb id.
        if collection_id:
            aid = str(collection_id)
        else:
            data = self._get("searchalbum.php",
                             {"s": artist, "a": title}, timeout)
            rows = (data or {}).get("album") or []
            if not rows:
                return None
            target_t = norm(title)
            target_a = norm(artist)
            chosen = None
            for r in rows:
                if norm(r.get("strAlbum") or "") == target_t and norm(r.get("strArtist") or "") == target_a:
                    chosen = r
                    break
            if not chosen:
                chosen = rows[0]
            aid = str(chosen.get("idAlbum") or "")
            if not aid:
                return None

        data = self._get("track.php", {"m": aid}, timeout)
        rows = (data or {}).get("track") or []
        if not rows:
            return None

        tracks: list[Track] = []
        for row in rows:
            try:
                tn = int(row.get("intTrackNumber") or 0)
            except (TypeError, ValueError):
                tn = 0
            try:
                disc = int(row.get("intCD") or 1) or 1
            except (TypeError, ValueError):
                disc = 1
            try:
                dur_ms = int(row.get("intDuration") or 0)
            except (TypeError, ValueError):
                dur_ms = 0
            tracks.append(Track(
                source="audiodb",
                source_id=str(row.get("idTrack") or f"adb-{aid}-{tn}"),
                title=(row.get("strTrack") or "").strip(),
                artist=(row.get("strArtist") or artist).strip(),
                duration_ms=dur_ms,
                track_number=tn,
                disc_number=disc,
                source_url=(row.get("strMusicVid") or f"https://www.theaudiodb.com/track/{row.get('idTrack') or ''}"),
            ))
        album = AlbumSummary(
            source="audiodb", source_id=aid,
            title=title or "",
            artist=artist or "",
            artwork=(rows[0].get("strTrackThumb") or ""),
            track_count=len(tracks),
            release_date="",
            genre="",
            source_url=f"https://www.theaudiodb.com/album/{aid}",
        )
        return AlbumPayload(source="audiodb", source_id=aid,
                            album=album, tracks=tracks,
                            source_url=album.source_url)


# ---------------------------------------------------------------- Spotify

class SpotifyProvider(AlbumProvider):
    name = "spotify"
    enabled = bool(os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET"))

    def __init__(self) -> None:
        self.s = requests.Session()
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()
        self._client_id = os.getenv("SPOTIFY_CLIENT_ID") or ""
        self._client_secret = os.getenv("SPOTIFY_CLIENT_SECRET") or ""

    def _ensure_token(self, timeout: float) -> Optional[str]:
        if not (self._client_id and self._client_secret):
            self.enabled = False
            return None
        with self._lock:
            now = time.time()
            if self._token and now < self._token_expires_at - 300:
                return self._token
            try:
                import base64
                creds = base64.b64encode(
                    f"{self._client_id}:{self._client_secret}".encode()).decode()
                r = self.s.post(SPOTIFY_AUTH,
                                data={"grant_type": "client_credentials"},
                                headers={"Authorization": f"Basic {creds}"},
                                timeout=timeout)
                r.raise_for_status()
                data = r.json()
            except Exception:
                self._token = None
                return None
            self._token = data.get("access_token")
            self._token_expires_at = now + int(data.get("expires_in", 3600))
            return self._token

    def _auth_get(self, path: str, params: dict, timeout: float) -> Optional[dict]:
        tok = self._ensure_token(timeout)
        if not tok:
            return None
        try:
            r = self.s.get(f"{SPOTIFY_BASE}{path}",
                           params=params,
                           headers={"Authorization": f"Bearer {tok}"},
                           timeout=timeout)
            if r.status_code == 401:
                # force-refresh once
                self._token = None
                tok = self._ensure_token(timeout)
                if not tok:
                    return None
                r = self.s.get(f"{SPOTIFY_BASE}{path}",
                               params=params,
                               headers={"Authorization": f"Bearer {tok}"},
                               timeout=timeout)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None

    def search_albums(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._auth_get("/search",
                              {"q": query, "type": "album", "limit": max(1, min(limit, 50))},
                              timeout)
        out: list[AlbumSummary] = []
        for item in (data or {}).get("albums", {}).get("items") or []:
            aid = str(item.get("id") or "")
            if not aid:
                continue
            images = item.get("images") or []
            art = images[0].get("url", "") if images else ""
            artists = item.get("artists") or []
            artist = (artists[0].get("name") if artists else "") or ""
            out.append(AlbumSummary(
                source="spotify", source_id=aid,
                title=item.get("name") or "",
                artist=artist,
                artwork=art,
                track_count=item.get("total_tracks") or 0,
                release_date=(item.get("release_date") or ""),
                genre="",
                source_url=f"https://open.spotify.com/album/{aid}",
            ))
        return out

    def search_songs(self, query, limit=25, timeout=DEFAULT_TIMEOUT):
        data = self._auth_get("/search",
                              {"q": query, "type": "track", "limit": max(1, min(limit, 50))},
                              timeout)
        out: list[Track] = []
        for item in (data or {}).get("tracks", {}).get("items") or []:
            artists = item.get("artists") or []
            artist = (artists[0].get("name") if artists else "") or ""
            album = item.get("album") or {}
            out.append(Track(
                source="spotify", source_id=str(item.get("id") or ""),
                title=item.get("name") or "",
                artist=artist,
                duration_ms=int(item.get("duration_ms") or 0),
                track_number=item.get("track_number") or 0,
                disc_number=item.get("disc_number") or 1,
                source_url=(item.get("external_urls") or {}).get("spotify") or "",
            ))
        return out

    def get_album_tracks(self, *, collection_id=None, title="", artist="",
                         timeout=DEFAULT_TIMEOUT):
        if collection_id:
            return self._by_id(collection_id, timeout)
        if not (title and artist):
            return None
        data = self._auth_get("/search",
                              {"q": f"album:{title} artist:{artist}",
                               "type": "album", "limit": 5},
                              timeout)
        items = (data or {}).get("albums", {}).get("items") or []
        target_t = norm(title)
        target_a = norm(artist)
        for item in items:
            if norm(item.get("name") or "") == target_t:
                artists = item.get("artists") or []
                artist_name = (artists[0].get("name") if artists else "") or ""
                if norm(artist_name) == target_a:
                    return self._by_id(str(item.get("id")), timeout)
        if items:
            return self._by_id(str(items[0].get("id")), timeout)
        return None

    def _by_id(self, aid: str, timeout: float) -> Optional[AlbumPayload]:
        data = self._auth_get(f"/albums/{aid}", None, timeout)
        if not data:
            return None
        # /albums/{id}/tracks for paginated full tracklist
        tracks: list[Track] = []
        next_url = f"/albums/{aid}/tracks?limit=50"
        while next_url:
            t_data = self._auth_get(next_url, None, timeout) or {}
            for item in t_data.get("items") or []:
                artists = item.get("artists") or []
                artist = (artists[0].get("name") if artists else "") or ""
                tracks.append(Track(
                    source="spotify",
                    source_id=str(item.get("id") or ""),
                    title=item.get("name") or "",
                    artist=artist,
                    duration_ms=int(item.get("duration_ms") or 0),
                    track_number=item.get("track_number") or 0,
                    disc_number=item.get("disc_number") or 1,
                    source_url=((item.get("external_urls") or {}).get("spotify") or ""),
                ))
            next_url = t_data.get("next")
            if next_url and next_url.startswith("https://api.spotify.com/v1"):
                next_url = next_url[len("https://api.spotify.com/v1"):]
            else:
                next_url = None

        artists = data.get("artists") or []
        artist = (artists[0].get("name") if artists else "") or ""
        images = data.get("images") or []
        art = images[0].get("url", "") if images else ""
        album = AlbumSummary(
            source="spotify", source_id=aid,
            title=data.get("name") or "",
            artist=artist,
            artwork=art,
            track_count=data.get("total_tracks") or len(tracks),
            release_date=data.get("release_date") or "",
            genre="",
            source_url=f"https://open.spotify.com/album/{aid}",
        )
        return AlbumPayload(source="spotify", source_id=aid,
                            album=album, tracks=tracks,
                            source_url=album.source_url)


# ---------------------------------------------------------------- helpers

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# ---------------------------------------------------------------- singleton providers

_PROVIDERS: list[AlbumProvider] = []


def _build_providers() -> list[AlbumProvider]:
    return [
        ITunesProvider(),
        MusicBrainzProvider(),
        DeezerProvider(),
        AudioDBProvider(),
        SpotifyProvider(),
    ]


def get_providers() -> list[AlbumProvider]:
    global _PROVIDERS
    if not _PROVIDERS:
        _PROVIDERS = _build_providers()
    return _PROVIDERS


def enabled_providers() -> list[AlbumProvider]:
    return [p for p in get_providers() if p.enabled]


def _call_provider(name: str, fn: Callable, *args, **kwargs):
    try:
        return name, fn(*args, **kwargs), None
    except Exception as e:  # last-resort guard
        return name, None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- aggregator: search


def aggregate_search(query: str, kind: str = "all", limit: int = 50, *,
                     timeout_per_provider: float = DEFAULT_TIMEOUT,
                     max_parallel: int = DEFAULT_PARALLEL,
                     providers: Optional[Iterable[str]] = None) -> dict:
    """Fan out search to every enabled provider in parallel, collect
    AlbumSummary lists, dedupe by (title_norm, artist_norm), return one
    entry per logical album.
    """
    enabled = enabled_providers()
    if providers is not None:
        wanted = [p for p in enabled if p.name in set(providers)]
    else:
        wanted = enabled
    if not wanted:
        return {"ok": False, "error": "no providers enabled",
                "albums": [], "songs": [],
                "provider_summary": {}, "query": query, "kind": kind}
    providers = wanted

    # Kick off searches in parallel.
    futures = {}
    with ThreadPoolExecutor(max_workers=max_parallel) as ex:
        for p in providers:
            fut = ex.submit(_call_provider, p.name, p.search_albums,
                            query, limit, timeout_per_provider)
            futures[fut] = p
        results: dict[str, list[AlbumSummary]] = {}
        errs: dict[str, str] = {}
        for fut in as_completed(futures):
            name, value, err = fut.result()
            results[name] = value or []
            if err:
                errs[name] = err

    # Dedup by (title_norm, artist_norm), keep all_sources list.
    by_key: dict[tuple[str, str], dict] = {}
    for pname, items in results.items():
        for item in items:
            if not (item.title and item.artist):
                continue
            key = item.norm_key()
            entry = by_key.get(key)
            if entry is None:
                entry = {
                    "album": item,
                    "all_sources": [],
                    "track_counts": {},
                }
                by_key[key] = entry
            entry["all_sources"].append({
                "source": item.source,
                "source_url": item.source_url,
                "track_count": item.track_count or 0,
            })
            if (item.track_count or 0) > entry["track_counts"].get(item.source, 0):
                entry["track_counts"][item.source] = item.track_count or 0
            # Upgrade the headline album if this provider has more tracks
            # or if it's still the empty placeholder.
            cur = entry["album"]
            cur_tc = cur.track_count or 0
            new_tc = item.track_count or 0
            if (not cur.title or cur.title != item.title) and item.title:
                # first concrete title wins
                if not cur.title:
                    entry["album"] = item
                continue
            if new_tc > cur_tc:
                entry["album"] = item

    # Build response
    albums = []
    for entry in by_key.values():
        a = entry["album"]
        albums.append({
            "id": _coerce_id(a.source, a.source_id, a.title, a.artist),
            "title": a.title,
            "artist": a.artist,
            "artwork": a.artwork,
            "track_count": a.track_count or 0,
            "release_date": a.release_date,
            "genre": a.genre,
            "source": a.source,
            "source_id": a.source_id,
            "source_url": a.source_url,
            "all_sources": entry["all_sources"],
        })

    # Best-effort sort by track count desc, then title.
    albums.sort(key=lambda x: (-(x.get("track_count") or 0), x.get("title", "").lower()))
    albums = albums[:limit]

    summary = {p.name: len(results.get(p.name) or []) for p in providers}
    summary["_errors"] = errs  # private key, stripped before return unless debug

    return {
        "ok": True,
        "albums": albums,
        "songs": [],
        "provider_summary": {k: v for k, v in summary.items() if not k.startswith("_")},
        "query": query,
        "kind": kind,
    }


# ---------------------------------------------------------------- aggregator: song search


def aggregate_song_search(query: str, limit: int = 50, *,
                          timeout_per_provider: float = DEFAULT_TIMEOUT,
                          max_parallel: int = DEFAULT_PARALLEL,
                          providers: Optional[Iterable[str]] = None) -> dict:
    """Same shape as aggregate_search but for songs. Used by /api/song/search."""
    enabled = enabled_providers()
    if providers is not None:
        wanted = [p for p in enabled if p.name in set(providers)]
    else:
        wanted = enabled
    if not wanted:
        return {"ok": False, "error": "no providers enabled",
                "songs": [], "provider_summary": {}, "query": query}
    providers = wanted

    with ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futures = {ex.submit(_call_provider, p.name, p.search_songs,
                             query, limit, timeout_per_provider): p
                   for p in providers}
        by_provider: dict[str, list[Track]] = {}
        errs: dict[str, str] = {}
        for fut in as_completed(futures):
            name, value, err = fut.result()
            by_provider[name] = value or []
            if err:
                errs[name] = err

    # Dedup by (title_norm, artist_norm)
    by_key: dict[tuple[str, str], dict] = {}
    for pname, items in by_provider.items():
        for it in items:
            if not (it.title and it.artist):
                continue
            key = (norm(it.title), norm(it.artist))
            entry = by_key.get(key)
            if entry is None:
                # pull album metadata from the underlying record where possible
                entry = {"track": it, "all_sources": [], "album_id": None}
                by_key[key] = entry
            entry["all_sources"].append({"source": it.source, "source_url": it.source_url})

    songs = []
    for entry in by_key.values():
        t = entry["track"]
        songs.append({
            "id": f"{t.source}-{t.source_id}",
            "title": t.title,
            "artist": t.artist,
            "album": "",                  # best-effort: see note below
            "album_id": "",
            "duration_ms": t.duration_ms,
            "source": t.source,
            "source_id": t.source_id,
            "source_url": t.source_url,
            "all_sources": entry["all_sources"],
        })

    summary = {p.name: len(by_provider.get(p.name) or []) for p in providers}
    return {
        "ok": True,
        "songs": songs,
        "provider_summary": summary,
        "query": query,
    }


# ---------------------------------------------------------------- aggregator: album tracks


def aggregate_album_tracks(*,
                           collection_id: Optional[str] = None,
                           title: str = "",
                           artist: str = "",
                           timeout_per_provider: float = DEFAULT_TIMEOUT,
                           max_parallel: int = DEFAULT_PARALLEL,
                           providers: Optional[Iterable[str]] = None) -> dict:
    """Try every enabled provider, merge by 'most tracks wins'. Returns
    the winner with per-track source attribution, plus a per-provider
    diagnostics block.

    collection_id, when present, is the *iTunes* collectionId (number).
    We forward it only to the iTunes provider; everyone else falls back
    to title/artist lookup.
    """
    wanted = [p for p in enabled_providers()
              if providers is None or p.name in providers]
    if not wanted:
        return {"ok": False, "error": "no providers enabled",
                "album": {}, "tracks": [], "providers": {},
                "source": ""}

    with ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futures = {}
        for p in wanted:
            fut = ex.submit(
                _call_provider, p.name, p.get_album_tracks,
                collection_id=collection_id if p.name == "itunes" else None,
                title=title, artist=artist,
                timeout=timeout_per_provider,
            )
            futures[fut] = p

        payloads: dict[str, AlbumPayload] = {}
        errs: dict[str, str] = {}
        for fut in as_completed(futures):
            name, value, err = fut.result()
            if value:
                payloads[name] = value
            if err:
                errs[name] = err

    if not payloads:
        return {"ok": False, "error": "every provider failed",
                "details": errs,
                "album": {}, "tracks": [],
                "providers": {p.name: {"ok": False, "error": errs.get(p.name, ""),
                                       "tracks": 0}
                              for p in wanted},
                "source": ""}

    # Pick the most-tracks winner; break ties by provider priority.
    def keyfn(item):
        name, p = item
        return (len(p.tracks),
                -PROVIDER_PRIORITY.get(name, 99),
                # prefer the one with more populated album metadata
                int(bool(p.album.artwork)),
                int(bool(p.album.title)))

    sorted_payloads = sorted(payloads.items(), key=keyfn, reverse=True)
    winner_name, winner = sorted_payloads[0]

    # Build the merged tracklist with three passes:
    #   1) group all tracks by (norm_title, norm_artist) across providers
    #   2) within each group, keep the most-complete entry as canonical;
    #      exact-duration matches (within 2 s) get auto-dropped;
    #      near-matches get a flag and stay in the list for user review
    #   3) renumber track numbers when two tracks collide on (disc, tn)
    grouped: dict[tuple[str, str], list[tuple[str, Track]]] = {}
    for pname, p in sorted_payloads:
        for t in p.tracks:
            if not t.title:
                continue
            key = (norm(t.title), norm(t.artist or ""))
            grouped.setdefault(key, []).append((pname, t))

    merged: list[Track] = []
    diag = {
        "exact_duplicates_removed": 0,
        "near_duplicates_flagged": 0,
        "track_numbers_renumbered": 0,
        "duplicate_pairs": [],  # [{kept, removed/flagged, reason, ...}]
    }

    for key, members in grouped.items():
        # Pick canonical: most-complete entry; ties broken by provider priority.
        def _score(member):
            pname, t = member
            s = 0
            if t.duration_ms > 0:
                s += 100_000 + t.duration_ms   # longer wins among same title
            if t.source_id:
                s += 1_000
            if t.source_url:
                s += 100
            if t.track_number > 0:
                s += 10
            s -= PROVIDER_PRIORITY.get(pname, 99)
            return s
        members.sort(key=_score, reverse=True)
        primary_pname, primary = members[0]
        primary_full_id = (
            f"{primary_pname}:{primary.source_id}" if primary.source_id else
            f"{primary_pname}:hash"
        )
        merged.append(primary)

        for pname, t in members[1:]:
            # Exact-duration match (≤ 2 s) => silent dedup
            if primary.duration_ms > 0 and t.duration_ms > 0 and \
                    abs(primary.duration_ms - t.duration_ms) <= 2000:
                diag["exact_duplicates_removed"] += 1
                diag["duplicate_pairs"].append({
                    "kept": primary_full_id,
                    "removed": f"{pname}:{t.source_id}",
                    "reason": "exact_match",
                })
                continue
            # Near-match => flag for user review
            diag["near_duplicates_flagged"] += 1
            t._flag = {  # type: ignore[attr-defined]
                "type": "near_duplicate",
                "duplicate_of": primary_full_id,
                "duration_ms_primary": primary.duration_ms,
                "duration_ms_duplicate": t.duration_ms,
                "primary_source": primary_pname,
                "duplicate_source": pname,
            }
            diag["duplicate_pairs"].append({
                "kept": primary_full_id,
                "flagged": f"{pname}:{t.source_id}",
                "reason": "title_match_different_duration",
            })
            merged.append(t)

    # Renumber track_number collisions. Order: keep first occurrence as-is,
    # later collisions push to next available number on the same disc.
    used_numbers: set[tuple[int, int]] = set()
    for t in merged:
        if t.track_number <= 0:
            continue
        key = (t.disc_number, t.track_number)
        if key in used_numbers:
            original = t.track_number
            new_n = original + 1
            while (t.disc_number, new_n) in used_numbers:
                new_n += 1
            t.track_number = new_n
            diag["track_numbers_renumbered"] += 1
            existing = getattr(t, "_flag", None) or {}
            t._flag = {  # type: ignore[attr-defined]
                **existing,
                "type": existing.get("type", "renumbered"),
                "track_number_original": original,
                "track_number_new": new_n,
            }
            used_numbers.add(key)
            used_numbers.add((t.disc_number, new_n))
        else:
            used_numbers.add(key)

    merged.sort(key=lambda t: (t.disc_number, t.track_number, norm(t.title)))

    # Synthesize a richer album metadata: prefer the winner's, but use
    # artwork from any provider if winner's is empty.
    album_meta = winner.album
    for _, p in sorted_payloads:
        if not album_meta.artwork and p.album.artwork:
            album_meta = AlbumSummary(
                source=album_meta.source,
                source_id=album_meta.source_id,
                title=album_meta.title or p.album.title,
                artist=album_meta.artist or p.album.artist,
                artwork=p.album.artwork,
                track_count=album_meta.track_count or p.album.track_count,
                release_date=album_meta.release_date or p.album.release_date,
                genre=album_meta.genre or p.album.genre,
                source_url=album_meta.source_url or p.album.source_url,
            )

    # Build the per-provider diagnostics block
    per_provider = {}
    for p in wanted:
        pld = payloads.get(p.name)
        if pld:
            per_provider[p.name] = {
                "ok": True,
                "tracks": len(pld.tracks),
                "winner": p.name == winner_name,
                "source_url": pld.album.source_url,
            }
        else:
            per_provider[p.name] = {
                "ok": False,
                "tracks": 0,
                "winner": False,
                "error": errs.get(p.name, "no data"),
            }

    return {
        "ok": True,
        "album": {
            "id": _coerce_id(winner.album.source, winner.album.source_id,
                              winner.album.title, winner.album.artist),
            "title": album_meta.title,
            "artist": album_meta.artist,
            "artwork": album_meta.artwork,
            "track_count": len(merged),
            "release_date": album_meta.release_date,
            "genre": album_meta.genre,
            "source": winner_name,
            "source_id": winner.album.source_id,
            "source_url": album_meta.source_url or winner.album.source_url,
            "all_sources": [{"source": pname,
                              "source_url": p.album.source_url,
                              "tracks": len(p.tracks)}
                             for pname, p in payloads.items()],
        },
        "tracks": _serialize_tracks(merged),
        "source": winner_name,
        "providers": per_provider,
        "dedup": diag,
    }


def _serialize_tracks(tracks: list[Track]) -> list[dict]:
    """Serialize tracks, exposing any `_flag` we attached during dedup as a
    top-level `flag` field on the JSON side.
    """
    out: list[dict] = []
    for t in tracks:
        d = asdict(t)
        flag = getattr(t, "_flag", None)
        if flag:
            d["flag"] = flag
        out.append(d)
    return out


# ---------------------------------------------------------------- id coercion


def _coerce_id(source: str, source_id: str, title: str, artist: str) -> str | int:
    """The cart uses collectionId as the dedup key. iTunes is the only
    provider with a numeric id, so we keep that as-is; for everyone else
    we synthesize a stable string id from the source + provider-native id.
    """
    if source == "itunes" and source_id:
        try:
            return int(source_id)
        except ValueError:
            pass
    if source_id:
        return f"{source}:{source_id}"
    # last resort: title+artist hash
    h = abs(hash((norm(title), norm(artist))))
    return f"{source}:{h}"