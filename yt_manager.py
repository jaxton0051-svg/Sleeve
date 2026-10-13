"""
yt_manager.py — persistent web download queue for F### YouTube downloader.

Differences from the previous in-memory version:
  * Jobs live in SQLite (`download_queue` / `download_history`). A server
    restart, Ctrl+C, sleep, or process crash no longer loses queued jobs.
  * The worker thread is supervised — if it dies for any reason, a watchdog
    restarts it within a couple of seconds.
  * While a download is running, a heartbeat thread logs progress every
    HEARTBEAT_INTERVAL seconds so you can tell the job is still alive even
    when yt-dlp's own logger is silenced.
  * Errors are stored on the row and kept in history (last 50), so a failed
    download's error string doesn't scroll off after 8 newer jobs.
  * On startup, any row stuck in 'downloading' is reset to 'queued' and
    retried — that's the case where the process died mid-download.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from app_paths import DATA_DIR

import jfile
import yt_tools

log = logging.getLogger("yt_manager")

DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

HEARTBEAT_INTERVAL = 3.0
WORKER_POLL_INTERVAL = 2.0
WORKER_RESPAWN_BACKOFF = 2.0
HISTORY_LIMIT = 50

_lock = threading.Lock()
_current: dict[str, Any] | None = None
_worker_thread: threading.Thread | None = None
_supervisor_thread: threading.Thread | None = None
_stop = threading.Event()
_started = False


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_job_dict(
    job_id: str,
    url: str,
    fmt: str,
    mode: str,
    quality: str,
    requested_by: str,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "url": url,
        "fmt": fmt,
        "mode": mode,
        "quality": quality,
        "requested_by": requested_by,
        "title": url,
        "progress": 0.0,
        "speed": "",
        "eta": "",
        "status": "queued",
        "error": "",
        "started": "",
        "created": _now(),
        "finished_at": "",
    }


def enqueue(
    url: str,
    fmt: str = "mp4",
    mode: str = "single",
    quality: str = "best",
    requested_by: str = "",
) -> dict[str, Any]:
    """Add a job to the persistent queue and make sure the worker is alive."""
    job_id = uuid.uuid4().hex[:10]
    job = _new_job_dict(job_id, url, fmt, mode, quality, requested_by)
    jfile.dlq_insert(job)
    log.info("queued job %s: %s [%s/%s/%s]", job_id, url, fmt, mode, quality)
    ensure_worker()
    return job


def status() -> dict[str, Any]:
    """Snapshot for the dashboard. Reads live from SQLite.

    `queue` only contains jobs still waiting (`status='queued'`); the job the
    worker is currently processing lives only under `current` so the
    frontend doesn't render it twice with stale + live state."""
    with _lock:
        current = dict(_current) if _current else None
    queued = jfile.dlq_list_queued()
    history = jfile.dlh_list_recent(HISTORY_LIMIT)
    return {
        "current": current,
        "queue": queued,
        "recent": history[:8],
        "history_size": len(history),
        "worker_alive": _worker_thread.is_alive() if _worker_thread else False,
        "time": _now(),
    }


def clear_history() -> int:
    """Drop finished+errored rows from the active queue (history is kept)."""
    return jfile.dlq_clear_finished()


# -----------------------------------------------------------------------------
# Worker
# -----------------------------------------------------------------------------


def _make_progress_callback(job: dict[str, Any]):
    def hook(d: dict) -> None:
        try:
            status_type = d.get("status")
            if status_type == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                progress = (downloaded / total * 100) if total else 0.0
                speed = d.get("speed")
                speed_s = f"{speed / 1024 / 1024:.1f} MB/s" if speed else ""
                eta = d.get("eta")
                eta_s = f"{eta}s" if eta is not None else ""
                info = d.get("info_dict", {}) or {}
                title = info.get("title") or job["url"]
                with _lock:
                    job["progress"] = progress
                    job["speed"] = speed_s
                    job["eta"] = eta_s
                    job["title"] = title
                    job["status"] = "downloading"
                jfile.dlq_update_progress(
                    job["id"], progress, speed_s, eta_s, title, "downloading"
                )
            elif status_type == "finished":
                with _lock:
                    job["progress"] = 100.0
                    job["status"] = "processing"
                jfile.dlq_update_progress(
                    job["id"], 100.0, job.get("speed", ""), job.get("eta", ""),
                    job.get("title", job["url"]), "processing"
                )
        except Exception as e:
            log.warning("progress callback error for %s: %s", job.get("id"), e)
    return hook


def _heartbeat(job: dict[str, Any]) -> None:
    """Periodically log that a job is still alive — useful when yt-dlp is silent."""
    while not _stop.is_set():
        if _stop.wait(HEARTBEAT_INTERVAL):
            return
        with _lock:
            if job.get("status") != "downloading":
                return
            title = (job.get("title") or job["url"])[:50]
            progress = job.get("progress", 0)
            speed = job.get("speed", "")
        log.info("[%s] heartbeat: %s %.0f%% %s",
                 job["id"], title, progress, speed)


def _process_one(row: dict[str, Any]) -> dict[str, Any]:
    """Run a single job end-to-end. Any exception is captured into the row."""
    job = dict(row)
    job["status"] = "downloading"
    job["started"] = _now()
    job["progress"] = 0.0
    job["error"] = ""
    jfile.dlq_update_progress(job["id"], 0.0, "", "", job["title"], "downloading")
    log.info("job %s started: %s (%s/%s)", job["id"], job["url"], job["fmt"], job["mode"])

    with _lock:
        # make the live state visible to status() too
        job.update({
            "status": "downloading",
            "started": job["started"],
            "progress": 0.0,
        })

    hb = threading.Thread(target=_heartbeat, args=(job,), daemon=True)
    hb.start()

    try:
        yt_tools.download_youtube(
            url=job["url"],
            media=job["fmt"],
            mode=job["mode"],
            quality=job["quality"],
            output_dir=DOWNLOAD_DIR,
            progress_callback=_make_progress_callback(job),
        )
        job["status"] = "done"
        job["progress"] = 100.0
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        log.error("job %s failed: %s", job["id"], job["error"])
        log.debug("job %s traceback:\n%s", job["id"], traceback.format_exc())
    finally:
        job["finished_at"] = _now()
        with _lock:
            for k in ("status", "error", "finished_at", "progress"):
                if k in job:
                    pass

    jfile.dlq_archive({
        "id": job["id"],
        "url": job["url"],
        "fmt": job["fmt"],
        "mode": job["mode"],
        "quality": job["quality"],
        "requested_by": job.get("requested_by", ""),
        "title": job.get("title", job["url"]),
        "status": job["status"],
        "error": job.get("error", ""),
        "started": job.get("started", ""),
        "created": job["created"],
        "finished_at": job["finished_at"],
    })
    return job


def _worker_loop() -> None:
    """Drains the SQLite queue. Self-contained — crashes here get respawned
    by the supervisor thread."""
    log.info("yt_manager worker loop started")
    while not _stop.is_set():
        try:
            row = jfile.dlq_get_next()
            if row is None:
                if _stop.wait(WORKER_POLL_INTERVAL):
                    return
                continue

            log.info("picking up job %s: %s", row["id"], row["url"])
            with _lock:
                global _current
                _current = row

            try:
                _process_one(row)
            finally:
                with _lock:
                    _current = None
        except Exception as e:
            # Anything escaping here is a bug, not a job failure.
            log.exception("worker loop iteration crashed; will retry: %s", e)
            if _stop.wait(WORKER_RESPAWN_BACKOFF):
                return
    log.info("yt_manager worker loop stopped")


def _supervisor_loop() -> None:
    """Restart the worker if it dies. Keeps going until stop() is called."""
    global _worker_thread
    log.info("yt_manager supervisor started")
    while not _stop.is_set():
        if _worker_thread is None or not _worker_thread.is_alive():
            log.warning("worker thread not alive — restarting")
            _worker_thread = threading.Thread(
                target=_worker_loop, name="yt_manager_worker", daemon=True
            )
            _worker_thread.start()
        if _stop.wait(2.0):
            return
    log.info("yt_manager supervisor stopped")


def ensure_worker() -> None:
    """Idempotent: starts the worker + supervisor exactly once per process."""
    global _started, _worker_thread, _supervisor_thread
    with _lock:
        if _started:
            return
        # Reset any rows stuck in 'downloading' from a previous run.
        reset = jfile.dlq_reset_orphan_running()
        if reset:
            log.warning("reset %d job(s) stuck in 'downloading' on startup — will retry",
                        reset)
        _started = True

    _stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="yt_manager_worker", daemon=True
    )
    _worker_thread.start()
    _supervisor_thread = threading.Thread(
        target=_supervisor_loop, name="yt_manager_supervisor", daemon=True
    )
    _supervisor_thread.start()


def stop() -> None:
    """For tests / clean shutdown."""
    _stop.set()
