"""
dl_log.py — download history log (SQLite-backed)
----------------------------------
Delegates to jfile.py for database storage.
"""

from jfile import (
    download_log_exists,
    download_log_add,
    download_log_get_one,
    download_log_remove,
    download_log_clear,
    download_log_get,
)


def already_downloaded(key: str) -> bool:
    return download_log_exists(key)


def log_download(key: str, title: str, source: str,
                 media: str, path: str = "") -> None:
    download_log_add(key, title, media)


def get_entry(key: str) -> dict | None:
    return download_log_get_one(key)


def all_entries() -> dict:
    return download_log_get()


def remove_entry(key: str) -> bool:
    return download_log_remove(key)


def clear_log() -> int:
    return download_log_clear()