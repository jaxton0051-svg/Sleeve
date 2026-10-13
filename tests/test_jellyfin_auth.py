"""Focused tests for Jellyfin sign-in and local session behavior."""
from __future__ import annotations

import pytest

import jellyfin_auth as auth


@pytest.mark.parametrize(
    "value",
    [
        "",
        "jellyfin.example.com",
        "ftp://jellyfin.example.com",
        "https://user:password@jellyfin.example.com",
        "https://jellyfin.example.com/?token=secret",
        "https://jellyfin.example.com/#private",
        "https://jellyfin.example.com:invalid",
    ],
)
def test_validate_server_url_rejects_unsafe_or_invalid_addresses(value):
    with pytest.raises(auth.JellyfinLoginError):
        auth.validate_server_url(value)


def test_validate_server_url_normalizes_trailing_slashes():
    assert auth.validate_server_url(" https://jellyfin.example.com:8096/ ") == (
        "https://jellyfin.example.com:8096"
    )


def test_authenticate_admin_returns_identity_and_discards_access_token(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "AccessToken": "private-token",
                "User": {
                    "Id": "user-123",
                    "Name": "Jax",
                    "Policy": {"IsAdministrator": True},
                },
            }

    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(auth.requests, "post", fake_post)

    result = auth.authenticate_admin(
        "https://jellyfin.example.com/", " jax ", "password"
    )

    assert result == {"user_id": "user-123", "username": "Jax"}
    assert "private-token" not in repr(result)
    assert captured["url"] == "https://jellyfin.example.com/Users/AuthenticateByName"
    assert captured["json"] == {"Username": "jax", "Pw": "password"}
    assert captured["allow_redirects"] is False


def test_authenticate_admin_rejects_non_admin(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "AccessToken": "private-token",
                "User": {"Id": "user-123", "Name": "Jax", "Policy": {}},
            }

    monkeypatch.setattr(auth.requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(auth.JellyfinLoginError, match="administrator account"):
        auth.authenticate_admin("https://jellyfin.example.com", "jax", "password")


def test_authenticate_admin_converts_bad_credentials_to_safe_error(monkeypatch):
    class Response:
        status_code = 401

    monkeypatch.setattr(auth.requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(auth.JellyfinLoginError, match="did not accept"):
        auth.authenticate_admin("https://jellyfin.example.com", "jax", "wrong")


def test_sessions_expire_and_can_be_deleted(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])

    session_id = auth.create_session({"user_id": "user-123", "username": "Jax"})
    assert auth.get_session(session_id) == {"user_id": "user-123", "username": "Jax"}

    now[0] += auth.SESSION_TTL_SECONDS
    assert auth.get_session(session_id) is None

    session_id = auth.create_session({"user_id": "user-123", "username": "Jax"})
    auth.delete_session(session_id)
    assert auth.get_session(session_id) is None


def test_login_rate_limit_clears_after_window(monkeypatch):
    now = [100.0]
    key = "test-rate-limit-client"
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    auth.clear_failed_logins(key)

    for _ in range(auth._LOGIN_MAX_ATTEMPTS):
        auth.record_failed_login(key)
    assert auth.login_is_limited(key)

    now[0] += auth._LOGIN_WINDOW_SECONDS
    assert not auth.login_is_limited(key)
    auth.clear_failed_logins(key)
