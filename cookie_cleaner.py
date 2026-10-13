"""
cookie_cleaner.py \u2014 self-healing cookie file maintenance.

yt-dlp requires the cookie file to be a strict Netscape-format text file
(tab-separated, 7 fields per line, no embedded newlines or NUL bytes).
Some browser cookie exports \u2014 especially Google cookies like the long
`COMPASS` JWT or `cf_clearance` Cloudflare values \u2014 get written with
embedded newlines that break the format. yt-dlp then refuses to load the
file entirely, which kills every download and subtitle probe.

This module:
  - Tracks the cookie file's mtime + size
  - On every check, re-parses the file. If it's malformed, joins split
    continuation lines, strips NUL bytes, drops rows that still can't be
    repaired, and rewrites the file in place (with a `.bak` backup).
  - Caches "last validated as good" so repeated checks are O(1) until the
    file changes again.

Public API:
  ensure_cookie_file_valid(path) -> dict
    Returns {ok, repaired, original_lines, valid_lines, dropped_lines, error}
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path

_TAB = "\t"
_LINE_RE = re.compile(r"^[^#\s]")  # first non-blank, non-comment line

_state_lock = threading.Lock()
_state: dict[str, tuple[float, int, bool]] = {}
# path -> (mtime, size, valid?)


def _read_text(path: Path) -> str:
    """Read the file, replacing NUL bytes with empty + decoding leniently."""
    raw = path.read_bytes()
    # Strip NUL bytes \u2014 they are never valid in a Netscape cookie line.
    raw = raw.replace(b"\x00", b"")
    # Decode as UTF-8 with replacement for any other odd chars.
    return raw.decode("utf-8", errors="replace")


def _count_valid_and_dropped(text: str) -> tuple[int, int, int]:
    """Return (total_lines, valid_lines, dropped_lines).

    Valid = blank line, comment, or \u22657 tab-separated fields.
    Dropped = anything else (split value, garbage, NUL-only after stripping).
    """
    valid = dropped = 0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            valid += 1   # counts as "kept"
            continue
        if len(line.split(_TAB)) >= 7:
            valid += 1
        else:
            dropped += 1
    return valid + dropped, valid, dropped


def clean_cookie_file(path: str | Path) -> dict:
    """Read, repair, and rewrite a Netscape cookie file in place.

    Strategy: strip NUL bytes, drop any line that isn't blank, comment, or a
    proper \u22657-field Netscape cookie row. We do NOT rejoin split lines \u2014
    a long value split across lines looks identical to garbage and rejoining
    it would silently corrupt the previous cookie. Dropping is safer: the
    user just re-exports from the browser to recover any lost entries.

    Returns:
      ok (bool): True if file is now valid.
      repaired (bool): True if a write happened.
      original_lines, valid_lines, dropped_lines (int).
      backup_path (str|None): Path to .bak if we wrote.
      error (str|None): Set on unrecoverable I/O failure.
    """
    p = Path(path)
    result = {"ok": False, "repaired": False, "original_lines": 0,
              "valid_lines": 0, "dropped_lines": 0,
              "backup_path": None, "error": None}
    if not p.exists():
        result["error"] = "cookie file does not exist"
        return result
    try:
        original_text = _read_text(p)
    except Exception as e:
        result["error"] = f"read failed: {type(e).__name__}: {e}"
        return result

    # Count before
    orig_total, orig_valid, orig_dropped = _count_valid_and_dropped(original_text)
    result["original_lines"] = orig_total
    result["valid_lines"] = orig_valid
    result["dropped_lines"] = orig_dropped
    if orig_dropped == 0:
        result["ok"] = True
        return result

    # Walk and rebuild \u2014 keep only blank/comment/valid-cookie rows
    cleaned_lines: list[str] = []
    kept = dropped = 0
    for line in original_text.splitlines():
        if not line or line.lstrip().startswith("#"):
            cleaned_lines.append(line)
            kept += 1
            continue
        if len(line.split(_TAB)) >= 7:
            cleaned_lines.append(line)
            kept += 1
        else:
            dropped += 1
    result["valid_lines"] = kept
    result["dropped_lines"] = dropped
    result["repaired"] = True

    # Write backup + new file
    try:
        bak = p.with_suffix(p.suffix + ".bak")
        shutil.copyfile(p, bak)
        result["backup_path"] = str(bak)
        p.write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")
    except Exception as e:
        result["error"] = f"write failed: {type(e).__name__}: {e}"
        return result

    result["ok"] = True
    return result


def ensure_cookie_file_valid(path: str | Path) -> dict:
    """Check the cookie file. If it's malformed or has changed since last
    successful check, repair it in place.

    The result is cached by (path, mtime, size) so repeated calls within a
    single request are cheap.
    """
    p = Path(path)
    result = {"ok": False, "repaired": False, "checked": False, "error": None}
    try:
        if not p.exists():
            result["error"] = "cookie file does not exist"
            return result
        st = p.stat()
        mtime, size = st.st_mtime, st.st_size
        with _state_lock:
            prev = _state.get(str(p))
            if prev and prev[0] == mtime and prev[1] == size and prev[2]:
                # Same file we already validated as good.
                result["ok"] = True
                result["checked"] = False
                return result
    except Exception as e:
        result["error"] = f"stat failed: {type(e).__name__}: {e}"
        return result

    # File changed (or never validated) \u2014 re-check.
    result["checked"] = True
    text = _read_text(p)
    _, valid, dropped = _count_valid_and_dropped(text)
    if dropped == 0:
        result["ok"] = True
        with _state_lock:
            _state[str(p)] = (mtime, size, True)
        return result

    # Has bad lines \u2014 run the cleaner.
    clean_result = clean_cookie_file(p)
    result.update(clean_result)
    if clean_result.get("ok"):
        try:
            new_st = p.stat()
            with _state_lock:
                _state[str(p)] = (new_st.st_mtime, new_st.st_size, True)
        except Exception:
            pass
    return result


def reset_cache(path: str | Path | None = None) -> None:
    """Forget cached validation state. Useful in tests / after manual edits."""
    with _state_lock:
        if path is None:
            _state.clear()
        else:
            _state.pop(str(path), None)
