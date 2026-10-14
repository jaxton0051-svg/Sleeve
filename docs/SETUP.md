# Setup and configuration

## Requirements

- Python 3.10 or newer.
- FFmpeg and FFprobe available on `PATH`. On Windows, install a trusted FFmpeg build and add its `bin` directory to `PATH`; on macOS or Linux, install FFmpeg through the platform package manager. Restart the terminal after changing `PATH`.
- A Jellyfin server is required to sign in. A Jellyfin administrator account is required because Repair and Automator can change library files. Jellyfin's API key is configured separately in Automator settings for server-side library operations.

The project intentionally does not ship FFmpeg binaries. They are large platform-specific executables with their own build and license obligations. `yt-dlp` and the Automator locate FFmpeg from `PATH`; existing project-local binaries remain supported for local development but are ignored by Git.

## Install and run

From the project directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 5000
```

Open <http://127.0.0.1:5000> and sign in with a Jellyfin administrator account. Bind to `127.0.0.1` for a local-only instance. For remote access, put the app behind HTTPS and a trusted reverse proxy; set `SLEEVE_COOKIE_SECURE=1` so browsers only send the session cookie over HTTPS.

## Docker

Docker Compose runs Sleeve on `127.0.0.1:5000` by default. Install Docker Desktop on Windows/macOS or Docker Engine with the Compose plugin on Linux. From the project directory, copy `.env.example` to `.env` and edit `SLEEVE_MUSIC_PATH` to point to the host music directory:

```powershell
Copy-Item .env.example .env
# Edit .env, then:
docker compose up --build -d
```

Open <http://127.0.0.1:5000>. Set `SLEEVE_PORT` in `.env` to change the host port. Use `docker compose logs -f sleeve` to view logs, `docker compose down` to stop Sleeve, and `docker compose up -d` to start it again. `docker compose down -v` also deletes the named `sleeve-data` volume, including the SQLite profile, API key, cookies, downloads, and caches; do not use `-v` unless you intend to erase that data.

The named `sleeve-data` volume persists app data across container replacements. The host music directory is mounted read/write at `/music`; in **Automator → Edit settings**, configure the music folder as `/music`. Sleeve needs write access there to save completed downloads, repair lyrics, and rename tracks. On Linux, make sure the host directory is writable by UID/GID `10001` (or adapt the image's `sleeve` user to your host ownership). On Windows, use a Docker Desktop accessible path such as `C:/Users/you/Music` in `.env`.

Jellyfin must be reachable from inside the container. If Jellyfin runs on the same Windows/macOS host, use `http://host.docker.internal:8096` as its address in Sleeve. The Compose file provides this hostname on Linux too; for Jellyfin on another machine or in another Compose stack, use an address reachable from the Sleeve container. Avoid `localhost` for Jellyfin unless it is running in the same container. The Jellyfin API key remains in Sleeve's private data volume, and the Jellyfin sign-in password is not saved.

Optional provider values (`SEARXNG_URL`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`) and `SLEEVE_COOKIE_SECURE` can also be set in `.env`; Compose passes them to the container. Keep `.env` private. Set `SLEEVE_COOKIE_SECURE=1` when HTTPS is terminated by a trusted reverse proxy. The default image is CPU-based for Whisper. It installs FFmpeg and FFprobe from the Debian package repository; these are external tools with separate licenses, not binaries bundled in the Sleeve source.

## Configuration

- For native startup, copy `.env.example` to `.env` only if enabling optional Spotify credentials. Docker Compose also uses `.env` for the host music path and passes the supported provider/cookie values into the container. Keep `.env` local; it is ignored by Git.
- `SEARXNG_URL` is optional. Set it to your own SearXNG instance for video search/thumbnail lookup; yt-dlp search is used as a fallback where available.
- The first visit shows Sleeve's welcome screen. Continue to sign in with a Jellyfin administrator account; successful sign-in marks the first-run setup complete. The server address is stored in the SQLite Automator profile, while the Jellyfin password is discarded after authentication.
- Configure Jellyfin's server-to-server API key, the music folder, Whisper model/device, and backup behavior in **Automator → Edit settings**. Settings are stored in `automator_profile.sqlite3` in the private app-data directory; it contains a Jellyfin API key and must remain private. Sleeve does not import legacy JSON profiles. A one-time first-run reset clears the Automator profile and removes obsolete `automator_profile.json` and `jellyfin_auth.json` files; downloads, cookies, caches, and unrelated settings are preserved.
- The sign-in screen uses the Jellyfin URL from the Automator profile. On first sign-in, if the profile has no URL yet, enter the Jellyfin address; after successful authentication Sleeve saves that address into the same SQLite profile. There is no separate sign-in URL setting.
- Sign-in calls Jellyfin's `POST /Users/AuthenticateByName` endpoint. Sleeve does not save the submitted password or Jellyfin access token. The browser receives a random HTTP-only session cookie; sessions expire after 12 hours and end when the app restarts. Each account must be a Jellyfin administrator.
- Browser-cookie export is optional and only needed when YouTube requires sign-in. Cookies are written to `cookie.txt`/`cookies.txt`; keep both private.
- Search providers are enabled according to their available credentials. The Discover settings panel lets you choose which enabled providers are queried.
- Automator settings also lets you choose and order album tracklist sources. The app tries them in order and uses the first one that returns tracks; this avoids combining slightly different catalog versions into duplicate rows.
- Runtime data defaults to `%LOCALAPPDATA%\Sleeve` on Windows, `$XDG_DATA_HOME/sleeve` on Linux, and `~/Library/Application Support/Sleeve` on macOS. Set `SLEEVE_DATA_DIR` to choose another location. Existing installations automatically continue using the earlier `AAA Best Music App` data directory if it exists; the old `AAA_BEST_MUSIC_DATA_DIR` override also remains supported. On first run, existing root-level profiles, cookies, caches, and database files are copied there if no destination copy exists; originals are left untouched. Existing root-level downloads remain browseable, while new downloads go to the data directory.
- Temporary audio, database state, caches, and repair skip lists are kept in the data directory. `.gitignore` also protects legacy root-level local files from accidental commits.

## Troubleshooting

- **FFmpeg not found:** run `ffmpeg -version` and `ffprobe -version` in the same terminal used to launch the app; install both and fix `PATH` if either command is missing.
- **Jellyfin unavailable:** verify the base URL, API key, library permissions, and that the configured music folder is writable and inside the Jellyfin library path.
- **YouTube download fails:** update yt-dlp, confirm network access, then optionally export browser cookies after closing the browser. Do not share cookie files or logs containing request details.
- **Provider search is empty:** check provider status in Automator settings and configure optional credentials in `.env` where required. Some provider APIs are rate-limited or temporarily unavailable.
- **Whisper is slow:** select CPU or CUDA explicitly if automatic device selection is unsuitable; smaller models use less memory and finish sooner.
