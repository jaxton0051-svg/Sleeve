"""Jellyfin-backed sign-in and short-lived, server-side web sessions."""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlsplit

import requests

from app_paths import data_file

AUTH_CONFIG_FILE = data_file("jellyfin_auth.json")
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_COOKIE = "sleeve_session"

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()
_login_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_ATTEMPTS = 8


class JellyfinLoginError(Exception):
    """A user-facing sign-in failure without sensitive request details."""


def _prune_sessions(now: float) -> None:
    """Drop expired sessions; callers hold ``_sessions_lock``."""
    expired = [session_id for session_id, session in _sessions.items()
               if session.get("expires_at", 0) <= now]
    for session_id in expired:
        _sessions.pop(session_id, None)


def validate_server_url(value: str) -> str:
    url = (value or "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port  # Force validation of the optional numeric port.
    except ValueError as exc:
        raise JellyfinLoginError("Enter a valid Jellyfin server address, including http:// or https://.") from exc
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise JellyfinLoginError("Enter a valid Jellyfin server address, including http:// or https://.")
    return url


def configured_server_url(profile_url: str = "") -> str:
    """Resolve the server from environment, Automator settings, then saved setup."""
    env_url = os.environ.get("SLEEVE_JELLYFIN_URL") or os.environ.get("JELLYFIN_URL")
    if env_url:
        try:
            return validate_server_url(env_url)
        except JellyfinLoginError:
            return ""
    if profile_url:
        try:
            return validate_server_url(profile_url)
        except JellyfinLoginError:
            pass
    try:
        saved = json.loads(AUTH_CONFIG_FILE.read_text(encoding="utf-8"))
        if saved.get("server_url"):
            return validate_server_url(saved["server_url"])
    except (OSError, ValueError, AttributeError, JellyfinLoginError):
        pass
    return ""


def save_server_url(url: str) -> None:
    if os.environ.get("SLEEVE_JELLYFIN_URL") or os.environ.get("JELLYFIN_URL"):
        return
    AUTH_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_CONFIG_FILE.write_text(json.dumps({"server_url": url}, indent=2), encoding="utf-8")


def authenticate_admin(server_url: str, username: str, password: str) -> dict:
    """Authenticate with Jellyfin and require administrator policy."""
    url = validate_server_url(server_url)
    username = (username or "").strip()
    if not username or not password:
        raise JellyfinLoginError("Enter your Jellyfin username and password.")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": ('MediaBrowser Client="Sleeve", Device="Web Browser", '
                           'DeviceId="sleeve-web", Version="1.0.0"'),
        "User-Agent": "Sleeve/1.0",
    }
    try:
        response = requests.post(
            f"{url}/Users/AuthenticateByName",
            headers=headers,
            json={"Username": username, "Pw": password},
            timeout=(5, 12),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise JellyfinLoginError("Could not reach that Jellyfin server. Check its address and try again.") from exc

    if response.status_code in {401, 403}:
        raise JellyfinLoginError("Jellyfin did not accept that username and password.")
    if response.status_code != 200:
        raise JellyfinLoginError(f"Jellyfin returned HTTP {response.status_code} during sign-in.")
    try:
        result = response.json()
    except ValueError as exc:
        raise JellyfinLoginError("Jellyfin returned an unreadable sign-in response.") from exc

    if not isinstance(result, dict):
        raise JellyfinLoginError("Jellyfin returned an unreadable sign-in response.")
    user = result.get("User") or {}
    if not isinstance(user, dict):
        raise JellyfinLoginError("Jellyfin did not return a valid user session.")
    policy = user.get("Policy") or {}
    if not isinstance(policy, dict):
        policy = {}
    if not result.get("AccessToken") or not user.get("Id"):
        raise JellyfinLoginError("Jellyfin did not return a valid user session.")
    if not policy.get("IsAdministrator"):
        raise JellyfinLoginError("This app needs a Jellyfin administrator account because its tools can change library files.")

    # The Jellyfin access token is deliberately discarded. App features use the
    # operator's configured server-side API key; the browser only gets a random
    # opaque session identifier.
    return {"user_id": str(user["Id"]), "username": str(user.get("Name") or username)}


def create_session(user: dict) -> str:
    now = time.time()
    session_id = os.urandom(32).hex()
    with _sessions_lock:
        _prune_sessions(now)
        _sessions[session_id] = {**user, "expires_at": now + SESSION_TTL_SECONDS}
    return session_id


def get_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    now = time.time()
    with _sessions_lock:
        session = _sessions.get(session_id)
        if not session or session["expires_at"] <= now:
            _sessions.pop(session_id, None)
            return None
        return {key: value for key, value in session.items() if key != "expires_at"}


def delete_session(session_id: str | None) -> None:
    if session_id:
        with _sessions_lock:
            _sessions.pop(session_id, None)


def login_is_limited(client_key: str) -> bool:
    now = time.monotonic()
    with _attempts_lock:
        for key, recorded in list(_login_attempts.items()):
            recent = [stamp for stamp in recorded if now - stamp < _LOGIN_WINDOW_SECONDS]
            if recent:
                _login_attempts[key] = recent
            else:
                _login_attempts.pop(key, None)
        attempts = [stamp for stamp in _login_attempts.get(client_key, [])
                    if now - stamp < _LOGIN_WINDOW_SECONDS]
        _login_attempts[client_key] = attempts
        return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def record_failed_login(client_key: str) -> None:
    now = time.monotonic()
    with _attempts_lock:
        attempts = [stamp for stamp in _login_attempts.get(client_key, [])
                    if now - stamp < _LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        _login_attempts[client_key] = attempts


def clear_failed_logins(client_key: str) -> None:
    with _attempts_lock:
        _login_attempts.pop(client_key, None)
