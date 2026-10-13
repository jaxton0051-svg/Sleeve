"""Paths for app-owned state, kept separate from the source checkout."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def _default_data_dir() -> Path:
    override = os.environ.get("SLEEVE_DATA_DIR") or os.environ.get("AAA_BEST_MUSIC_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        parent, current, legacy = Path(base), "Sleeve", "AAA Best Music App"
    elif sys.platform == "darwin":
        parent, current, legacy = Path.home() / "Library" / "Application Support", "Sleeve", "AAA Best Music App"
    else:
        parent = Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"))
        current, legacy = "sleeve", "aaa-best-music-app"
    # Existing installations keep their local credentials and downloads.
    old_path = parent / legacy
    return old_path if old_path.is_dir() and not (parent / current).exists() else parent / current


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_file(name: str, *, migrate_legacy: bool = True) -> Path:
    """Return an app-data path and copy a legacy root file there once.

    Legacy files are copied (never deleted or overwritten) so existing local
    settings and credentials continue to work while the checkout stays clean.
    """
    destination = DATA_DIR / name
    legacy = PROJECT_DIR / name
    if migrate_legacy and not destination.exists() and legacy.is_file():
        try:
            shutil.copy2(legacy, destination)
        except OSError as exc:
            # Preserve the old installation's behavior when app data is not
            # writable; surface the issue in the normal runtime logs.
            print(f"[paths] unable to migrate {legacy.name}: {exc}")
            return legacy
    return destination
