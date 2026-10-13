# Contributing

Thanks for helping improve Sleeve. The project uses Python, FastAPI, Jinja templates, and plain JavaScript/CSS; there is no frontend build step.

## Before you start

1. Read [the setup guide](docs/SETUP.md) and create a local virtual environment.
2. Keep API keys, browser cookies, Jellyfin details, downloads, and generated caches out of commits. `.gitignore` covers the standard local files; check `git status` before committing.
3. For a larger change, open an issue first so the intended behavior can be discussed.

## Changes

- Keep backend behavior and existing API response shapes compatible unless a change is necessary and documented.
- Prefer small functions and accessible semantic HTML. Keep page-specific browser behavior in its page script and shared behavior in `static/app.js`.
- Do not commit downloaded media, browser cookies, local profiles, or FFmpeg binaries.
- Add or update focused tests for behavior changes. Run `python -m pytest` and `python -m compileall app.py app_paths.py automator.py providers.py` before submitting.
- Describe user-facing changes and any setup changes in the pull request.

## Pull requests

Include a short summary, the reason for the change, verification performed, and screenshots for visible UI changes. Keep unrelated formatting or cleanup out of the same change where practical.
