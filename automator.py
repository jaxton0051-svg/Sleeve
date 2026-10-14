"""
automator.py \u2014 AUTOMATOR tab backend.

Pipeline per album (driven by automator_album_job, called from app.py):
  1. yt-dlp search "<artist> - <title> audio" -> bestaudio MP3
  2. ffprobe audio integrity (duration sanity, codec is audio, bitrate sane)
  3. mutagen writes correct ID3 tags (TIT2/TPE1/TALB/TPE2/TRCK/TPOS/TYER/TCON)
  4. mutagen tag-verify re-read
  5. faster-whisper transcription -> .lrc sidecar
  6. copy audio + .lrc to <music_folder>/<Artist>/<Album>/<NN> - <Title>.mp3
  7. if backup enabled: same copy to <backup_path>/...
  8. POST Jellyfin /Library/Refresh (once per album job)
  9. cleanup <app-data>/automator_tmp/<job>/

The worker respects a per-job cancel flag (`jobs[job_id]['cancel_requested']`)
checked at the top of every track loop.

The Automator profile and local state live in the per-user app-data directory.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import shutil
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from cookie_cleaner import ensure_cookie_file_valid  # self-healing cookies
from app_paths import DATA_DIR, data_file

PROJECT_DIR = Path(__file__).parent.resolve()
PROFILE_FILE = DATA_DIR / "automator_profile.json"
PROFILE_DB_FILE = DATA_DIR / "automator_profile.sqlite3"
_PROFILE_DB_LOCK = threading.RLock()
_PROFILE_RESET_MARKER = "profile_reset_2026_10_welcome"
TMP_ROOT = DATA_DIR / "automator_tmp"
TMP_ROOT.mkdir(exist_ok=True)

APP_COOKIE_FILE = data_file("cookie.txt")
APP_FFMPEG = Path(shutil.which("ffmpeg") or (PROJECT_DIR / "ffmpeg.exe"))
APP_FFPROBE = Path(shutil.which("ffprobe") or (PROJECT_DIR / "ffprobe.exe"))

# ---------------------------------------------------------------- profile


@dataclass
class BackupConfig:
    enabled: bool = False
    mode: str = "skip"        # "skip" | "local" | "smb"
    path: str = ""            # UNC or local path, only used when mode != skip


@dataclass
class AutomatorProfile:
    server_name: str = ""
    jellyfin_url: str = ""    # e.g. http://192.168.1.50:8096
    jellyfin_api_key: str = ""
    music_folder: str = ""    # e.g. \\JELLYFIN\Music\Automator
    whisper_model: str = "turbo"      # tiny.en|base.en|small.en|small|medium.en|large-v3|turbo
    whisper_device: str = "auto"      # auto|cpu|cuda
    backup: BackupConfig = field(default_factory=BackupConfig)
    setup_complete: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def mask_key(self) -> str:
        if not self.jellyfin_api_key:
            return ""
        if len(self.jellyfin_api_key) <= 4:
            return "*" * len(self.jellyfin_api_key)
        return "*" * (len(self.jellyfin_api_key) - 4) + self.jellyfin_api_key[-4:]

    def public_view(self) -> dict:
        return {
            "server_name": self.server_name,
            "jellyfin_url": self.jellyfin_url,
            "api_key_masked": self.mask_key(),
            "music_folder": self.music_folder,
            "whisper_model": self.whisper_model,
            "whisper_device": self.whisper_device,
            "backup": {
                "enabled": self.backup.enabled,
                "mode": self.backup.mode,
                "path": self.backup.path,
            },
            "updated_at": self.updated_at,
        }

    def is_configured(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key and self.music_folder)


def _connect_profile_db() -> sqlite3.Connection:
    PROFILE_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(PROFILE_DB_FILE), timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _ensure_profile_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS automator_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            server_name TEXT NOT NULL DEFAULT '',
            jellyfin_url TEXT NOT NULL DEFAULT '',
            jellyfin_api_key TEXT NOT NULL DEFAULT '',
            music_folder TEXT NOT NULL DEFAULT '',
            whisper_model TEXT NOT NULL DEFAULT 'turbo',
            whisper_device TEXT NOT NULL DEFAULT 'auto',
            backup_enabled INTEGER NOT NULL DEFAULT 0,
            backup_mode TEXT NOT NULL DEFAULT 'skip',
            backup_path TEXT NOT NULL DEFAULT '',
            setup_complete INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        )
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(automator_profile)")}
    if "setup_complete" not in columns:
        connection.execute("ALTER TABLE automator_profile ADD COLUMN setup_complete INTEGER NOT NULL DEFAULT 0")
    connection.execute("CREATE TABLE IF NOT EXISTS sleeve_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    reset_done = connection.execute(
        "SELECT 1 FROM sleeve_state WHERE key = ?", (_PROFILE_RESET_MARKER,)
    ).fetchone()
    if not reset_done:
        # A one-time profile reset requested for the fresh-start test. Other
        # app data lives separately and is intentionally left untouched.
        connection.execute("DELETE FROM automator_profile")
        connection.execute("INSERT INTO sleeve_state (key, value) VALUES (?, 'done')", (_PROFILE_RESET_MARKER,))
        for legacy_file in (
            PROFILE_FILE,
            PROJECT_DIR / "automator_profile.json",
            DATA_DIR / "jellyfin_auth.json",
            PROJECT_DIR / "jellyfin_auth.json",
        ):
            try:
                legacy_file.unlink(missing_ok=True)
            except OSError as exc:
                print(f"[automator] could not remove obsolete {legacy_file.name}: {exc}")


def _write_profile(connection: sqlite3.Connection, profile: AutomatorProfile) -> None:
    backup = profile.backup
    connection.execute("""
        INSERT INTO automator_profile (
            id, server_name, jellyfin_url, jellyfin_api_key, music_folder,
            whisper_model, whisper_device, backup_enabled, backup_mode,
            backup_path, setup_complete, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            server_name=excluded.server_name,
            jellyfin_url=excluded.jellyfin_url,
            jellyfin_api_key=excluded.jellyfin_api_key,
            music_folder=excluded.music_folder,
            whisper_model=excluded.whisper_model,
            whisper_device=excluded.whisper_device,
            backup_enabled=excluded.backup_enabled,
            backup_mode=excluded.backup_mode,
            backup_path=excluded.backup_path,
            setup_complete=excluded.setup_complete,
            updated_at=excluded.updated_at
    """, (
        profile.server_name, profile.jellyfin_url, profile.jellyfin_api_key,
        profile.music_folder, profile.whisper_model, profile.whisper_device,
        int(backup.enabled), backup.mode, backup.path, int(profile.setup_complete), profile.updated_at,
    ))


def load_profile() -> AutomatorProfile:
    try:
        with _PROFILE_DB_LOCK:
            connection = _connect_profile_db()
            try:
                with connection:
                    _ensure_profile_schema(connection)
                    row = connection.execute("""
                        SELECT server_name, jellyfin_url, jellyfin_api_key, music_folder,
                               whisper_model, whisper_device, backup_enabled,
                               backup_mode, backup_path, setup_complete, updated_at
                        FROM automator_profile WHERE id = 1
                    """).fetchone()
            finally:
                connection.close()
        if not row:
            return AutomatorProfile()
        return AutomatorProfile(
            server_name=row[0], jellyfin_url=row[1], jellyfin_api_key=row[2],
            music_folder=row[3], whisper_model=row[4], whisper_device=row[5],
            backup=BackupConfig(enabled=bool(row[6]), mode=row[7], path=row[8]),
            setup_complete=bool(row[9]), updated_at=row[10],
        )
    except Exception as exc:
        print(f"[automator] profile load failed: {type(exc).__name__}: {exc}")
        return AutomatorProfile()


def save_profile(profile: AutomatorProfile) -> None:
    profile.updated_at = datetime.now().isoformat(timespec="seconds")
    with _PROFILE_DB_LOCK:
        connection = _connect_profile_db()
        try:
            with connection:
                _ensure_profile_schema(connection)
                _write_profile(connection, profile)
        finally:
            connection.close()


# ---------------------------------------------------------------- Jellyfin


class JellyfinClient:
    """Lightweight REST wrapper for the bits of Jellyfin's API we need."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 10):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "X-Emby-Token": self.api_key,
            "Accept": "application/json",
            "User-Agent": "Sleeve/1.0",
        }

    def test_connection(self) -> dict:
        """Hit /System/Info/Public (no auth) to confirm the server is reachable
        AND we can resolve its advertised URL, then /System/Info with the API key
        to confirm auth works."""
        if not self.base_url:
            return {"ok": False, "error": "Jellyfin URL is empty."}
        if not self.api_key:
            return {"ok": False, "error": "API key is empty."}
        try:
            r = requests.get(f"{self.base_url}/System/Info/Public",
                             timeout=self.timeout,
                             headers={"User-Agent": "Sleeve/1.0"})
        except Exception as e:
            return {"ok": False, "error": f"Cannot reach {self.base_url}: {type(e).__name__}: {e}"}
        if r.status_code != 200:
            return {"ok": False, "error": f"Public endpoint returned HTTP {r.status_code}."}
        try:
            info = r.json()
        except Exception:
            return {"ok": False, "error": "Server responded, but body was not JSON."}
        try:
            r2 = requests.get(f"{self.base_url}/System/Info",
                              timeout=self.timeout,
                              headers=self._headers())
        except Exception as e:
            return {"ok": False, "error": f"Authenticated request failed: {type(e).__name__}: {e}"}
        if r2.status_code == 401:
            return {"ok": False, "error": "Authentication failed (401). Check the API key."}
        if r2.status_code != 200:
            return {"ok": False, "error": f"Authenticated request returned HTTP {r2.status_code}."}
        try:
            info2 = r2.json()
        except Exception:
            return {"ok": False, "error": "Auth endpoint returned non-JSON."}
        return {
            "ok": True,
            "server_name": info2.get("ServerName") or info.get("ServerName") or "Jellyfin",
            "version": info2.get("Version") or info.get("Version") or "",
            "os": (info2.get("OperatingSystem") or info.get("OperatingSystem") or ""),
        }

    def get_recent_albums(self, limit: int = 24) -> list[dict]:
        """GET /Items/Latest?Limit=N&IncludeItemTypes=MusicAlbum.

        Falls back to /Items (Recursive, sorted by SortName) if /Items/Latest
        returns nothing — this covers the common case where the user has just
        configured the server and there are no "recently added" items yet, but
        the library already contains older albums.
        """
        def _normalize(items: list) -> list[dict]:
            out = []
            for item in items or []:
                artist = item.get("AlbumArtist") or ""
                if not artist:
                    artists = item.get("Artists")
                    if isinstance(artists, list) and artists:
                        artist = artists[0]
                    elif isinstance(artists, str) and artists:
                        artist = artists
                out.append({
                    "id": item.get("Id"),
                    "title": item.get("Name") or "",
                    "artist": artist or "",
                    "artwork": self._thumb_url(item.get("Id"), (item.get("ImageTags") or {}).get("Primary")),
                })
            return out

        # 1. /Items/Latest (newest additions)
        try:
            r = requests.get(
                f"{self.base_url}/Items/Latest",
                params={"Limit": max(1, min(limit, 50)),
                        "IncludeItemTypes": "MusicAlbum",
                        "Fields": "PrimaryImageAspectRatio,SortName,AlbumArtist,Artists,ImageTags"},
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code == 200:
                data = r.json() or []
                if data:
                    return _normalize(data)
        except Exception:
            pass

        # 2. Fallback: full library list sorted by SortName
        try:
            r = requests.get(
                f"{self.base_url}/Items",
                params={"IncludeItemTypes": "MusicAlbum",
                        "Recursive": "true",
                        "SortBy": "SortName",
                        "SortOrder": "Ascending",
                        "Limit": max(1, min(limit, 50)),
                        "Fields": "PrimaryImageAspectRatio,SortName,AlbumArtist,Artists,ImageTags"},
                headers=self._headers(),
                timeout=max(self.timeout, 30),
            )
            if r.status_code != 200:
                return []
            data = (r.json() or {}).get("Items") or []
            return _normalize(data)
        except Exception:
            return []

    def get_all_albums(self, limit: int = 500, start_index: int = 0,
                       sort_by: str = "SortName") -> tuple[list[dict], int]:
        """Gallery: fetch ALL albums from Jellyfin (paginated).

        Returns (albums, total_count). Each album dict has the same shape as
        get_recent_albums (id, title, artist, artwork) plus track_count and
        year when available, so the Gallery UI can show full metadata.

        Uses /Items?IncludeItemTypes=MusicAlbum&Recursive=true so we get the
        entire library, not just the latest 50.
        """
        def _normalize(items: list) -> list[dict]:
            out = []
            for item in items or []:
                artist = item.get("AlbumArtist") or ""
                if not artist:
                    artists = item.get("Artists")
                    if isinstance(artists, list) and artists:
                        artist = artists[0]
                    elif isinstance(artists, str) and artists:
                        artist = artists
                # ProductionYear can be empty string; coerce safely.
                try:
                    year = int(item.get("ProductionYear") or 0) or None
                except Exception:
                    year = None
                out.append({
                    "id": item.get("Id"),
                    "title": item.get("Name") or "",
                    "artist": artist or "",
                    "artwork": self._thumb_url(item.get("Id"), (item.get("ImageTags") or {}).get("Primary")),
                    "year": year,
                    "track_count": int(item.get("ChildCount") or 0),
                })
            return out

        try:
            r = requests.get(
                f"{self.base_url}/Items",
                params={
                    "IncludeItemTypes": "MusicAlbum",
                    "Recursive": "true",
                    "SortBy": sort_by,
                    "SortOrder": "Ascending",
                    "StartIndex": max(0, int(start_index)),
                    "Limit": max(1, min(int(limit), 5000)),
                    "Fields": "PrimaryImageAspectRatio,SortName,AlbumArtist,Artists,"
                              "ImageTags,ProductionYear,ChildCount",
                },
                headers=self._headers(),
                timeout=max(self.timeout, 60),
            )
            if r.status_code != 200:
                return [], 0
            data = r.json() or {}
            items = data.get("Items") or []
            total = int(data.get("TotalRecordCount") or len(items))
            return _normalize(items), total
        except Exception as e:
            print(f"[JellyfinClient.get_all_albums] failed: {type(e).__name__}: {e}")
            return [], 0

    def get_album_tracks(self, album_id: str) -> list[dict]:
        """Gallery: list tracks for one Jellyfin album. Each dict is shaped
        to match the cart's `tracks` list, so an album can be added to the
        cart directly from the Gallery UI."""
        if not album_id:
            return []
        try:
            r = requests.get(
                f"{self.base_url}/Items",
                params={
                    "ParentId": album_id,
                    "IncludeItemTypes": "Audio",
                    "Recursive": "false",
                    "SortBy": "IndexNumber",
                    "SortOrder": "Ascending",
                    "Fields": "Name,AlbumArtist,Artists,Artists,IndexNumber,ParentIndexNumber,"
                              "RunTimeTicks,Album,AlbumId",
                },
                headers=self._headers(),
                timeout=max(self.timeout, 30),
            )
            if r.status_code != 200:
                return []
            items = (r.json() or {}).get("Items") or []
            out = []
            for it in items:
                artist = it.get("AlbumArtist") or ""
                if not artist:
                    artists = it.get("Artists")
                    if isinstance(artists, list) and artists:
                        artist = artists[0]
                    elif isinstance(artists, str) and artists:
                        artist = artists
                ticks = int(it.get("RunTimeTicks") or 0)
                duration_ms = ticks // 10000 if ticks > 0 else 0
                try:
                    track_num = int(it.get("IndexNumber") or 0) or None
                except Exception:
                    track_num = None
                try:
                    disc_num = int(it.get("ParentIndexNumber") or 1) or 1
                except Exception:
                    disc_num = 1
                out.append({
                    "id": f"jf-{it.get('Id')}",
                    "title": it.get("Name") or "",
                    "artist": artist or "",
                    "duration_ms": duration_ms,
                    "track_number": track_num,
                    "disc_number": disc_num,
                })
            return out
        except Exception as e:
            print(f"[JellyfinClient.get_album_tracks] failed: {type(e).__name__}: {e}")
            return []

    def get_recent_tracks(self, limit: int = 500) -> list[dict]:
        """R2: fetch up to `limit` audio items from the Jellyfin library for
        track-level server dedup. Returns [{artist, title, duration_ms}, ...].

        Hits /Items?IncludeItemTypes=Audio&Recursive=true. Falls back to /Items/Latest
        if /Items returns an empty list (e.g. server just configured, no recent
        adds). On any error returns [] so callers can fall back to False.
        """
        def _norm_tracks(items: list) -> list[dict]:
            out = []
            for item in items or []:
                artist = item.get("Artist") or ""
                if not artist:
                    artists = item.get("Artists")
                    if isinstance(artists, list) and artists:
                        artist = artists[0]
                artist = artist or item.get("AlbumArtist") or ""
                # duration comes from Jellyfin in ticks (1 tick = 100 ns)
                ticks = int(item.get("RunTimeTicks") or 0)
                duration_ms = ticks // 10000 if ticks > 0 else 0
                out.append({
                    "artist": artist or "",
                    "title": item.get("Name") or "",
                    "duration_ms": duration_ms,
                })
            return out

        # 1. /Items?Recursive=true — full library snapshot, up to `limit`.
        try:
            r = requests.get(
                f"{self.base_url}/Items",
                params={
                    "IncludeItemTypes": "Audio",
                    "Recursive": "true",
                    "SortBy": "DateCreated,SortName",
                    "SortOrder": "Descending,Ascending",
                    "Limit": max(1, min(limit, 5000)),
                    "Fields": "Artist,AlbumArtist,Artists,RunTimeTicks",
                },
                headers=self._headers(),
                timeout=max(self.timeout, 30),
            )
            if r.status_code == 200:
                data = (r.json() or {}).get("Items") or []
                if data:
                    return _norm_tracks(data)
        except Exception:
            pass
        # 2. Fallback: /Items/Latest (newer / smaller libraries)
        try:
            r = requests.get(
                f"{self.base_url}/Items/Latest",
                params={"Limit": max(1, min(limit, 50)),
                        "IncludeItemTypes": "Audio",
                        "Fields": "Artist,AlbumArtist,Artists,RunTimeTicks"},
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code == 200:
                data = r.json() or []
                return _norm_tracks(data)
        except Exception:
            pass
        return []

    def refresh_library(self) -> dict:
        """POST /Library/Refresh — kicks a full library scan."""
        try:
            r = requests.post(f"{self.base_url}/Library/Refresh",
                              headers=self._headers(),
                              timeout=self.timeout)
            if r.status_code in (200, 204):
                return {"ok": True}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _thumb_url(self, item_id: Optional[str], tag: Optional[str]) -> str:
        if not item_id or not tag:
            return ""
        return f"{self.base_url}/Items/{item_id}/Images/Primary?maxHeight=300&tag={tag}"


# ---------------------------------------------------------------- path utils

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_path_component(s: str, fallback: str = "_") -> str:
    if not s:
        return fallback
    cleaned = ILLEGAL.sub("_", s).strip().rstrip(".").strip()
    # Windows reserved names
    upper = cleaned.upper()
    if upper in {"CON", "PRN", "AUX", "NUL"} or upper.startswith(("COM", "LPT")) and upper[3:].isdigit():
        cleaned = "_" + cleaned
    return cleaned or fallback


def unique_target_path(folder: Path, base: str, ext: str) -> Path:
    """Return folder / f"{base}{ext}" if free, else folder / f"{base} - 2{ext}", -3, etc."""
    p = folder / f"{base}{ext}"
    if not p.exists():
        return p
    n = 2
    while True:
        p = folder / f"{base} - {n}{ext}"
        if not p.exists():
            return p
        n += 1
        if n > 999:
            raise RuntimeError(f"Too many name collisions for {base}{ext} in {folder}")


# ---------------------------------------------------------------- verification


def verify_audio(mp3_path: Path, expected_ms: int = 0) -> dict:
    """ffprobe the file. Returns {ok, error, duration, bit_rate, codec_type}."""
    if not mp3_path.exists():
        return {"ok": False, "error": "file missing"}
    if mp3_path.stat().st_size < 50_000:
        return {"ok": False, "error": "file too small (< 50KB)"}
    if not APP_FFPROBE.exists():
        return {"ok": False, "error": "ffprobe.exe not found in project root"}
    try:
        out = subprocess.run(
            [str(APP_FFPROBE), "-v", "error",
             "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,codec_type:format=duration,bit_rate",
             "-of", "json", str(mp3_path)],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        return {"ok": False, "error": f"ffprobe failed: {type(e).__name__}: {e}"}
    if out.returncode != 0:
        return {"ok": False, "error": f"ffprobe rc={out.returncode}: {out.stderr.strip()[:160]}"}
    try:
        data = json.loads(out.stdout or "{}")
    except Exception as e:
        return {"ok": False, "error": f"ffprobe parse: {e}"}
    streams = (data.get("streams") or [])
    if not streams:
        return {"ok": False, "error": "no audio stream found"}
    s0 = streams[0]
    codec_type = s0.get("codec_type") or ""
    if codec_type and codec_type != "audio":
        return {"ok": False, "error": f"codec_type={codec_type} (expected audio)"}
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or 0.0)
    bit_rate = int(fmt.get("bit_rate") or 0)
    if duration < 5.0:
        return {"ok": False, "error": f"duration {duration:.1f}s too short"}
    if expected_ms > 0:
        expected_s = expected_ms / 1000.0
        # Allow generous slack — live versions, intros, and extended/club mixes
        # are routinely 1.3–1.8x the studio edit. We accept up to 1.8x the expected
        # duration, capped so a wildly mismatched track (e.g. a 30-min live recording
        # tagged as a 3-min single) still gets rejected.
        low = max(5.0, expected_s * 0.4)
        high = max(expected_s * 1.8, expected_s + 60.0)
        # Hard ceiling: never accept anything more than 15 minutes over expected,
        # even if expected_s * 1.8 is huge.
        high = min(high, expected_s + 900.0)
        if duration < low or duration > high:
            return {"ok": False, "error":
                    f"duration {duration:.1f}s out of expected range "
                    f"[{low:.0f}\u2013{high:.0f}s]"}
    if bit_rate and bit_rate < 32000:
        return {"ok": False, "error": f"bit_rate {bit_rate} too low"}
    return {"ok": True, "duration": duration, "bit_rate": bit_rate,
            "codec_type": codec_type or "audio", "codec_name": s0.get("codec_name", "")}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tag_matches(actual: str, expected: str) -> bool:
    """R2: tolerance for read-back tag verification.

    Two layers of tolerance:
      1. Substring match on the legacy lower-cased trimmed form — preserves
         the original 'Abbey Road (Remastered)' ~= 'Abbey Road' behavior.
      2. Equality on FULL-CAPS no-spaces canonical form — covers
         'B T S' vs 'BTS', 'P!nk' vs 'PNK', accents stripped.
    """
    if not expected:
        return True
    a, e = _norm(actual), _norm(expected)
    if not e:
        return True
    if e in a or a in e:
        return True
    # Strip parenthetical suffixes for tolerant matching: "Abbey Road (Remastered)" ~= "Abbey Road"
    a_clean = re.sub(r"\s*\([^)]*\)\s*", " ", a).strip()
    e_clean = re.sub(r"\s*\([^)]*\)\s*", " ", e).strip()
    if e_clean and (e_clean in a_clean or a_clean in e_clean):
        return True
    # Canonical equality (R2): FULL CAPS, no spaces/punctuation/accents.
    return canonical(actual) == canonical(expected)


def tag_and_verify(mp3_path: Path, expected: dict) -> dict:
    """Write expected ID3 tags (artist/title/album/track/disc/year/genre),
    then re-read and assert they match.

    expected keys: artist, title, album, album_artist, track_number, disc_number,
                   total_tracks, total_discs, year, genre.
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TYER, TDRC, TCON, TXXX, ID3NoHeaderError
    except ImportError as e:
        return {"ok": False, "error": f"mutagen not installed: {e}"}

    title = (expected.get("title") or "").strip()
    artist = (expected.get("artist") or expected.get("album_artist") or "").strip()
    album = (expected.get("album") or "").strip()
    album_artist = (expected.get("album_artist") or artist).strip()
    tn = expected.get("track_number") or 0
    dn = expected.get("disc_number") or 1
    tot_tr = expected.get("total_tracks") or 0
    tot_di = expected.get("total_discs") or 1
    year = expected.get("year") or ""
    genre = expected.get("genre") or ""

    try:
        try:
            audio = MP3(str(mp3_path), ID3=ID3)
        except ID3NoHeaderError:
            audio = MP3(str(mp3_path))
            audio.add_tags()
        tags = audio.tags
        if title:
            tags.add(TIT2(encoding=3, text=[title]))
        if artist:
            tags.add(TPE1(encoding=3, text=[artist]))
        if album:
            tags.add(TALB(encoding=3, text=[album]))
        if album_artist:
            tags.add(TPE2(encoding=3, text=[album_artist]))
        if tn:
            # Track number as plain integer ("1", "2", ...) so Jellyfin displays it
            # cleanly. Total-track count goes into a separate frame below if available.
            tags.add(TRCK(encoding=3, text=[str(int(tn))]))
            if tot_tr:
                tags.add(TXXX(encoding=3, text=[str(int(tot_tr))], desc="TOTALTRACKS"))
        if dn:
            tpos = str(int(dn))
            tags.add(TPOS(encoding=3, text=[tpos]))
            if tot_di and int(tot_di) > 1:
                tags.add(TXXX(encoding=3, text=[str(int(tot_di))], desc="TOTALDISCS"))
        if year:
            # Clear stale frames first so an old year doesn't survive a re-tag.
            for stale in (TYER, TDRC):
                if stale in tags:
                    try:
                        del tags[stale]
                    except Exception:
                        pass
            tags.add(TDRC(encoding=3, text=[str(year)[:4]]))
            tags.add(TYER(encoding=3, text=[str(year)[:4]]))
        if genre:
            tags.add(TCON(encoding=3, text=[genre]))
        audio.save()
    except Exception as e:
        return {"ok": False, "error": f"tag write failed: {type(e).__name__}: {e}"}

    # Verify by re-reading
    try:
        a2 = MP3(str(mp3_path), ID3=ID3)
        t2 = a2.tags
        checks = []
        for tag, expected_v in (
            (TIT2, title),
            (TPE1, artist),
            (TALB, album),
        ):
            try:
                actual = str(tag).text[0] if tag in t2 else ""
            except Exception:
                actual = ""
            checks.append((tag.__name__, expected_v, actual))
            if not _tag_matches(actual, expected_v):
                return {"ok": False, "error":
                        f"tag verify failed: {tag.__name__}={actual!r} != expected {expected_v!r}"}
    except Exception as e:
        return {"ok": False, "error": f"tag re-read failed: {e}"}

    return {"ok": True}


# ---------------------------------------------------------------- whisper

_whisper_model_cache: dict = {}
_whisper_lock = threading.Lock()


def _get_whisper_model(model_size: str, device: str):
    """Lazy-load + cache faster_whisper models. LRU not strictly enforced \u2014
    small.en is the default and only ~460MB so we keep one or two resident."""
    key = (model_size, device)
    with _whisper_lock:
        m = _whisper_model_cache.get(key)
        if m is not None:
            return m
        from faster_whisper import WhisperModel
        # device "auto" \u2192 let ctranslate2 choose (cuda if available, else cpu)
        dev = device if device in ("cpu", "cuda") else "auto"
        kw = {"device": dev, "compute_type": "auto"}
        try:
            m = WhisperModel(model_size, **kw)
        except Exception as e:
            # If user picked cuda and it failed, fall back to cpu once.
            if dev == "cuda":
                print(f"[whisper] cuda load failed ({e}); falling back to cpu")
                m = WhisperModel(model_size, device="cpu", compute_type="auto")
            else:
                raise
        _whisper_model_cache[key] = m
        return m


def transcribe_lrc(mp3_path: Path, model_size: str = "small.en",
                   device: str = "auto") -> dict:
    """Run faster-whisper on mp3_path. Returns {ok, lrc_text, error}.

    Tuned for vocal music (not speech). Key decisions vs the old version:

    * vad_filter is OFF. Silero VAD is trained on speech and splits every vocal
      breath / instrumental bridge into its own ~1s segment, producing 1-5
      word fragments that look like nonsense. Letting Whisper see the whole
      audio lets it output coherent multi-word lines with realistic timestamps.

    * beam_size=5: a small accuracy bump at a modest speed cost.

    * compute_type is decided per-device: int8 on CPU (2-3x faster than
      float32), float16 on CUDA (better numerical stability). If int8 is
      rejected for the chosen model (e.g. large-v3), we fall back to float32.

    * condition_on_previous_text=False: a hallucinated line won't poison the
      next line.

    * Output is filtered for the obvious audio-description hallucinations
      ([Music], [Applause], ...) and for lines shorter than 3 visible chars.
    """
    try:
        model = _get_whisper_model(model_size, device)
    except Exception as e:
        # NOTE: lrc_text is intentionally empty. The caller uses `ok and
        # lrc_text` to decide whether to write a .lrc file; an empty
        # lrc_text here means "don't write a placeholder file". The
        # status surfaces via `error` and the UI's "Lyrics unavailable"
        # banner separately.
        return {"ok": False, "error": f"whisper load: {type(e).__name__}: {e}",
                "lrc_text": ""}
    # Decide compute_type from the resolved device. `_get_whisper_model` may
    # have used compute_type="auto" when device=="auto"; we override here so
    # music runs fast. int8 is much faster on CPU; CUDA prefers float16.
    try:
        dev_resolved = str(getattr(model, "device", "cpu") or "cpu").lower()
    except Exception:
        dev_resolved = "cpu"
    is_cuda = "cuda" in dev_resolved
    compute_type = "float16" if is_cuda else "int8"
    segments: list = []
    info = None
    try:
        try:
            seg_iter, info = model.transcribe(
                str(mp3_path),
                task="transcribe",
                beam_size=5,
                vad_filter=False,                  # KEY FIX: don't fragment vocals
                condition_on_previous_text=False,  # don't poison the next line
                word_timestamps=False,
                compute_type=compute_type,
            )
            segments = list(seg_iter)
        except Exception as inner:
            # int8 sometimes rejects models that don't have int8 weights
            # (e.g. some large-v3 variants). Fall back to float32 once.
            if compute_type == "int8":
                seg_iter, info = model.transcribe(
                    str(mp3_path),
                    task="transcribe",
                    beam_size=5,
                    vad_filter=False,
                    condition_on_previous_text=False,
                    word_timestamps=False,
                    compute_type="float32",
                )
                segments = list(seg_iter)
            else:
                raise inner
    except Exception as e:
        return {"ok": False, "error": f"whisper transcribe: {type(e).__name__}: {e}",
                "lrc_text": ""}

    lang = getattr(info, "language", "") or "" if info else ""
    hallucination_tokens = {
        "[music]", "(music)", "[applause]", "(applause)",
        "[silence]", "(silence)", "[inaudible]", "(inaudible)",
        "...", "[laughter]", "(laughter)", "[noise]", "(noise)",
    }
    lines = []
    for s in segments:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        low = text.lower().strip()
        if low in hallucination_tokens:
            continue
        if len(text) < 3:
            continue
        start = float(getattr(s, "start", 0.0) or 0.0)
        lines.append(f"[{_lrc_ts(start)}]{text}")
    if not lines:
        return {"ok": True, "lrc_text": "[00:00.00]\n",
                "language": lang, "segments": 0}
    header = f"; generated by faster-whisper ({model_size}, vad=off)\n"
    body = "\n".join(lines) + "\n"
    return {"ok": True, "lrc_text": header + body,
            "language": lang, "segments": len(lines)}


def embed_cover_art(mp3_path: Path, artwork_url: str) -> dict:
    """Fetch artwork_url and embed it as an ID3 APIC frame in mp3_path.

    Best-effort: any failure returns {ok:False, error:...} and the worker
    continues without cover art. Never raises.

    Supports JPEG / PNG / GIF (mime-type is inferred from magic bytes).
    Type 3 = front cover. Existing APIC frames are removed first so we don't
    accumulate duplicates on re-tag.
    """
    if not artwork_url or not mp3_path or not mp3_path.exists():
        return {"ok": False, "error": "missing input"}
    try:
        from mutagen.id3 import ID3, APIC, ID3NoHeaderError
        from mutagen.mp3 import MP3
    except ImportError as e:
        return {"ok": False, "error": f"import: {type(e).__name__}: {e}"}

    try:
        # Fetch artwork bytes (8s timeout, up to 2 attempts).
        img_bytes: bytes = b""
        last_err = ""
        for _attempt in (1, 2):
            try:
                rr = requests.get(
                    artwork_url,
                    timeout=8,
                    headers={"User-Agent": "Mozilla/5.0 (Sleeve)"},
                )
                if rr.status_code == 200 and len(rr.content) > 200:
                    img_bytes = rr.content
                    break
                last_err = f"status={rr.status_code}, bytes={len(rr.content)}"
            except Exception as ee:
                last_err = f"{type(ee).__name__}: {ee}"
        if not img_bytes:
            return {"ok": False, "error": f"fetch failed: {last_err}"}

        # Guess mime from magic bytes; default to image/jpeg.
        if img_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif img_bytes[:2] == b"\xff\xd8":
            mime = "image/jpeg"
        elif img_bytes[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        else:
            mime = "image/jpeg"

        try:
            audio = MP3(str(mp3_path), ID3=ID3)
        except ID3NoHeaderError:
            return {"ok": False, "error": "no id3 header"}
        tags = audio.tags
        if tags is None:
            return {"ok": False, "error": "no tags container"}
        # Remove existing APIC frames so we don't accumulate duplicates.
        for k in list(tags.keys()):
            if k.startswith("APIC"):
                try:
                    del tags[k]
                except Exception:
                    pass
        tags.add(APIC(encoding=3, mime=mime, type=3,  # type=3 = front cover
                      desc="Front cover", data=img_bytes))
        audio.save()
        return {"ok": True, "bytes": len(img_bytes), "mime": mime}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _lrc_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:02d}:{s:05.2f}"


# ---------------------------------------------------------------- LRCLIB lyrics lookup

# Module-level config for LRCLIB. Public API, no auth required.
LRCLIB_BASE = "https://lrclib.net"
LRCLIB_USER_AGENT = "Sleeve/1.0 (https://lrclib.net)"


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect LRCLIB / generic 'Too Many Requests' / HTTP 429 errors."""
    msg = str(exc).lower()
    return any(s in msg for s in (
        "too many requests", "429", "rate limit", "rate-limit",
    ))


def _lrclib_get_request(path: str, params: dict, timeout: float = 15.0,
                        max_retries: int = 3) -> dict | None:
    """GET LRCLIB API. Returns parsed JSON dict, or None on failure.

    Returns:
        dict on success (may contain "instrumental": true, no lyrics).
        None on transport / non-404 failure.
    Raises:
        urllib.error.HTTPError 404 — caller should treat as "not found".
    Retries on 429 with exponential backoff. 404 and other 4xx/5xx fail fast.
    """
    import urllib.request
    import urllib.parse
    import urllib.error
    import json as _json

    url = f"{LRCLIB_BASE}{path}?{urllib.parse.urlencode(params)}"
    backoff = 2.0
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": LRCLIB_USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return _json.loads(body.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            # 404: not found — bubble up so caller can fall back to /search
            if e.code == 404:
                raise
            last_err = e
            if e.code == 429 and attempt < max_retries:
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue
            print(f"[lrclib] GET {path} failed: HTTP {e.code}")
            return None
        except Exception as e:
            last_err = e
            if _is_rate_limit_error(e) and attempt < max_retries:
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue
            print(f"[lrclib] GET {path} failed: {type(e).__name__}: {e}")
            return None
    print(f"[lrclib] GET {path} gave up after {max_retries + 1} attempts: {last_err}")
    return None


def _lrclib_lookup_one(t: dict) -> dict:
    """Look up lyrics for one track in LRCLIB.

    Returns a result dict with the shape used by lookup_lyrics_for_tracks().
    Never raises — every failure mode becomes an entry with `available=False`
    and a human-readable `error` so the UI can show *why* LRCLIB didn't help.
    """
    import urllib.error

    title = (t.get("title") or "").strip()
    artist = (t.get("artist") or t.get("album_artist") or "").strip()
    album = (t.get("album") or "").strip()
    duration_ms = int(t.get("duration_ms") or 0)
    duration_s = duration_ms / 1000.0 if duration_ms > 0 else 0.0

    base = {
        "track_id": t.get("id"),
        "title": title,
        "artist": artist,
        "lyrics": {
            "available": False,
            "source": None,
            "synced": False,
            "instrumental": False,
            "language": "",
            "lrc_text": "",
            "preview": "",
            "lrclib_id": None,
            "fetched_at": "",
            "error": "",
        },
    }
    if not title or not artist:
        base["lyrics"]["error"] = "missing title or artist"
        return base

    # Try /api/get first with full metadata — LRCLIB's best-match endpoint
    # uses duration (±2s tolerance) to disambiguate same-titled tracks.
    params: dict = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration_s > 0:
        params["duration"] = f"{duration_s:.1f}"

    rec = None
    try:
        rec = _lrclib_get_request("/api/get", params)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            base["lyrics"]["error"] = f"LRCLIB /api/get HTTP {e.code}"
            return base
        # 404 → fall through to /api/search below
    except Exception as e:
        base["lyrics"]["error"] = f"{type(e).__name__}: {str(e)[:140]}"
        return base

    # If /api/get failed entirely (transport error), try /api/search as backup
    if rec is None:
        try:
            results = _lrclib_get_request("/api/search", {
                "track_name": title, "artist_name": artist})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                base["lyrics"]["error"] = "no LRCLIB match"
            else:
                base["lyrics"]["error"] = f"LRCLIB /api/search HTTP {e.code}"
            return base
        except Exception as e:
            base["lyrics"]["error"] = f"{type(e).__name__}: {str(e)[:140]}"
            return base
        if not results:
            base["lyrics"]["error"] = "no LRCLIB match"
            return base
        # Pick first non-instrumental record; fall back to first record if all
        # are instrumental.
        rec = None
        for r in results:
            if not r.get("instrumental"):
                rec = r
                break
        if rec is None:
            rec = results[0]

    # Extract fields from the LRCLIB record
    synced = (rec.get("syncedLyrics") or "").strip()
    plain = (rec.get("plainLyrics") or "").strip()
    instrumental = bool(rec.get("instrumental"))
    lang = (rec.get("language") or "").strip() or ""
    lrclib_id = rec.get("id")
    track_name = (rec.get("trackName") or rec.get("name") or "").strip()
    artist_name = (rec.get("artistName") or "").strip()

    if instrumental and not synced and not plain:
        # LRCLIB says: this track is instrumental, no lyrics exist
        base["lyrics"].update({
            "available": True,
            "source": "lrclib",
            "instrumental": True,
            "language": lang,
            "lrc_text": "",
            "preview": "(instrumental)",
            "lrclib_id": lrclib_id,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "error": "",
        })
        return base

    if not synced and not plain:
        base["lyrics"]["error"] = "LRCLIB record has no lyrics fields"
        return base

    if synced:
        lrc_text = synced
        synced_flag = True
    else:
        # LRCLIB only returned plainLyrics. Wrap as a single block at [00:00.00]
        # so the UI still shows something and Jellyfin can render the text.
        # Whisper will still be offered as an alternative for synced timing.
        lrc_text = "[00:00.00]" + plain.replace("\n", " \u23ce ") + "\n"
        synced_flag = False

    base["lyrics"].update({
        "available": True,
        "source": "lrclib",
        "synced": synced_flag,
        "instrumental": instrumental,
        "language": lang,
        "lrc_text": lrc_text,
        "preview": _preview_from_lrc(lrc_text),
        "lrclib_id": lrclib_id,
        "lrclib_track": track_name,
        "lrclib_artist": artist_name,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "error": "",
    })
    return base


def _preview_from_lrc(lrc_text: str, max_chars: int = 240) -> str:
    if not lrc_text:
        return ""
    # Strip timestamp tags for the preview
    flat = re.sub(r"\[\d{2}:\d{2}(?::\d{2})?\.\d+\]", "", lrc_text)
    flat = re.sub(r"\s+", " ", flat).strip()
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rstrip() + "\u2026"


def lookup_lyrics_for_tracks(tracks: list[dict], progress_cb=None) -> list[dict]:
    """Look up synced lyrics for each track via LRCLIB.

    `progress_cb(done, total, track)` may be called from a thread.

    Returns a list of:
      {
        'track_id', 'title', 'artist',
        'lyrics': {
            'available': bool,
            'source':   'lrclib' | None,
            'synced':   bool,           # true if syncedLyrics was present
            'instrumental': bool,
            'language': 'en'|...|'',
            'lrc_text': '...',          # full LRC body when available
            'preview':  '...',          # first ~240 chars for the UI
            'lrclib_id': int|None,
            'fetched_at': ISO string,
            'error':    '' (or short reason)
        }
      }
    """
    results: list[dict] = []
    total = len(tracks)
    for idx, t in enumerate(tracks):
        if progress_cb:
            try:
                progress_cb(idx, total, t)
            except Exception:
                pass
        results.append(_lrclib_lookup_one(t))
    if progress_cb:
        try:
            progress_cb(total, total, None)
        except Exception:
            pass
    return results


# ---------------------------------------------------------------- lyrics lookup: background state

# Module-level state for asynchronous lyrics-lookup jobs. The endpoint in
# app.py returns immediately with a job_id; the client polls the status
# endpoint. Each entry holds:
#   status:   "running" | "done" | "error"
#   done:     int — tracks looked up so far
#   total:    int — total tracks
#   current:  str — title currently being looked up (for UI feedback)
#   results:  list[dict|None] — index-aligned lookup results, None until done
#   error:    str — top-level error if status == "error"
#   started:  iso timestamp
#   finished: iso timestamp (when status is terminal)
#   track_order: list[track_id] — used to preserve input order in the response
_lookup_jobs: dict[str, dict] = {}
_lookup_jobs_lock = threading.Lock()
_LOOKUP_RESULTS_TTL_S = 30 * 60  # drop finished lookup state after 30 min


def _lookup_one_with_retry(t: dict, max_retries: int = 3) -> dict:
    """Look up one track's lyrics; retry with exponential backoff on 429."""
    backoff = 2.0
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return _lrclib_lookup_one(t)
        except Exception as e:
            last_exc = e
            if _is_rate_limit_error(e) and attempt < max_retries:
                print(f"[lrclib] rate-limit on '{t.get('title', '')}', "
                      f"backing off {backoff:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue
            raise
    raise last_exc  # pragma: no cover


def lyrics_lookup_job_worker(lookup_id: str, tracks: list[dict],
                             jobs_dict: dict, jobs_lock) -> None:
    """Background worker for the lookup-lyrics endpoint.

    Writes progress into jobs_dict[lookup_id] under jobs_lock. The HTTP layer
    reads from there on poll. Concurrency is intentionally capped (4 workers)
    — LRCLIB is a public service without rate-limit guarantees, so we keep
    per-track concurrency modest.
    """
    import concurrent.futures as _cf

    total = len(tracks)
    with jobs_lock:
        st = jobs_dict.get(lookup_id)
        if st is None:
            return
        st["status"] = "running"
        st["done"] = 0
        st["total"] = total
        st["current"] = ""
        st["results"] = [None] * total
        st["error"] = ""
        st["track_order"] = [str(t.get("id") or "") for t in tracks]
        st["started"] = datetime.now().isoformat(timespec="seconds")
        st["finished"] = ""

    def _do_one(idx_and_t):
        idx, t = idx_and_t
        try:
            r = _lookup_one_with_retry(t)
        except Exception as e:
            r = {
                "track_id": t.get("id"),
                "title": t.get("title"),
                "artist": t.get("artist") or t.get("album_artist") or "",
                "lyrics": {
                    "available": False,
                    "source": None,
                    "synced": False,
                    "instrumental": False,
                    "language": "",
                    "lrc_text": "",
                    "preview": "",
                    "lrclib_id": None,
                    "fetched_at": "",
                    "error": f"{type(e).__name__}: {str(e)[:160]}",
                },
            }
        # Fill in track metadata in case lookup failed before populating it.
        r.setdefault("track_id", t.get("id"))
        r.setdefault("title", t.get("title"))
        r.setdefault("artist", t.get("artist") or t.get("album_artist") or "")
        return idx, r

    try:
        with _cf.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_do_one, (i, t)): i for i, t in enumerate(tracks)}
            for fut in _cf.as_completed(futures):
                idx = futures[fut]
                try:
                    idx, r = fut.result(timeout=60)
                except Exception as e:
                    t = tracks[idx]
                    r = {
                        "track_id": t.get("id"),
                        "title": t.get("title"),
                        "artist": t.get("artist") or t.get("album_artist") or "",
                        "lyrics": {
                            "available": False, "source": None, "synced": False,
                            "instrumental": False, "language": "",
                            "lrc_text": "", "preview": "", "lrclib_id": None,
                            "fetched_at": "",
                            "error": f"{type(e).__name__}: {str(e)[:160]}",
                        },
                    }
                with jobs_lock:
                    st = jobs_dict.get(lookup_id)
                    if st is None:
                        return
                    st["results"][idx] = r
                    st["done"] += 1
                    st["current"] = (r.get("title") or "")
        with jobs_lock:
            st = jobs_dict.get(lookup_id)
            if st is not None:
                st["status"] = "done"
                st["current"] = ""
                st["finished"] = datetime.now().isoformat(timespec="seconds")
    except Exception as e:
        with jobs_lock:
            st = jobs_dict.get(lookup_id)
            if st is not None:
                st["status"] = "error"
                st["error"] = f"{type(e).__name__}: {e}"
                st["finished"] = datetime.now().isoformat(timespec="seconds")


def cleanup_lookup_state(jobs_dict: dict, jobs_lock, now: float | None = None) -> int:
    """Drop finished lookup entries older than _LOOKUP_RESULTS_TTL_S. Returns count removed."""
    if now is None:
        now = time.time()
    cutoff = now - _LOOKUP_RESULTS_TTL_S
    removed = 0
    with jobs_lock:
        for k in list(jobs_dict.keys()):
            st = jobs_dict[k]
            if st.get("kind") != "lyrics-lookup":
                continue
            if st.get("status") not in ("done", "error"):
                continue
            try:
                finished = datetime.fromisoformat(st.get("finished") or
                                                  st.get("started") or "")
            except Exception:
                continue
            ts = finished.timestamp()
            if ts < cutoff:
                del jobs_dict[k]
                removed += 1
    return removed


# ---------------------------------------------------------------- REPAIR — scan & fix broken .lrc files
#
# The REPAIR tab walks the user's configured music folder, finds every
# MP3 that has a sibling .lrc, and classifies each .lrc as healthy or
# broken. A "broken" .lrc is one that contains a known placeholder
# string ("[00:00.00]Lyrics unavailable"), is suspiciously short
# (< 50 bytes), or has zero timestamp lines.
#
# Fix action: re-fetch from LRCLIB using the MP3's ID3 tags and write
# the new content back. No Whisper fallback (per user choice).


# Heuristics for a "broken" .lrc. Kept conservative so we don't
# auto-flag a perfectly good file by mistake.
_LRC_PLACEHOLDER_PATTERNS = (
    "[00:00.00]lyrics unavailable",
    "[00:00.00]lyrics not available",
    "[00:00.00]no lyrics",
    "[00:00.00]transcription failed",
    "[ar:]\n[ti:]\n[al:]",
    "[00:00.00]instrumental",
)
import re as _re


def _lrc_is_broken(text: str) -> bool:
    if not text or not text.strip():
        return True
    stripped = text.strip().lower()
    if len(stripped) < 50:
        return True
    for pat in _LRC_PLACEHOLDER_PATTERNS:
        if pat in stripped:
            return True
    # An .lrc with zero timestamp lines ([mm:ss.xx]) is content-free.
    if not _re.search(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]", text):
        return True
    return False


def _read_id3_tags(mp3_path: Path) -> dict:
    """Best-effort artist/title extraction from an MP3's ID3 tags."""
    try:
        from mutagen.id3 import ID3
        tags = ID3(str(mp3_path))
        title = ""
        artist = ""
        for key in ("TIT2", "TIT1", "TITLE"):
            if key in tags:
                title = str(tags[key])
                break
        for key in ("TPE1", "TPE2", "ARTIST", "TPE3"):
            if key in tags:
                artist = str(tags[key])
                break
        return {"title": title, "artist": artist}
    except Exception:
        return {"title": "", "artist": ""}


def scan_music_folder(music_folder: str, *, max_files: int = 5000,
                       progress_cb=None) -> list[dict]:
    """Walk music_folder recursively. Return one entry per MP3+LRC pair.

    Each entry: {mp3, lrc, status, reason, title, artist, size_mp3,
                 size_lrc, mtime_lrc, mp3_relpath, lrc_relpath,
                 album_key, album_display}
    `status` is one of: 'broken', 'healthy', 'no_lrc'.
    """
    root = Path(music_folder)
    out: list[dict] = []
    if not root.exists() or not root.is_dir():
        return [{"error": f"music folder not found: {music_folder}"}]

    mp3_files = []
    try:
        for path in root.rglob("*.mp3"):
            try:
                ok = path.is_file()
            except OSError:
                ok = False
            if ok:
                mp3_files.append(path)
            if len(mp3_files) >= max_files:
                break
    except Exception as e:
        return [{"error": f"scan failed: {type(e).__name__}: {e}"}]

    total = len(mp3_files)
    for i, mp3 in enumerate(mp3_files):
        if progress_cb:
            try:
                progress_cb(i, total, mp3)
            except Exception:
                pass
        lrc = mp3.with_suffix(".lrc")
        rel_mp3 = str(mp3.relative_to(root)) if mp3.is_relative_to(root) else str(mp3)
        rel_lrc = str(lrc.relative_to(root)) if lrc.is_relative_to(root) else str(lrc)
        try:
            album_key = str(mp3.parent.relative_to(root)) if mp3.parent.is_relative_to(root) else str(mp3.parent)
            album_display = album_key.replace("\\", " / ")
        except Exception:
            album_key = str(mp3.parent)
            album_display = album_key
        try:
            size_mp3 = mp3.stat().st_size
            mtime_lrc = 0
            size_lrc = 0
            text = ""
            if lrc.exists():
                size_lrc = lrc.stat().st_size
                mtime_lrc = lrc.stat().st_mtime
                text = lrc.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            out.append({"mp3": str(mp3), "lrc": str(lrc),
                        "mp3_relpath": rel_mp3, "lrc_relpath": rel_lrc,
                        "album_key": album_key,
                        "album_display": album_display,
                        "status": "error", "reason": f"{type(e).__name__}: {e}"})
            continue

        if not lrc.exists():
            out.append({
                "mp3": str(mp3), "lrc": "",
                "mp3_relpath": rel_mp3, "lrc_relpath": rel_lrc,
                "album_key": album_key,
                "album_display": album_display,
                "status": "no_lrc", "reason": "",
                "title": "", "artist": "",
                "size_mp3": size_mp3, "size_lrc": 0, "mtime_lrc": 0,
            })
            continue

        tags = _read_id3_tags(mp3)
        # Distinguish a few "broken" shapes so the Repair UI can tell the
        # user what's actually wrong with each file. Empty files are
        # almost always a half-finished write (interrupted download,
        # crashed previous run) — they need a manual upload, not another
        # LRCLIB lookup. Placeholder files are recoverable via LRCLIB.
        if size_lrc == 0:
            broken = True
            reason = "empty file (0 bytes)"
        elif not text.strip():
            broken = True
            reason = "empty content"
        else:
            broken = _lrc_is_broken(text)
            reason = "placeholder_or_no_timestamps" if broken else ""
        out.append({
            "mp3": str(mp3),
            "lrc": str(lrc),
            "mp3_relpath": rel_mp3,
            "lrc_relpath": rel_lrc,
            "album_key": album_key,
            "album_display": album_display,
            "status": "broken" if broken else "healthy",
            "reason": reason,
            "title": tags["title"],
            "artist": tags["artist"],
            "size_mp3": size_mp3,
            "size_lrc": size_lrc,
            "mtime_lrc": mtime_lrc,
        })

    if progress_cb:
        try:
            progress_cb(total, total, None)
        except Exception:
            pass
    return out


def repair_lrc_file(mp3_path: str) -> dict:
    """Re-fetch lyrics for one MP3 from LRCLIB and overwrite its .lrc.

    Returns {ok, mp3, lrc, lrc_text, source, error}.
    On success the file is overwritten with real lyrics.
    On not-found the .lrc file is left alone (we don't delete it —
    that would lose any existing legitimate data and break the
    user-defined semantics).
    """
    mp3 = Path(mp3_path)
    if not mp3.exists():
        return {"ok": False, "error": "mp3 not found", "mp3": mp3_path}
    tags = _read_id3_tags(mp3)
    title = tags["title"]
    artist = tags["artist"]
    if not (title and artist):
        return {"ok": False, "error": "no id3 tags (title/artist missing)",
                "mp3": mp3_path, "title": title, "artist": artist}

    # Reuse the existing LRCLIB lookup path. _lrclib_lookup_one takes
    # a dict with 'title' + 'artist'.
    res = _lrclib_lookup_one({"title": title, "artist": artist,
                               "album": "", "duration_ms": 0})
    lyrics = res.get("lyrics") or {}
    if not lyrics.get("available"):
        return {"ok": False, "error": lyrics.get("error") or "no lyrics on LRCLIB",
                "mp3": mp3_path, "title": title, "artist": artist,
                "lrclib_id": lyrics.get("lrclib_id")}

    lrc_text = lyrics.get("lrc_text") or ""
    if not lrc_text.strip():
        return {"ok": False, "error": "lrclib returned empty lyrics",
                "mp3": mp3_path, "title": title, "artist": artist}

    lrc_path = mp3.with_suffix(".lrc")
    try:
        # Atomic-ish write: temp + replace.
        tmp = lrc_path.with_suffix(".lrc.tmp")
        tmp.write_text(lrc_text, encoding="utf-8")
        tmp.replace(lrc_path)
    except Exception as e:
        return {"ok": False, "error": f"lrc write: {type(e).__name__}: {e}",
                "mp3": mp3_path}

    return {
        "ok": True,
        "mp3": mp3_path,
        "lrc": str(lrc_path),
        "lrc_text": lrc_text,
        "source": lyrics.get("source") or "lrclib",
        "title": title,
        "artist": artist,
        "synced": lyrics.get("synced", False),
        "instrumental": lyrics.get("instrumental", False),
        "lrclib_id": lyrics.get("lrclib_id"),
    }


# ---------------------------------------------------------------- write-lrc + path safety
#
# Used by the REPAIR tab's manual lyrics recovery. The .lrc file is
# written atomically (temp + replace) and tagged with a source comment
# line so future scans can distinguish user-supplied lyrics from
# auto-fetched ones.


_LRC_SOURCE_PREFIX = "; source: "


def _strip_source_comment(text: str) -> tuple[str, str | None]:
    """Strip the leading "; source: …" comment if present. Returns
    (clean_text, source_or_None). Lines starting with ';' are treated
    as comments.
    """
    source = None
    lines = text.splitlines()
    while lines and lines[0].strip().startswith(";"):
        head = lines[0].strip()
        if head.startswith(_LRC_SOURCE_PREFIX):
            source = head[len(_LRC_SOURCE_PREFIX):].strip()
        lines.pop(0)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), source


def _strip_text(text: str) -> str:
    """Strip our '; source: …' audit comment if present, then trim."""
    cleaned, _ = _strip_source_comment(text)
    return cleaned.strip()


def _is_inside_music_folder(path: str | Path) -> bool:
    """True if `path` resolves to a child of the configured music folder.
    Used as a safety check before any file write or delete on the
    server. Returns False if the music folder isn't configured.
    """
    try:
        prof = load_profile()
    except Exception:
        return False
    root = prof.music_folder or ""
    if not root:
        return False
    try:
        rp = Path(root).resolve()
        pp = Path(path).resolve()
        # Path.is_relative_to exists in 3.9+
        return pp.is_relative_to(rp)
    except Exception:
        return False


def write_lrc_text(mp3_path: str, lrc_text: str, *, source: str = "manual") -> dict:
    """Write a user-supplied .lrc file atomically and tag it with
    `; source: <source>` so the broken-classifier ignores it and
    future audits can trace provenance.

    Returns {ok, mp3, lrc, size_lrc, source} or {ok: False, error}.
    """
    mp3 = Path(mp3_path)
    if not mp3.exists():
        return {"ok": False, "error": "mp3 not found", "mp3": mp3_path}
    if not _is_inside_music_folder(mp3):
        return {"ok": False, "error": "mp3 path is outside the configured music folder",
                "mp3": mp3_path}
    if not lrc_text or not lrc_text.strip():
        return {"ok": False, "error": "lrc_text is empty", "mp3": mp3_path}
    lrc_path = mp3.with_suffix(".lrc")
    tags = _read_id3_tags(mp3)
    # Prepend the source comment if not already present.
    head = f"{_LRC_SOURCE_PREFIX}{source}\n"
    existing = ""
    try:
        if lrc_path.exists():
            existing = lrc_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        existing = ""
    cleaned_existing, existing_source = _strip_source_comment(existing)
    final_text = head + (cleaned_existing or lrc_text)
    # If the existing source comment is identical, don't double it.
    if existing_source == source and cleaned_existing == lrc_text:
        final_text = existing  # no-op write
    try:
        tmp = lrc_path.with_suffix(".lrc.tmp")
        tmp.write_text(final_text, encoding="utf-8")
        tmp.replace(lrc_path)
    except Exception as e:
        return {"ok": False, "error": f"lrc write: {type(e).__name__}: {e}",
                "mp3": mp3_path}
    return {
        "ok": True,
        "mp3": mp3_path,
        "lrc": str(lrc_path),
        "size_lrc": len(final_text.encode("utf-8")),
        "source": source,
        "title": tags["title"],
        "artist": tags["artist"],
    }


# ---------------------------------------------------------------- LRCLIB search wrapper
#
# Used by the REPAIR tab's "Manual search" tab — proxy to LRCLIB
# /api/search?q=… and normalize each hit's `syncedLyrics` /
# `plainLyrics` into the same `lrc_text` shape `write_lrc_text`
# expects.


def _lrclib_search(q: str, limit: int = 20) -> dict:
    """Hit LRCLIB's full-text search and return the candidate list.
    The response shape is documented at https://lrclib.net/api.
    """
    if not q or not q.strip():
        return {"ok": False, "error": "empty query"}
    url = f"{LRCLIB_BASE}/api/search"
    try:
        r = requests.get(
            url,
            params={"q": q.strip()},
            headers={"User-Agent": LRCLIB_USER_AGENT, "Accept": "application/json"},
            timeout=15,
        )
    except Exception as e:
        return {"ok": False, "error": f"lrclib: {type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"lrclib http {r.status_code}"}
    try:
        rows = r.json()
    except Exception as e:
        return {"ok": False, "error": f"lrclib: bad json: {e}"}
    if not isinstance(rows, list):
        return {"ok": False, "error": "lrclib: unexpected response shape"}

    out = []
    for row in rows[:max(1, min(limit, 50))]:
        synced = (row.get("syncedLyrics") or "").strip()
        plain = (row.get("plainLyrics") or "").strip()
        out.append({
            "lrclib_id": row.get("id"),
            "name": row.get("name") or "",
            "trackName": row.get("trackName") or row.get("name") or "",
            "artistName": row.get("artistName") or "",
            "albumName": row.get("albumName") or "",
            "duration_ms": int(round((row.get("duration") or 0) * 1000)),
            "instrumental": bool(row.get("instrumental")),
            "synced": bool(synced),
            "lrc_text": synced or plain,
        })
    return {"ok": True, "results": out, "query": q}


# ---------------------------------------------------------------- Deduper — within-album duplicate songs
#
# Flags MP3s whose titles collide inside the same album folder, plus
# normalized-title near-duplicates. Used by the REPAIR tab's Deduper
# sub-panel.

import re as _re_dedupe


def _norm_for_dedupe(title: str) -> str:
    """Looser than providers.norm — also strips common version markers
    so 'Lady (Live)' and 'Lady' collide, while keeping all other
    characters ASCII-folded.
    """
    s = (title or "").strip().lower()
    # Drop parenthetical and bracketed variants like '(Live)', '[2011 remaster]'.
    s = _re_dedupe.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)
    # Drop trailing version / edition tags.
    s = _re_dedupe.sub(
        r"\b(remastered|remaster|re|mono|stereo|edit|version|"
        r"single|radio edit|album version|live|acoustic|remix|"
        r"demo|instrumental|bonus track|explicit|clean)\b",
        "", s,
    )
    # Apply providers.norm's canonicalization (apostrophes, dashes, etc.)
    from providers import norm as _providers_norm
    s = _providers_norm(s)
    s = _re_dedupe.sub(r"\s+", " ", s).strip()
    return s


# send2trash is preferred for safe Recycle-Bin-style deletes. If it
# isn't installed, refuse to delete and ask the user to install it
# (deleting files permanently via unlink() is too dangerous).
try:
    import send2trash as _send2trash  # type: ignore
    _HAVE_SEND2TRASH = True
except Exception:
    _send2trash = None
    _HAVE_SEND2TRASH = False


def send2trash_available() -> bool:
    return _HAVE_SEND2TRASH


def _trash_file(path: Path) -> tuple[bool, str | None]:
    """Move `path` to the OS Recycle Bin (or refuse if send2trash missing)."""
    if not _HAVE_SEND2TRASH:
        return False, "send2trash not installed; pip install send2trash"
    try:
        _send2trash.send2trash(str(path))
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _load_dedupe_skips() -> dict:
    p = data_file("dedupe_skipped.json")
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_dedupe_skips(d: dict) -> None:
    p = data_file("dedupe_skipped.json")
    try:
        p.write_text(json.dumps(d, indent=0), encoding="utf-8")
    except Exception:
        pass


def dedupe_album(music_folder: str, *, max_files: int = 5000) -> list[dict]:
    """Walk music_folder, group MP3s by parent folder + normalized
    title, and return one entry per album-group with len(songs) >= 2.

    Output: [{album_key, album_display, groups: [{norm_title,
    reason, songs: [{mp3, mp3_relpath, title, duration_ms,
    track_number}]}]}]
    """
    root = Path(music_folder)
    if not root.exists() or not root.is_dir():
        return []
    mp3_files: list[Path] = []
    try:
        for p in root.rglob("*.mp3"):
            try:
                if p.is_file():
                    mp3_files.append(p)
            except OSError:
                pass
            if len(mp3_files) >= max_files:
                break
    except Exception:
        return []

    skips = _load_dedupe_skips()

    by_album: dict[str, dict] = {}
    for mp3 in mp3_files:
        try:
            ak = str(mp3.parent.relative_to(root))
        except Exception:
            ak = str(mp3.parent)
        tags = _read_id3_tags(mp3)
        title = (tags.get("title") or mp3.stem).strip()
        norm = _norm_for_dedupe(title)
        if not norm:
            continue
        songs = by_album.setdefault(ak, {"album_display": ak.replace("\\", " / "), "groups": {}})
        g = songs["groups"].setdefault(norm, {"norm_title": norm, "songs": []})
        g["songs"].append({
            "mp3": str(mp3),
            "mp3_relpath": str(mp3.relative_to(root)) if mp3.is_relative_to(root) else str(mp3),
            "title": title,
            "duration_ms": 0,
            "track_number": 0,
        })

    out: list[dict] = []
    for ak, info in by_album.items():
        skip_key_prefix = f"{ak}::"
        groups_out = []
        for norm_title, g in info["groups"].items():
            if len(g["songs"]) < 2:
                continue
            # Skip groups the user already marked as "Skip".
            skip_match = False
            for k, v in skips.items():
                if not k.startswith(skip_key_prefix):
                    continue
                if set(v.get("mp3_paths", [])) & set(s["mp3"] for s in g["songs"]):
                    skip_match = True
                    break
            if skip_match:
                continue
            # Decide reason: exact-title vs near-title
            titles = {s["title"] for s in g["songs"]}
            norm_titles = {norm_title}  # all share this by construction
            reason = "exact_title_match" if len(titles) == 1 else "near_title_match"
            groups_out.append({
                "norm_title": norm_title,
                "reason": reason,
                "songs": sorted(g["songs"], key=lambda s: s["mp3"]),
            })
        if groups_out:
            out.append({
                "album_key": ak,
                "album_display": info["album_display"],
                "groups": groups_out,
            })
    # Sort albums by display name so output is stable across scans.
    out.sort(key=lambda x: x["album_key"].lower())
    return out


def dedupe_action(action: str, paths: list[str], music_folder: str) -> dict:
    """Apply Delete (trash) or Skip (write to dedupe_skipped.json) over
    the given MP3 paths.
    """
    if action not in ("delete", "skip"):
        return {"ok": False, "error": "action must be 'delete' or 'skip'"}
    if not isinstance(paths, list) or not paths:
        return {"ok": False, "error": "paths must be a non-empty list"}
    results = []
    if action == "delete":
        if not _HAVE_SEND2TRASH:
            return {"ok": False, "error":
                    "send2trash not installed. Run: pip install send2trash",
                    "results": [], "summary": {"deleted": 0, "failed": 0}}
        for p in paths:
            entry = {"path": p, "ok": False}
            mp3 = Path(p)
            if not _is_inside_music_folder(mp3):
                entry["error"] = "outside music folder"
                results.append(entry)
                continue
            ok, err = _trash_file(mp3)
            if ok:
                # Trash sibling .lrc too if it exists.
                lrc = mp3.with_suffix(".lrc")
                if lrc.exists():
                    _trash_file(lrc)
                entry["ok"] = True
            else:
                entry["error"] = err
            results.append(entry)
        ok_count = sum(1 for r in results if r["ok"])
        return {"ok": True, "summary": {"deleted": ok_count,
                                          "failed": len(results) - ok_count},
                "results": results}

    # action == "skip": persist paths so the next scan filters them out.
    skips = _load_dedupe_skips()
    for p in paths:
        mp3 = Path(p)
        try:
            ak = str(mp3.parent.relative_to(Path(music_folder))) if mp3.parent.is_relative_to(Path(music_folder)) else str(mp3.parent)
        except Exception:
            ak = str(mp3.parent)
        # Key by (album_key, norm_title) using the file's own title.
        tags = _read_id3_tags(mp3)
        norm = _norm_for_dedupe(tags.get("title") or mp3.stem)
        key = f"{ak}::{norm}"
        skips[key] = {
            "mp3_paths": [p],
            "skipped_at": datetime.now().isoformat(timespec="seconds"),
        }
        results.append({"path": p, "ok": True})
    _save_dedupe_skips(skips)
    return {"ok": True, "summary": {"skipped": len(results)},
            "results": results}


# ---------------------------------------------------------------- Renamer


def _safe_filename(s: str, max_len: int = 120) -> str:
    """Sanitize a string for use as an MP3 basename on Windows.

    Strips characters Windows disallows in filenames and trims trailing
    dots/spaces. Does NOT split on backslash because callers already pass
    only the basename component.
    """
    if s is None:
        return ""
    # Strip control chars + Windows-illegal chars: <>:"/\\|?* plus 0-31.
    out = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", s).strip()
    # Windows also dislikes reserved trailing chars and trailing spaces/dots.
    out = out.rstrip(" .")
    if len(out) > max_len:
        out = out[:max_len].rstrip(" .")
    return out or "untitled"


def _parse_track_number(value) -> str:
    """Pull a leading integer out of an ID3 TRCK tag value.

    ID3 TRCK is sometimes '3' and sometimes '3/12'. Return the left side
    as a string, or '' if it isn't an integer.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    head = s.split("/", 1)[0].strip()
    if head.isdigit():
        return head
    return ""


def _read_id3_tags_full(mp3_path: Path) -> dict:
    """Read title / artist / track number / album from ID3 (or fallback to
    filename heuristics when tags are absent).

    The Renamer uses this as its source of truth for what a file "should"
    be named. Missing track numbers degrade to '' (and the file is then
    only a collision candidate if its sibling has a number — a soft
    signal that there's drift in the album).
    """
    title, artist, album, track = "", "", "", ""
    try:
        from mutagen.id3 import ID3
        tags = ID3(str(mp3_path))
        for key in ("TIT2", "TIT1", "TITLE"):
            if key in tags:
                title = str(tags[key])
                break
        for key in ("TPE1", "TPE2", "ARTIST", "TPE3"):
            if key in tags:
                artist = str(tags[key])
                break
        for key in ("TALB", "ALBUM"):
            if key in tags:
                album = str(tags[key])
                break
        for key in ("TRCK", "TRK", "TRACKNUMBER"):
            if key in tags:
                track = _parse_track_number(tags[key])
                break
    except Exception:
        pass
    return {"title": title, "artist": artist, "album": album,
            "track_number": track}


def scan_renames(music_folder: str, *, max_files: int = 5000) -> dict:
    """Walk the music folder and return two parallel lists:

    * ``misnamed`` — files whose current basename doesn't match the
      ID3-derived canonical name ``"<track> - <title>.mp3"``.
    * ``collisions`` — album folders where two or more MP3s share the
      same ID3 track number (e.g. 1, 2, 2, 3) or where track numbers
      are out of range / missing. Each collision entry includes a
      proposed sequential renumbering the user can approve.

    Returns ``{ok, music_folder, misnamed, collisions, counts}``.
    """
    root = Path(music_folder)
    out_misnamed: list[dict] = []
    out_collisions: list[dict] = []
    if not root.exists() or not root.is_dir():
        return {"ok": False, "error": f"music folder not found: {music_folder}",
                "music_folder": music_folder, "misnamed": [],
                "collisions": [], "counts": {}}

    mp3_files: list[Path] = []
    try:
        for path in root.rglob("*.mp3"):
            try:
                if path.is_file():
                    mp3_files.append(path)
            except OSError:
                continue
            if len(mp3_files) >= max_files:
                break
    except Exception as e:
        return {"ok": False, "error": f"scan failed: {type(e).__name__}: {e}",
                "music_folder": music_folder, "misnamed": [],
                "collisions": [], "counts": {}}

    # Group by parent dir (= album folder).
    by_album: dict[str, list[Path]] = {}
    for mp3 in mp3_files:
        try:
            ak = str(mp3.parent.relative_to(root))
        except ValueError:
            ak = str(mp3.parent)
        by_album.setdefault(ak, []).append(mp3)

    for album_key, paths in by_album.items():
        # First pass: collect (path, tags, suggested_basename)
        entries: list[dict] = []
        for mp3 in paths:
            tags = _read_id3_tags_full(mp3)
            if not tags["title"]:
                # Without a title we can't propose a name. Skip — the
                # collision pass below may still flag this file if its
                # track number conflicts with a sibling.
                entries.append({"mp3": mp3, "tags": tags,
                                "suggested": "", "current": mp3.name})
                continue
            track = tags["track_number"] or ""
            base = _safe_filename(tags["title"]) + ".mp3"
            if track:
                suggested = f"{int(track):02d} - {base}"
            else:
                suggested = base
            entries.append({"mp3": mp3, "tags": tags,
                            "suggested": suggested,
                            "current": mp3.name})

        # Misnamed: file's current basename doesn't equal the suggested one.
        # We tolerate leading zeros: "3 - foo.mp3" matches "03 - foo.mp3".
        for e in entries:
            if not e["suggested"]:
                continue
            if _basename_eq_loose(e["current"], e["suggested"]):
                continue
            mp3 = e["mp3"]
            new_path = mp3.with_name(e["suggested"])
            out_misnamed.append({
                "mp3": str(mp3),
                "current": e["current"],
                "suggested": e["suggested"],
                "new_path": str(new_path),
                "title": e["tags"]["title"],
                "artist": e["tags"]["artist"],
                "album": e["tags"]["album"],
                "track_number": e["tags"]["track_number"],
                "album_key": album_key,
                "reason": _why_rename(e["current"], e["suggested"]),
            })

        # Collisions: two or more mp3s in this album share the same
        # non-empty track number, OR the track numbers are missing /
        # out-of-range. Build a proposed sequential renumbering.
        track_groups: dict[str, list[dict]] = {}
        for e in entries:
            tn = e["tags"]["track_number"]
            if tn:
                track_groups.setdefault(tn, []).append(e)
        dup_tracks = {k: v for k, v in track_groups.items() if len(v) > 1}
        if dup_tracks:
            # Sort each duplicate group by current filename so the
            # ordering is stable across rescans.
            collision_rows = []
            for tn, group in dup_tracks.items():
                group_sorted = sorted(group, key=lambda g: g["current"].lower())
                collision_rows.append({
                    "track_number": tn,
                    "count": len(group_sorted),
                    "current_names": [g["current"] for g in group_sorted],
                    "mp3s": [str(g["mp3"]) for g in group_sorted],
                })
            # Proposed renumbering: sort every mp3 in the album by
            # (track_number, current filename), drop gaps, and assign
            # 1..N. Each entry includes the new basename if it would
            # change.
            sorted_album = sorted(
                entries,
                key=lambda e: (
                    int(e["tags"]["track_number"]) if e["tags"]["track_number"] else 9999,
                    e["current"].lower(),
                ),
            )
            proposed = []
            for i, e in enumerate(sorted_album, start=1):
                new_num = f"{i:02d}"
                title = e["tags"]["title"] or Path(e["current"]).stem
                safe = _safe_filename(title) + ".mp3"
                new_basename = f"{new_num} - {safe}"
                new_path = e["mp3"].with_name(new_basename)
                proposed.append({
                    "mp3": str(e["mp3"]),
                    "current": e["current"],
                    "new_basename": new_basename,
                    "new_path": str(new_path),
                    "track_old": e["tags"]["track_number"],
                    "track_new": str(i),
                    "title": e["tags"]["title"],
                })
            out_collisions.append({
                "album_key": album_key,
                "duplicate_tracks": collision_rows,
                "proposed": proposed,
            })

    return {
        "ok": True,
        "music_folder": str(root),
        "misnamed": out_misnamed,
        "collisions": out_collisions,
        "counts": {
            "mp3_total": len(mp3_files),
            "albums_total": len(by_album),
            "misnamed": len(out_misnamed),
            "collisions": len(out_collisions),
        },
    }


def _basename_eq_loose(a: str, b: str) -> bool:
    """Case-insensitive comparison that ignores leading zero on the
    track number, so '03 - Foo.mp3' == '3 - Foo.mp3'."""
    if not a or not b:
        return False
    if a.lower() == b.lower():
        return True
    # Strip a leading NN - prefix and compare the remainder.
    ra = re.sub(r"^\d+\s*-\s*", "", a)
    rb = re.sub(r"^\d+\s*-\s*", "", b)
    return ra.lower() == rb.lower()


def _why_rename(current: str, suggested: str) -> str:
    if not current:
        return "missing basename"
    cur_num = re.match(r"^(\d+)\s*-", current)
    sug_num = re.match(r"^(\d+)\s*-", suggested)
    if cur_num and sug_num and cur_num.group(1).lstrip("0") != sug_num.group(1).lstrip("0"):
        return "track number changed"
    return "title changed"


def apply_renames(actions: list[dict], music_folder: str) -> dict:
    """Apply a batch of rename actions. Each ``actions[i]`` is::

        {"mp3": "<abs path>", "new_basename": "<basename.mp3>"}

    The mp3 path is treated as the source; ``new_basename`` becomes the
    file's new name in the same folder. Sibling ``.lrc`` files are
    renamed alongside so lyric files stay attached.

    Refuses any source outside the configured music folder and any
    new_basename containing a path separator.

    Actions are topologically re-ordered so that if one action's target
    equals another action's source, the source rename runs first. This
    lets collision fixes like ``"A.mp3"->"B.mp3"`` and ``"B.mp3"->"C.mp3"``
    succeed without a temp-file dance.
    """
    if not isinstance(actions, list) or not actions:
        return {"ok": False, "error": "actions must be a non-empty list",
                "summary": {"renamed": 0, "failed": 0}, "results": []}
    root = Path(music_folder) if music_folder else None

    # --- Pre-validate actions, build target->action lookup, topo-sort.
    validated: list[dict] = []
    for a in actions:
        if not isinstance(a, dict):
            validated.append({"_invalid": True, "_err": "action must be an object",
                              "mp3": "", "new_basename": ""})
            continue
        validated.append({
            "mp3": a.get("mp3") or "",
            "new_basename": a.get("new_basename") or "",
        })

    # Re-order so dependent renames run first. Two actions conflict when
    # action A's target is action B's source — then B must precede A.
    # Anything that isn't paired into such a chain keeps its relative
    # order (stable sort).
    n = len(validated)
    order = list(range(n))
    if n > 1:
        # Build target-source key set for fast lookup.
        target_keys: dict[tuple[str, str], int] = {}
        for i, v in enumerate(validated):
            if v.get("_invalid"):
                continue
            src_path = v["mp3"]
            new_base = v["new_basename"]
            if not src_path or not new_base:
                continue
            if "/" in new_base or "\\" in new_base:
                continue
            try:
                t_key = (str(Path(src_path).parent.resolve()),
                         Path(new_base).name.lower())
            except Exception:
                continue
            target_keys[t_key] = i
        # For each action, count how many others must run before it.
        deps: list[int] = []
        for i, v in enumerate(validated):
            if v.get("_invalid"):
                deps.append(0)
                continue
            src_path = v["mp3"]
            if not src_path:
                deps.append(0)
                continue
            try:
                s_key = (str(Path(src_path).parent.resolve()),
                         Path(src_path).name.lower())
            except Exception:
                deps.append(0)
                continue
            dep = target_keys.get(s_key, -1)
            deps.append(0 if dep < 0 or dep == i else 1)
        # Stable sort so dependent renames run FIRST. An item with
        # deps=1 means "one other action must run before me" — so I
        # come first; deps=0 items keep their relative order at the end.
        order = sorted(range(n), key=lambda i: (-deps[i], i))

    results: list[dict] = []
    ok_count = 0
    fail_count = 0
    for idx in order:
        v = validated[idx]
        entry = {"mp3": v["mp3"], "new_basename": v["new_basename"], "ok": False}
        if v.get("_invalid"):
            entry["error"] = v["_err"]
            results.append(entry)
            fail_count += 1
            continue
        src = v["mp3"]
        new_base = v["new_basename"]
        if not src or not new_base:
            entry["error"] = "mp3 and new_basename required"
            results.append(entry)
            fail_count += 1
            continue
        if "/" in new_base or "\\" in new_base or new_base != Path(new_base).name:
            entry["error"] = "new_basename must be a bare filename"
            results.append(entry)
            fail_count += 1
            continue
        src_path = Path(src)
        if not _is_inside_music_folder(src_path):
            entry["error"] = "outside music folder"
            results.append(entry)
            fail_count += 1
            continue
        if not src_path.exists() or not src_path.is_file():
            entry["error"] = "source file not found"
            results.append(entry)
            fail_count += 1
            continue
        try:
            new_path = src_path.with_name(new_base)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            results.append(entry)
            fail_count += 1
            continue
        if new_path == src_path:
            entry["ok"] = True
            entry["note"] = "no-op"
            results.append(entry)
            ok_count += 1
            continue
        if new_path.exists():
            entry["error"] = f"target already exists: {new_base}"
            results.append(entry)
            fail_count += 1
            continue
        try:
            src_path.rename(new_path)
        except Exception as e:
            entry["error"] = f"rename failed: {type(e).__name__}: {e}"
            results.append(entry)
            fail_count += 1
            continue
        # Carry sibling .lrc along if present and basename matches.
        lrc_old = src_path.with_suffix(".lrc")
        if lrc_old.exists() and lrc_old.is_file():
            lrc_new = new_path.with_suffix(".lrc")
            try:
                lrc_old.rename(lrc_new)
                entry["lrc_renamed_to"] = str(lrc_new)
            except Exception as e:
                entry["lrc_warning"] = f"could not rename .lrc: {e}"
        entry["ok"] = True
        results.append(entry)
        ok_count += 1
    return {
        "ok": True,
        "summary": {"renamed": ok_count, "failed": fail_count},
        "results": results,
    }


# ---------------------------------------------------------------- yt-dlp helper


def resolve_youtube_single(url: str) -> dict:
    """R4: turn a YouTube URL or `ytsearch:<query>` string into a synthetic
    single-track album dict for the cart.

    Accepts:
      - https://www.youtube.com/watch?v=XXXXXXXXXXX
      - https://youtu.be/XXXXXXXXXXX
      - https://www.youtube.com/shorts/XXXXXXXXXXX
      - Direct video ID (11 chars, [A-Za-z0-9_-]{11})
      - ytsearch:<query>  -> uses ytsearch1 to find the top result
      - Otherwise: treated as a free-text query and run through ytsearch1

    Returns the synthetic-album dict shape used by the cart:
      {
        id: 'yt-<video_id>',
        title, artist, artwork, trackCount=1, alreadyOnServer=False,
        kind='single',
        tracks: [{...single track...}],
      }
    Raises ValueError with a user-readable message on failure.
    """
    import yt_dlp
    if not url or not isinstance(url, str):
        raise ValueError("URL is empty")
    s = url.strip()

    # Build the right yt-dlp extraction target
    target = s
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        target = f"https://www.youtube.com/watch?v={s}"
    elif "youtube.com" not in s and "youtu.be" not in s and not s.startswith("ytsearch"):
        # Treat as free-text query
        target = f"ytsearch1:{s}"

    extract_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    extract_opts.update(_yt_opts())

    info = None
    try:
        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as e:
        raise ValueError(f"Could not resolve YouTube link: {type(e).__name__}: {e}")

    if info is None:
        raise ValueError("YouTube returned no information for that link")
    # If ytsearch returned a list (search results), take the first entry.
    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    video_id = info.get("id")
    if not video_id:
        raise ValueError("YouTube returned no video ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id)):
        raise ValueError(f"YouTube returned an invalid video ID: {video_id!r}")

    title = (info.get("title") or "").strip() or f"YouTube {video_id}"
    artist = (info.get("artist") or info.get("creator") or info.get("uploader") or "").strip()
    duration = int(info.get("duration") or 0)
    duration_ms = duration * 1000 if duration > 0 else 0
    artwork = ""
    thumbnails = info.get("thumbnails") or []
    if thumbnails:
        # Pick the highest-resolution thumbnail we can find
        for t in reversed(thumbnails):
            if t.get("url"):
                artwork = t["url"]
                break
    if not artist:
        # Fall back to parsing "Artist - Title" from the title.
        if " - " in title:
            artist, title = title.split(" - ", 1)
            artist = artist.strip()
            title = title.strip()

    synthetic_id = f"yt-{video_id}"
    track = {
        "id": synthetic_id,
        "title": title,
        "artist": artist,
        "duration_ms": duration_ms,
        "track_number": 1,
        "disc_number": 1,
    }
    return {
        "id": synthetic_id,
        "title": title,
        "artist": artist,
        "release_date": "",
        "kind": "single",
        "trackCount": 1,
        "alreadyOnServer": False,
        "artwork": artwork,
        "fetchTracksOnStart": False,
        "tracks": [track],
        "expanded": False,
    }


def _yt_opts() -> dict:
    """Mirror app.py's anti-bot opts, but without an outtmpl/postprocessors —
    this returns the base opts for the automator worker.

    Note: cookies are INTENTIONALLY not loaded here. The main download flow
    in app.py still loads cookies because actual video downloads may need
    them. Lyrics are now sourced from LRCLIB (no YouTube involved), so this
    helper is only used by `download_track_mp3` for the actual audio fetch.
    """
    extra: dict = {}
    if APP_FFMPEG.exists():
        extra["ffmpeg_location"] = str(APP_FFMPEG)
    if APP_FFPROBE.exists():
        extra["ffprobe_location"] = str(APP_FFPROBE)
    extra["extractor_args"] = {
        "youtube": {
            "player_client": ["ios", "web", "web_safari", "mweb", "tv", "tv_embedded", "android"],
            "skip": ["translated_subs", "dash", "live_chat"],
        }
    }
    extra["socket_timeout"] = 20
    return extra


def download_track_mp3(query: str, out_path: Path) -> dict:
    """Search YouTube for `query`, pick bestaudio, save MP3 to `out_path`.

    Returns {ok, candidates_tried, error}.
    """
    candidates = ytsearch_candidates(query, max_n=3)
    if not candidates:
        return {"ok": False, "candidates_tried": 0, "error": "no_candidate"}
    return _download_track_with_candidates(candidates, out_path)


def ytsearch_candidates(query: str, max_n: int = 5) -> list[str]:
    """A1: list of candidate video URLs for `query`. Worker iterates this and
    tries each one until one passes `verify_audio`. Order is preserved
    (top of ytsearch first).
    """
    import yt_dlp
    search_opts = {"quiet": True, "no_warnings": True,
                   "extract_flat": True, "skip_download": True,
                   "noplaylist": True}
    search_opts.update(_yt_opts())
    try:
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_n}:{query}", download=False)
    except Exception as e:
        print(f"[ytsearch_candidates] ytsearch failed: {type(e).__name__}: {e}")
        return []
    entries = (info or {}).get("entries") or []
    return [f"https://www.youtube.com/watch?v={e['id']}"
            for e in entries if e.get("id")]


def ytsearch_with_fallback(artist: str, title: str, max_n: int = 5) -> tuple[list[str], str]:
    """Try multiple query variants until one returns results.

    Returns (candidates, used_query). If no variant yields results,
    returns ([], "<last variant tried>") so the caller can surface the
    attempted query in error messages.

    Order of attempts (most-specific first, broadest fallback last):
      1.  "{artist} - {title} audio"     # canonical (works for popular tracks)
      2.  "{artist} {title} audio"       # no dash (works for some artists)
      3.  "{artist} - {title}"           # no "audio" suffix
      4.  "{artist} {title}"              # no dash, no audio
      5.  "{title} lyrics"                # title-only with qualifier
      6.  "{title} official"              # title-only with qualifier
      7.  "{title}"                       # bare title (last resort)

    Each variant is tried with `max_n` results; we return the first
    non-empty list. If all 7 return 0, we return ([], last_variant) so
    the log can show the user what was tried.
    """
    import yt_dlp
    def _search(q: str) -> list[str]:
        try:
            search_opts = {"quiet": True, "no_warnings": True,
                           "extract_flat": True, "skip_download": True,
                           "noplaylist": True}
            search_opts.update(_yt_opts())
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{max_n}:{q}", download=False)
            return [f"https://www.youtube.com/watch?v={e['id']}"
                    for e in (info or {}).get("entries") or [] if e.get("id")]
        except Exception as e:
            print(f"[ytsearch] '{q}' failed: {type(e).__name__}: {e}")
            return []

    title_clean = (title or "").strip()
    artist_clean = (artist or "").strip()
    variants = [
        f"{artist_clean} - {title_clean} audio",
        f"{artist_clean} {title_clean} audio",
        f"{artist_clean} - {title_clean}",
        f"{artist_clean} {title_clean}",
        f"{title_clean} lyrics",
        f"{title_clean} official",
        f"{title_clean}",
    ]
    for q in variants:
        if not q.strip():
            continue
        cands = _search(q)
        if cands:
            return cands, q
    return [], variants[-1]


def _download_track_with_candidates(candidates: list[str], out_path: Path) -> dict:
    """A1: try each candidate URL with two format selectors. Used by both the
    synchronous `download_track_mp3` and the worker's retry loop.
    Returns {ok, candidates_tried, url, error}.
    """
    import yt_dlp
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base_opts = {
        "format": "bestaudio[acodec!=none]/bestaudio/best",
        "outtmpl": str(out_path.with_suffix(".%(ext)s")),
        "noplaylist": True,
        "quiet": True, "no_warnings": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
    }
    base_opts.update(_yt_opts())

    formats = ["bestaudio[acodec!=none]/bestaudio/best", "bestaudio/best"]
    last_err = None
    tried = 0
    for url in candidates:
        for fmt in formats:
            tried += 1
            try:
                opts = dict(base_opts)
                opts["format"] = fmt
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                if out_path.exists():
                    return {"ok": True, "candidates_tried": tried, "url": url}
                stem = out_path.stem
                for p in out_path.parent.glob(f"{stem}.*"):
                    if p.suffix.lower() == ".mp3" and p.exists():
                        if p != out_path:
                            try:
                                p.replace(out_path)
                            except Exception:
                                shutil.copyfile(p, out_path)
                                p.unlink(missing_ok=True)
                        return {"ok": True, "candidates_tried": tried, "url": url}
                last_err = RuntimeError("download finished but no mp3 found at expected path")
                continue
            except Exception as e:
                last_err = e
                if "format is not available" in str(e).lower():
                    continue
                break
    return {"ok": False, "candidates_tried": tried,
            "error": (f"{type(last_err).__name__ if last_err else 'Unknown'}: {last_err}"
                     if last_err else "unknown")}


def download_track_with_duration_retry(
        query: str,
        out_path: Path,
        expected_ms: int,
        max_candidates: int = 5,
        progress_cb=None,
) -> dict:
    """A1: search for `query`, try each candidate; on duration-mismatch, retry
    the next candidate instead of failing the track. Bound by `max_candidates`.

    `progress_cb(idx, max_candidates, candidate_url, used_query)` may be called
    from any thread. `used_query` is the ytsearch variant string that found
    this batch of candidates (so the caller can log it).

    Returns {ok, candidates_tried, url, error}. On full failure, error includes
    how many candidates failed and why each was rejected.

    Note: `query` is unused — kept in the signature for backward compat.
    The actual search uses `ytsearch_with_fallback(artist, title)` from the
    track metadata passed by the worker (via the `query` argument's parsed
    parts). For now, `query` is parsed as "artist - title".
    """
    # Parse artist and title from the legacy query string. The worker still
    # builds the canonical query for backward compat; we extract its parts
    # for the fallback. If parsing fails, just use the query as a title.
    artist, title = "", query
    if " - " in query:
        # Best-effort split on " - " (matches how the worker constructs it)
        artist, _, title = query.partition(" - ")
        # Drop trailing "audio" suffix used by the worker
        for suffix in (" audio",):
            if title.endswith(suffix):
                title = title[: -len(suffix)]
                break

    candidates, used_query = ytsearch_with_fallback(artist, title, max_n=max_candidates)
    if not candidates:
        return {
            "ok": False, "candidates_tried": 0,
            "error": (f"ytsearch returned 0 results for '{title}' by '{artist}' "
                      f"(last tried: '{used_query}'). The song may not exist "
                      f"on YouTube, the artist name may be misspelled, or all "
                      f"variants may be region-locked."),
        }
    failures = []
    for i, url in enumerate(candidates):
        if progress_cb:
            try:
                progress_cb(i, len(candidates), url, used_query)
            except TypeError:
                # Backward compat with old 3-arg progress_cb
                try: progress_cb(i, len(candidates), url)
                except Exception: pass
            except Exception:
                pass
        # Clean up any prior failed download at out_path
        try:
            out_path.unlink(missing_ok=True)
            for p in out_path.parent.glob(f"{out_path.stem}.*"):
                try: p.unlink()
                except Exception: pass
        except Exception:
            pass
        dl = _download_track_with_candidates([url], out_path)
        if not dl.get("ok"):
            failures.append(f"{url}: {dl.get('error', 'unknown')}")
            continue
        # Verify duration
        v = verify_audio(out_path, expected_ms)
        if not v.get("ok"):
            failures.append(f"{url}: duration {v.get('duration', '?')}s rejected ({v.get('error')})")
            # Move on to next candidate
            continue
        # Success
        return {
            "ok": True,
            "candidates_tried": i + 1,
            "url": url,
            "duration": v.get("duration"),
            "used_query": used_query,
            "candidates_failed": failures,
        }
    return {
        "ok": False,
        "candidates_tried": len(candidates),
        "error": (f"all {len(candidates)} candidate(s) failed; "
                  f"first failure: {failures[0] if failures else 'unknown'}"),
        "candidates_failed": failures,
    }



# ---------------------------------------------------------------- copy helpers


def safe_copy(src: Path, dst: Path, retries: int = 3) -> dict:
    """Copy file with SMB-style retry. Returns {ok, path, error}."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return {"ok": True, "path": str(dst)}
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)  # 2s, 4s
    return {"ok": False, "error": f"{type(last_err).__name__}: {last_err}" if last_err else "io_error"}


# ---------------------------------------------------------------- main worker

ACTIVE_AUTOMATOR_STATUSES = {
    "queued", "loading tracks", "searching", "downloading",
    "verifying", "tagging", "transcribing", "saving", "refreshing",
}


def automator_album_job(job_id: str, album: dict, backup: bool,
                        jobs_dict: dict, jobs_lock,
                        only_statuses: list[str] | None = None) -> None:
    """One album pipeline. Spawned as a daemon thread from app.py.

    `album` shape:
        {id, title, artist, tracks:[{id, title, duration_ms, track_number, disc_number}]}

    A2: `only_statuses` filters which track indices to process. When set (e.g.
    ['failed']), tracks that aren't in that status are marked 'skipped' and
    the job runs end-to-end. Used by the retry-failed-tracks endpoint.
    """
    # Late imports so app startup stays light
    with jobs_lock:
        job = jobs_dict[job_id]
        job["status"] = "loading tracks"
        job["title"] = f"{album.get('title', '')} \u2014 {album.get('artist', '')}"
        job["progress"] = {"done": 0, "total": len(album.get("tracks") or [])}

    profile = load_profile()
    if not profile.is_configured():
        with jobs_lock:
            job["status"] = "error"
            job["error"] = "automator profile not configured (visit Settings)"
        return

    tracks = list(album.get("tracks") or [])
    if not tracks:
        with jobs_lock:
            job["status"] = "error"
            job["error"] = "no tracks in album payload"
        return

    title_album = album.get("title", "Unknown Album")
    artist_album = album.get("artist", "Unknown Artist")
    safe_artist = sanitize_path_component(artist_album, "Unknown Artist")
    # R4: singles go to <artist>/Singles/ instead of <artist>/<album title>/
    is_single = album.get("kind") in ("single", "single-pending")
    if is_single:
        safe_album = "Singles"
    else:
        safe_album = sanitize_path_component(title_album, "Unknown Album")
    music_folder = Path(profile.music_folder)
    backup_folder = Path(profile.backup.path) if (
        backup and profile.backup.enabled and profile.backup.mode != "skip" and profile.backup.path
    ) else None

    # Prepare tmp dir
    tmp_dir = TMP_ROOT / job_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Initialize per-track statuses
    with jobs_lock:
        job["files"] = [{
            "id": t.get("id"),
            "title": t.get("title") or "",
            "track_number": t.get("track_number") or 0,
            "disc_number": t.get("disc_number") or 1,
            "status": "queued",
            "lyrics": False,
            "lyrics_source": "",   # "lrclib" | "whisper" | ""
            "language": "",
            "lrc_path": "",
            "mp3_path": "",
            "error": "",
        } for t in tracks]

    total = len(tracks)
    done = 0
    failed = 0
    partial = False
    backup_failed = False

    # Detect "already in flight" collision: same Artist/Album folder already being written this job
    target_folder = music_folder / safe_artist / safe_album
    try:
        target_folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        with jobs_lock:
            job["status"] = "error"
            job["error"] = f"cannot create target folder: {type(e).__name__}: {e}"
        return

    for idx, t in enumerate(tracks):
        # Cancel check
        with jobs_lock:
            if job.get("cancel_requested"):
                with jobs_lock:
                    job["status"] = "cancelled"
                    job["title"] = f"{title_album} \u2014 {artist_album} (stopped at {done}/{total})"
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return

        # A2: skip tracks that aren't in `only_statuses` (set by retry-failed).
        # Track statuses: pending, downloading, verifying, tagging,
        # transcribing, saving, done, failed, skipped.
        if only_statuses:
            current_status = (job.get("files") or [{}])[idx].get("status") or "pending"
            if current_status not in only_statuses:
                with jobs_lock:
                    job["files"][idx]["status"] = "skipped"
                    job["files"][idx]["error"] = f"skipped (status={current_status} not in {only_statuses})"
                done += 1
                with jobs_lock:
                    job["progress"]["done"] = done
                continue

        track_title = (t.get("title") or "").strip()
        track_artist = (t.get("artist") or artist_album).strip()
        tn = int(t.get("track_number") or (idx + 1))
        dn = int(t.get("disc_number") or 1)
        duration_ms = int(t.get("duration_ms") or 0)
        if not track_title:
            with jobs_lock:
                job["files"][idx]["status"] = "skipped"
                job["files"][idx]["error"] = "missing title"
            done += 1
            continue

        safe_title = sanitize_path_component(track_title, f"track-{tn:02d}")
        prefix = f"{tn:02d} - " if tn else ""
        tmp_filename = f"{prefix}{safe_title}.mp3"
        tmp_path = tmp_dir / tmp_filename

        # 0. R2 filesystem dedup: skip if an mp3 with matching (artist, title)
        #    already exists in the target folder. Best-effort — if the folder
        #    isn't readable or mutagen isn't available, fall through to download.
        try:
            existing = find_existing_in_folder(target_folder, track_artist, track_title)
            if existing is not None:
                with jobs_lock:
                    job["files"][idx]["status"] = "skipped"
                    job["files"][idx]["error"] = f"already exists: {existing.name}"
                    job["files"][idx]["mp3_path"] = str(existing)
                done += 1
                with jobs_lock:
                    job["progress"]["done"] = done
                continue
        except Exception:
            pass

        # 1. Download (A1: retry on duration-mismatch across multiple candidates;
        # Plan: also surfaces used_query so the per-track log shows which
        # ytsearch variant actually matched).
        with jobs_lock:
            job["status"] = "downloading"
            job["progress"]["current"] = f"{track_artist} \u2014 {track_title}"
            job["files"][idx]["status"] = "downloading"
        query = f"{track_artist} - {track_title} audio"
        _log_track(job, jobs_lock, idx, f"ytsearch: {query}")
        def _progress(i, n, u, q=None):
            used = f" (matched via {q!r})" if q else ""
            _log_track(job, jobs_lock, idx,
                       f"  candidate {i+1}/{n}: {u}{used}")
        dl = download_track_with_duration_retry(query, tmp_path, duration_ms,
                                                 progress_cb=_progress)
        if not dl.get("ok"):
            with jobs_lock:
                job["files"][idx]["status"] = "failed"
                job["files"][idx]["error"] = f"download: {dl.get('error', 'unknown')}"
            _log_track(job, jobs_lock, idx, f"FAIL: {dl.get('error', 'unknown')}")
            failed += 1
            done += 1
            continue

        # 2. Verify audio
        with jobs_lock:
            job["status"] = "verifying"
            job["files"][idx]["status"] = "verifying"
        v = verify_audio(tmp_path, duration_ms)
        if not v.get("ok"):
            with jobs_lock:
                job["files"][idx]["status"] = "failed"
                job["files"][idx]["error"] = f"audio_invalid: {v.get('error')}"
            tmp_path.unlink(missing_ok=True)
            failed += 1
            done += 1
            with jobs_lock:
                job["progress"]["done"] = done
            continue

        # 3. Tag
        with jobs_lock:
            job["status"] = "tagging"
            job["files"][idx]["status"] = "tagging"
        total_tracks = max(total, 1)
        total_discs = max(int(tracks[-1].get("disc_number") or 1), 1)
        year = (t.get("release_date") or "")[:4]
        tag_in = {
            "title": track_title,
            "artist": track_artist,
            "album": title_album,
            "album_artist": artist_album,
            "track_number": tn,
            "disc_number": dn,
            "total_tracks": total_tracks,
            "total_discs": total_discs,
            "year": year,
            "genre": (album.get("genre") or ""),
        }
        tag = tag_and_verify(tmp_path, tag_in)
        if not tag.get("ok"):
            with jobs_lock:
                job["files"][idx]["status"] = "failed"
                job["files"][idx]["error"] = f"tag_error: {tag.get('error')}"
            tmp_path.unlink(missing_ok=True)
            failed += 1
            done += 1
            with jobs_lock:
                job["progress"]["done"] = done
            continue

        # 3b. Embed cover art (best-effort, after tags but before lyrics so the
        # track always has art regardless of which lyrics source is used).
        artwork = (album.get("artwork") or album.get("artworkUrl100") or "").strip()
        if artwork:
            _log_track(job, jobs_lock, idx, "embedding cover art")
            art_result = embed_cover_art(tmp_path, artwork)
            if art_result.get("ok"):
                _log_track(job, jobs_lock, idx,
                           f"cover art: OK {art_result.get('mime')} {art_result.get('bytes')}B")
            else:
                _log_track(job, jobs_lock, idx,
                           f"cover art: FAIL: {art_result.get('error', 'unknown')}")
            with jobs_lock:
                job["files"][idx]["cover_art"] = {
                    "ok": bool(art_result.get("ok")),
                    "bytes": art_result.get("bytes", 0),
                    "mime": art_result.get("mime", ""),
                    "error": art_result.get("error", ""),
                }

        # 4. Transcribe / use pre-supplied lyrics
        with jobs_lock:
            job["status"] = "transcribing"
            job["files"][idx]["status"] = "transcribing"
        track_key = str(t.get("id") or "")
        pre_lyrics = (job.get("track_lyrics") or {}).get(track_key) or {}
        if pre_lyrics.get("source") == "none":
            # User picked "No lyrics": skip Whisper entirely, no .lrc written.
            _log_track(job, jobs_lock, idx, "lyrics: skipped (user picked 'no lyrics')")
            with jobs_lock:
                job["files"][idx]["lyrics_source"] = "none"
                job["files"][idx]["lyrics_status"] = "user skipped"
            lrc = {"ok": False, "skip": True, "lrc_text": ""}
        elif pre_lyrics.get("source") == "lyrics" and pre_lyrics.get("lrc_text"):
            lrc = {"ok": True,
                   "lrc_text": pre_lyrics["lrc_text"],
                   "language": pre_lyrics.get("language") or "",
                   "segments": 0}
            with jobs_lock:
                job["files"][idx]["lyrics_source"] = "lrclib"
                job["files"][idx]["lyrics_status"] = "saved (lrclib)"
            _log_track(job, jobs_lock, idx, "lyrics: using LRCLIB result")
        else:
            # Track wasn't in the lyrics-review modal's results — either
            # LRCLIB had no record for it (so the modal skipped showing it)
            # or the user picked Whisper explicitly. Either way we try
            # Whisper next; record the reason so the UI can show it when
            # Whisper fails or returns nothing.
            with jobs_lock:
                job["files"][idx]["lyrics_source"] = "whisper"
            _log_track(job, jobs_lock, idx, "lyrics: no LRCLIB match, trying Whisper")
            try:
                lrc = transcribe_lrc(tmp_path, profile.whisper_model, profile.whisper_device)
            except Exception as e:
                lrc = {"ok": False, "error": f"{type(e).__name__}: {e}",
                       "lrc_text": ""}
            if not lrc.get("ok"):
                with jobs_lock:
                    err = (lrc.get("error") or "").strip()
                    job["files"][idx]["lyrics_status"] = (
                        f"no LRCLIB match; whisper failed: {err}"
                        if err else "no LRCLIB match; whisper returned nothing"
                    )
        lrc_path: Optional[Path] = None
        # Only write .lrc when the lyrics pipeline succeeded AND returned
        # real content. Failures return empty lrc_text so we don't
        # pollute the music folder with placeholder files.
        if lrc.get("ok") and lrc.get("lrc_text") and not lrc.get("skip"):
            lrc_path = tmp_path.with_suffix(".lrc")
            try:
                lrc_path.write_text(lrc["lrc_text"], encoding="utf-8")
            except Exception as e:
                lrc_path = None
                lrc = {"ok": False, "error": f"lrc write: {e}", "lrc_text": ""}
                with jobs_lock:
                    cur = job["files"][idx].get("lyrics_status") or "lyrics pipeline ok"
                    job["files"][idx]["lyrics_status"] = f"{cur}; disk write failed: {e}"
            else:
                # Surface what actually produced the .lrc for this track.
                with jobs_lock:
                    src = job["files"][idx].get("lyrics_source") or "?"
                    job["files"][idx]["lyrics_status"] = f"saved ({src})"
        if not lrc.get("ok") and not lrc.get("skip"):
            partial = True

        # 5. Save to Jellyfin
        with jobs_lock:
            job["status"] = "saving"
            job["files"][idx]["status"] = "saving"
        try:
            target_base = f"{prefix}{safe_title}"
            target_mp3 = unique_target_path(target_folder, target_base, ".mp3")
            _log_track(job, jobs_lock, idx, f"saving to {target_mp3}")
            shutil.copyfile(tmp_path, target_mp3)
            target_lrc = None
            if lrc_path and lrc_path.exists():
                target_lrc = unique_target_path(target_folder, target_base, ".lrc")
                shutil.copyfile(lrc_path, target_lrc)
        except Exception as e:
            with jobs_lock:
                job["files"][idx]["status"] = "failed"
                job["files"][idx]["error"] = f"disk_write: {type(e).__name__}: {e}"
            tmp_path.unlink(missing_ok=True)
            (lrc_path and lrc_path.unlink(missing_ok=True))
            failed += 1
            done += 1
            with jobs_lock:
                job["progress"]["done"] = done
            continue

        # 6. Backup copy (optional)
        if backup_folder:
            try:
                bk_folder = backup_folder / safe_artist / safe_album
                bk_base = f"{prefix}{safe_title}"
                bk_mp3 = unique_target_path(bk_folder, bk_base, ".mp3")
                shutil.copyfile(tmp_path, bk_mp3)
                if lrc_path and lrc_path.exists():
                    bk_lrc = unique_target_path(bk_folder, bk_base, ".lrc")
                    shutil.copyfile(lrc_path, bk_lrc)
            except Exception as e:
                backup_failed = True
                partial = True

        with jobs_lock:
            job["files"][idx]["status"] = "done"
            job["files"][idx]["mp3_path"] = str(target_mp3)
            job["files"][idx]["lrc_path"] = str(target_lrc) if target_lrc else ""
            job["files"][idx]["lyrics"] = bool(lrc.get("ok"))
            job["files"][idx]["error"] = ""
        _log_track(job, jobs_lock, idx, "OK: track completed")

        # Cleanup tmp
        tmp_path.unlink(missing_ok=True)
        if lrc_path and lrc_path.exists():
            lrc_path.unlink(missing_ok=True)

        done += 1
        with jobs_lock:
            job["progress"]["done"] = done
            job["progress"]["current"] = ""

    # 7. Trigger Jellyfin refresh
    with jobs_lock:
        job["status"] = "refreshing"
    try:
        client = JellyfinClient(profile.jellyfin_url, profile.jellyfin_api_key)
        refresh = client.refresh_library()
        if not refresh.get("ok"):
            partial = True
    except Exception as e:
        partial = True

    # 8. Cleanup tmp
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # 9. Final status
    with jobs_lock:
        if failed == total:
            job["status"] = "error"
            job["error"] = f"All {total} tracks failed."
            job["title"] = f"{title_album} \u2014 {artist_album} (0/{total})"
        elif failed > 0 or partial or backup_failed:
            job["status"] = "partial"
            tags = []
            if failed:
                tags.append(f"{failed} failed")
            if backup_failed:
                tags.append("backup failed")
            job["title"] = (f"{title_album} \u2014 {artist_album} "
                            f"({done - failed}/{total}" + (f"; {', '.join(tags)}" if tags else "") + ")")
        else:
            job["status"] = "done"
            job["title"] = f"{title_album} \u2014 {artist_album} ({total}/{total})"
        job["progress"]["current"] = ""


# ---------------------------------------------------------------- library-already-on-server check

def canonical(s: str) -> str:
    """FULL CAPS + no spaces + no punctuation + no underscores, accents stripped.

    Single source of truth for 'is this the same thing' comparisons across the
    app: album-level server match, track-level server match, filesystem dedup,
    and tag-readback verification.

    Examples (R2):
        "Love Yourself 結 'Answer'"      -> "LOVEYOURSELF結ANSWER"
        "BTS"                             -> "BTS"
        "B T S"                           -> "BTS"
        "P!nk"                            -> "PNK"
        "Café del Mar"                    -> "CAFEDELMAR"
        "보아 (BoA)"                       -> "보아BOA"
        "a-b_c"                           -> "ABC"     # underscore stripped
    """
    if not s:
        return ""
    import unicodedata
    # Decompose accented chars then drop combining marks. e.g. é -> e + ́ -> e
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("\u3000", " ")  # full-width space -> normal space
    # Keep letters, digits, CJK block. Strip everything else: whitespace,
    # punctuation (incl. underscore), control chars, emoji, symbols.
    # We do this by removing underscore explicitly then applying the same
    # char-class filter as before.
    s = s.replace("_", "")
    s = re.sub(r"[^\w\u00C0-\uFFFF]+", "", s)
    return s.upper()


def _album_signature(artist: str, title: str) -> tuple[str, str]:
    """Match an album while ignoring a trailing artist alias in parentheses.

    Jellyfin may label BTS as ``BTS (방탄소년단)`` while music catalogs use
    ``BTS``. The album title still has to match exactly after normalization.
    """
    primary_artist = re.sub(r"\s*(?:\([^)]*\)|\[[^]]*\])\s*$", "", artist or "")
    return (canonical(primary_artist), canonical(title))


def _log_track(job: dict, jobs_lock, idx: int, line: str) -> None:
    """C1: append a human-readable log line to a track's per-file record.
    Logs survive across restarts (kept in the in-memory job dict, surfaced
    to the UI via the existing files[i].log field).

    Capped at 200 entries per file to bound memory in long jobs.
    """
    with jobs_lock:
        f = job["files"][idx]
        f.setdefault("log", [])
        if len(f["log"]) >= 200:
            f["log"].pop(0)
        f["log"].append(line)


def _track_signature(artist: str, title: str, duration_ms: int = 0) -> tuple[str, str, int]:
    """Track-level signature for server dedup (R2).

    Includes duration in seconds so we don't false-positive match
    'Bohemian Rhapsody' (354s) against 'Bohemian Rhapsody (Remix)' (480s).
    """
    return (canonical(artist), canonical(title), int(duration_ms or 0) // 1000)


def mark_already_on_server(albums: list[dict], client: JellyfinClient) -> list[dict]:
    """Annotate albums using the complete Jellyfin album library.

    Recent items only cover the newest 50 Jellyfin albums, which made older
    releases appear addable in Automator search results.

    R2: also annotates each track dict with `trackAlreadyOnServer: bool` if the
    client exposes a recent-tracks endpoint (else False).
    """
    if not albums:
        return albums
    server_albums = []
    offset = 0
    while True:
        page, total = client.get_all_albums(limit=500, start_index=offset)
        server_albums.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    album_sigs = {_album_signature(a.get("artist", ""), a.get("title", ""))
                  for a in server_albums}

    # R2: track-level match against Jellyfin's audio library.
    recent_track_sigs = set()
    recent_title_artist_sigs = set()
    if hasattr(client, "get_recent_tracks"):
        try:
            for t in client.get_recent_tracks(limit=5000) or []:
                artist = t.get("artist", "")
                title = t.get("title", "")
                recent_track_sigs.add(
                    _track_signature(artist, title, int((t.get("duration_ms") or 0))))
                recent_title_artist_sigs.add((canonical(artist), canonical(title)))
        except Exception:
            pass

    for a in albums:
        a["alreadyOnServer"] = _album_signature(a.get("artist", ""), a.get("title", "")) in album_sigs
        # Per-track annotation
        for t in (a.get("tracks") or []):
            track_artist = t.get("artist", "") or a.get("artist", "")
            duration_ms = int(t.get("duration_ms") or 0)
            signature = _track_signature(track_artist, t.get("title", ""), duration_ms)
            title_artist = (canonical(track_artist), canonical(t.get("title", "")))
            t["trackAlreadyOnServer"] = signature in recent_track_sigs or (
                not duration_ms and title_artist in recent_title_artist_sigs
            )
    return albums


def find_existing_in_folder(folder: Path, expected_artist: str,
                            expected_title: str) -> Optional[Path]:
    """R2 filesystem dedup: walks `folder` looking for an mp3 whose ID3 tags
    match `(canonical(expected_artist), canonical(expected_title))`. Returns the
    first match, or None. Read-only — does not delete or modify anything.

    On any error (folder missing, mutagen unavailable, etc.) returns None so
    the caller falls back to the always-download path.
    """
    if not folder or not folder.exists():
        return None
    sig = (canonical(expected_artist), canonical(expected_title))
    if not sig[0] and not sig[1]:
        return None
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB
    except ImportError:
        return None
    try:
        for mp3 in folder.glob("*.mp3"):
            try:
                try:
                    audio = MP3(str(mp3), ID3=ID3)
                except ID3NoHeaderError:
                    continue
                tags = audio.tags or {}
                try:
                    title = str(tags[TIT2]).text[0] if TIT2 in tags else ""
                except Exception:
                    title = ""
                try:
                    artist = str(tags[TPE1]).text[0] if TPE1 in tags else ""
                except Exception:
                    artist = ""
                if (canonical(artist), canonical(title)) == sig:
                    return mp3
            except Exception:
                continue
    except Exception:
        return None
    return None
