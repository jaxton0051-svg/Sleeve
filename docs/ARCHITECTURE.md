# Architecture and service notes

## Application structure

- `app.py` serves the FastAPI application, Jinja pages, API endpoints, chart feeds, download jobs, and repair endpoints.
- `templates/index.html` is the shared page shell. The `active_tab` value selects Library, Discover, Automator, or Repair content.
- `static/app.js` owns shared cookie, download-queue, file-list, and Library behavior. `static/discover.js`, `static/automator.js`, and `static/repair.js` own their respective workflows.
- `providers.py` adapts album and song search/tracklist providers to a common shape and deduplicates results.
- `automator.py` contains Jellyfin integration, download verification/tagging, lyric lookup/transcription, and library repair operations.
- `cookie_cleaner.py` normalizes exported browser cookie files. `requirements.txt` lists the runtime Python dependencies.
- `app_paths.py` selects the per-user data directory and migrates legacy local files by copying them once; the source checkout remains the code/config template location.
- `jellyfin_auth.py` checks Jellyfin administrator credentials and keeps random, expiring web sessions in process memory. The browser cookie contains no Jellyfin access token.

The browser calls the existing JSON endpoints directly. Keep DOM IDs consumed by the page scripts and response shapes consumed by those scripts aligned when changing a workflow. Static assets are served with revalidation headers; update the asset version in the template when cache behavior changes.

All application pages and APIs require an active Jellyfin administrator session. The login route and static files are public. Sessions expire after 12 hours or when the server restarts; the default deployment is one Uvicorn process. Jellyfin's administrator API key remains server-side in the Automator profile for library operations.

## External services and attribution

- **Apple/iTunes Search and RSS feeds** provide album artwork, charts, and catalog metadata. Artwork and catalog data remain the property of their respective owners.
- **Apple's top-songs RSS feed** supplies the Discover top 10 and its cover art. Billboard is a fallback when Apple is unavailable; its chart results may use the app's placeholder artwork. A small built-in sample is used if both feeds fail.
- **MusicBrainz, Deezer, TheAudioDB, and Spotify** may be queried by the provider aggregator. Spotify requires optional Client ID/Secret credentials. Respect each provider's current API terms, rate limits, and attribution requirements.
- **LRCLIB** provides community-maintained lyric matches via its public API. Lyrics are third-party content; respect the service's usage policy and local copyright rules.
- **YouTube via yt-dlp** is used for user-requested audio retrieval. Users are responsible for following applicable site terms and copyright law.
- **FFmpeg/FFprobe** are external tools installed by the operator. This project does not redistribute their binaries.

External APIs may change, throttle requests, or be unavailable. The interface reports provider failures where possible; chart feeds and cached data can serve as fallbacks.
