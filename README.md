# Sleeve

![Sleeve logo](static/sleeve-mark.png)

A self-hosted music utility for browsing a Jellyfin music library, discovering albums and tracks, downloading audio, and maintaining lyric files and album folders.

## What it does

- **Library:** browse and filter albums from a configured Jellyfin server.
- **Discover:** browse current charts and new albums, search enabled music catalogs, and queue track or album downloads.
- **Automator:** collect albums, review lyric matches, fetch and verify tracks, write tags and lyrics, and sync the finished album to Jellyfin.
- **Repair:** find broken or missing lyrics, write manual or uploaded LRC files, detect duplicates, and review filename/track-number fixes.
- **Downloads:** monitor and cancel active jobs, then retrieve completed files.

## Quick start

See [Setup and configuration](docs/SETUP.md) for prerequisites, FFmpeg installation, provider credentials, and troubleshooting.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 5000
```

Visit <http://127.0.0.1:5000>. Keep the service bound to localhost unless you add an appropriate authentication and network-security layer; the app does not provide user login.

## Project guide

- [Architecture and external services](docs/ARCHITECTURE.md)
- [Contributing](CONTRIBUTING.md)
- [MIT License](LICENSE)

The frontend uses plain JavaScript and CSS, with no package manager or build step. The main FastAPI application is `app:app`.

## Local data and privacy

The app writes browser cookies, Jellyfin settings/API keys, caches, temporary audio, and downloads to the per-user data directory (see [Setup](docs/SETUP.md)). Existing root-level settings are copied on first run and left untouched. `.gitignore` also excludes legacy local files. Review `git status` before sharing a checkout, and never publish `.env`, `automator_profile.json`, `cookie.txt`, or `cookies.txt`.
