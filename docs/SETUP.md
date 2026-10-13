# Setup and configuration

## Requirements

- Python 3.10 or newer.
- FFmpeg and FFprobe available on `PATH`. On Windows, install a trusted FFmpeg build and add its `bin` directory to `PATH`; on macOS or Linux, install FFmpeg through the platform package manager. Restart the terminal after changing `PATH`.
- A Jellyfin server and API key are optional for the Library/Automator workflows. Basic Discover and direct download features can be used without Jellyfin.

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

Open <http://127.0.0.1:5000>. Bind to `127.0.0.1` for a local-only instance. Do not expose the app to an untrusted network: it has no user authentication and offers operations against configured local files and services.

## Configuration

- Copy `.env.example` to `.env` only if enabling optional Spotify credentials. Keep `.env` local; it is ignored by Git.
- `SEARXNG_URL` is optional. Set it to your own SearXNG instance for video search/thumbnail lookup; yt-dlp search is used as a fallback where available.
- Configure Jellyfin, the music folder, Whisper model/device, and backup behavior in **Automator → Edit settings**. This creates `automator_profile.json`; it contains a Jellyfin API key and must remain private.
- Browser-cookie export is optional and only needed when YouTube requires sign-in. Cookies are written to `cookie.txt`/`cookies.txt`; keep both private.
- Search providers are enabled according to their available credentials. The Discover settings panel lets you choose which enabled providers are queried.
- Runtime data defaults to `%LOCALAPPDATA%\Sleeve` on Windows, `$XDG_DATA_HOME/sleeve` on Linux, and `~/Library/Application Support/Sleeve` on macOS. Set `SLEEVE_DATA_DIR` to choose another location. Existing installations automatically continue using the earlier `AAA Best Music App` data directory if it exists; the old `AAA_BEST_MUSIC_DATA_DIR` override also remains supported. On first run, existing root-level profiles, cookies, caches, and database files are copied there if no destination copy exists; originals are left untouched. Existing root-level downloads remain browseable, while new downloads go to the data directory.
- Temporary audio, database state, caches, and repair skip lists are kept in the data directory. `.gitignore` also protects legacy root-level local files from accidental commits.

## Troubleshooting

- **FFmpeg not found:** run `ffmpeg -version` and `ffprobe -version` in the same terminal used to launch the app; install both and fix `PATH` if either command is missing.
- **Jellyfin unavailable:** verify the base URL, API key, library permissions, and that the configured music folder is writable and inside the Jellyfin library path.
- **YouTube download fails:** update yt-dlp, confirm network access, then optionally export browser cookies after closing the browser. Do not share cookie files or logs containing request details.
- **Provider search is empty:** check provider status in Automator settings and configure optional credentials in `.env` where required. Some provider APIs are rate-limited or temporarily unavailable.
- **Whisper is slow:** select CPU or CUDA explicitly if automatic device selection is unsuitable; smaller models use less memory and finish sooner.
