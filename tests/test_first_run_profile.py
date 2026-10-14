import automator


def test_first_run_reset_removes_only_obsolete_profiles_and_disables_import(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "app-data"
    project_dir = tmp_path / "checkout"
    data_dir.mkdir()
    project_dir.mkdir()
    database = data_dir / "automator_profile.sqlite3"
    legacy_profile = data_dir / "automator_profile.json"
    legacy_auth = data_dir / "jellyfin_auth.json"
    legacy_checkout_profile = project_dir / "automator_profile.json"
    preserved_cookie = data_dir / "cookie.txt"
    legacy_profile.write_text('{"jellyfin_url":"https://old.example"}', encoding="utf-8")
    legacy_auth.write_text('{"server_url":"https://old.example"}', encoding="utf-8")
    legacy_checkout_profile.write_text('{"music_folder":"old folder"}', encoding="utf-8")
    preserved_cookie.write_text("existing cookie data", encoding="utf-8")

    monkeypatch.setattr(automator, "DATA_DIR", data_dir)
    monkeypatch.setattr(automator, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(automator, "PROFILE_DB_FILE", database)
    monkeypatch.setattr(automator, "PROFILE_FILE", legacy_profile)

    profile = automator.load_profile()

    assert profile.jellyfin_url == ""
    assert profile.music_folder == ""
    assert profile.setup_complete is False
    assert not legacy_profile.exists()
    assert not legacy_auth.exists()
    assert not legacy_checkout_profile.exists()
    assert preserved_cookie.read_text(encoding="utf-8") == "existing cookie data"


def test_setup_completion_persists_and_reset_is_not_repeated(tmp_path, monkeypatch):
    data_dir = tmp_path / "app-data"
    data_dir.mkdir()
    database = data_dir / "automator_profile.sqlite3"
    monkeypatch.setattr(automator, "DATA_DIR", data_dir)
    monkeypatch.setattr(automator, "PROFILE_DB_FILE", database)
    monkeypatch.setattr(automator, "PROFILE_FILE", data_dir / "automator_profile.json")
    monkeypatch.setattr(automator, "PROJECT_DIR", tmp_path / "checkout")
    (tmp_path / "checkout").mkdir()

    profile = automator.load_profile()
    profile.jellyfin_url = "https://jellyfin.example"
    profile.setup_complete = True
    automator.save_profile(profile)

    reloaded = automator.load_profile()
    assert reloaded.jellyfin_url == "https://jellyfin.example"
    assert reloaded.setup_complete is True

    # A subsequent load must not clear the newly completed profile.
    reloaded.music_folder = "D:/Music"
    automator.save_profile(reloaded)
    assert automator.load_profile().music_folder == "D:/Music"
