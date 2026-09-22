import json
import os
import stat

from lamplight import config


def test_settings_reject_junk_and_fall_back():
    settings = config.Settings.from_dict({"port": "8080", "document_root": "   ", "host": ""})
    assert settings == config.Settings()


def test_settings_accept_valid_values():
    settings = config.Settings.from_dict({"port": 8080, "document_root": "/srv/www"})
    assert (settings.port, settings.document_root) == (8080, "/srv/www")


def test_theme_is_not_a_setting():
    """The panel is dark-only; a stray theme key must not become an attribute."""
    assert not hasattr(config.Settings.from_dict({"theme": "light"}), "theme")


def test_corrupt_settings_file_is_replaced(paths):
    paths.settings_path.write_text("{not json", encoding="utf-8")
    assert config.load_settings(paths) == config.Settings()


def test_token_is_stable_and_private(paths):
    token = config.load_or_create_token(paths)
    assert len(token) >= config.MIN_TOKEN_LENGTH
    assert config.load_or_create_token(paths) == token
    assert json.loads(paths.auth_path.read_text())["token"] == token
    assert stat.S_IMODE(os.stat(paths.auth_path).st_mode) == 0o600


def test_short_token_is_regenerated(paths):
    paths.auth_path.write_text(json.dumps({"token": "tiny"}), encoding="utf-8")
    assert config.load_or_create_token(paths) != "tiny"


def test_settings_round_trip(paths):
    config.save_settings(paths, config.Settings(port=9000, document_root="/srv/site"))
    reloaded = config.load_settings(paths)
    assert reloaded.port == 9000
    assert reloaded.document_root == "/srv/site"
