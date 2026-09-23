"""Vhost names, the site files they become, and the reload that follows."""

import os
import subprocess
from pathlib import Path

import pytest

from lamplight import systemops, vhosts

DEFAULT_CONF = """<VirtualHost *:80>
	# The ServerName directive sets the request scheme, hostname and port that
	#ServerName www.example.com

	ServerAdmin webmaster@localhost
	DocumentRoot /var/www/html

	ErrorLog ${APACHE_LOG_DIR}/error.log
	CustomLog ${APACHE_LOG_DIR}/access.log combined
</VirtualHost>
"""


def _layout(monkeypatch, tmp_path):
    available = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    available.mkdir()
    enabled.mkdir()
    (available / "000-default.conf").write_text(DEFAULT_CONF, encoding="utf-8")
    (enabled / "000-default.conf").write_text(DEFAULT_CONF, encoding="utf-8")
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    monkeypatch.setattr(vhosts, "SITES_AVAILABLE", available)
    monkeypatch.setattr(vhosts, "SITES_ENABLED", enabled)
    monkeypatch.setattr(vhosts, "HOSTS_FILE", hosts)
    monkeypatch.setattr(systemops, "is_root", lambda: True)

    def fake_symlink(target, link, target_is_directory=False):
        Path(link).write_text("enabled\n", encoding="utf-8")

    monkeypatch.setattr(os, "symlink", fake_symlink)
    return available, enabled, hosts


def test_names_are_hostnames_and_folders_are_single_paths():
    assert vhosts.clean_name(" App.Test. ") == "app.test"
    assert vhosts.clean_folder("/var/www/app.test/") == "/var/www/app.test"
    for name in ("default", "000-default", "app test", "app\nInclude", "../x", ""):
        with pytest.raises(ValueError):
            vhosts.clean_name(name)
    for folder in (
        "var/www",
        "/var/www/my site",
        "/var/www/../etc",
        "/var/www/html\nInclude /etc/passwd",
        "/",
    ):
        with pytest.raises(ValueError):
            vhosts.clean_folder(folder)
    with pytest.raises(ValueError, match="invalid vhost"):
        vhosts.clean_site_id("../000-default")


def test_the_default_site_keeps_its_name_and_ignores_a_commented_servername():
    parsed = vhosts.parse_site("000-default", DEFAULT_CONF)
    assert parsed == {
        "id": "000-default",
        "name": "default",
        "folder": "/var/www/html",
        "default": True,
    }
    other = vhosts.parse_site("app.test", "ServerName app.test\nDocumentRoot /var/www/app\n")
    assert other["name"] == "app.test"
    assert other["folder"] == "/var/www/app"
    assert other["default"] is False


def test_replacing_the_document_root_leaves_the_rest_of_the_file():
    updated = vhosts.replace_document_root(DEFAULT_CONF, "/srv/site")
    assert "DocumentRoot /srv/site" in updated
    assert "DocumentRoot /var/www/html" not in updated
    assert "ServerAdmin webmaster@localhost" in updated
    assert "#ServerName www.example.com" in updated
    with pytest.raises(vhosts.ApacheError, match="no DocumentRoot"):
        vhosts.replace_document_root("<VirtualHost *:80>\n</VirtualHost>\n", "/srv/site")


def test_render_and_hosts_lines_quote_nothing_they_were_not_given():
    text = vhosts.render_vhost("app.test", "/var/www/app.test")
    assert "ServerName app.test\n" in text
    assert "DocumentRoot /var/www/app.test\n" in text
    assert "${APACHE_LOG_DIR}/app.test-access.log" in text

    added = vhosts.hosts_with("127.0.0.1 localhost\n", "app.test")
    assert added == "127.0.0.1 localhost\n127.0.0.1 app.test # lamplight\n"
    assert vhosts.hosts_with(added, "app.test") == added
    # An existing mapping without our marker is left alone, and so is its line on delete.
    preexisting = "127.0.0.1 app.test\n"
    assert vhosts.hosts_with(preexisting, "app.test") == preexisting
    removed = vhosts.hosts_without(added + "127.0.0.1 app.test\n", "app.test")
    assert "lamplight" not in removed
    assert "127.0.0.1 app.test\n" in removed
    assert "127.0.0.1 localhost\n" in vhosts.hosts_without(added, "app.test")


def test_list_shows_the_default_site_first_and_skips_stray_files(monkeypatch, tmp_path):
    available, enabled, _hosts = _layout(monkeypatch, tmp_path)
    (available / "app.test.conf").write_text(
        "ServerName app.test\nDocumentRoot /var/www/app\n", encoding="utf-8"
    )
    (enabled / "app.test.conf").write_text("enabled\n", encoding="utf-8")
    (enabled / "bad name.conf").write_text("DocumentRoot /tmp/nope\n", encoding="utf-8")
    (enabled / "notes.txt").write_text("nope\n", encoding="utf-8")

    listed = vhosts.list_vhosts()
    assert [item["name"] for item in listed] == ["default", "app.test"]
    assert listed[0]["folder"] == "/var/www/html"
    assert listed[0]["default"] is True
    assert listed[1]["folder"] == "/var/www/app"


def test_enable_site_uses_a_relative_symlink(monkeypatch, tmp_path):
    _available, enabled, _hosts = _layout(monkeypatch, tmp_path)
    seen = {}

    def fake_symlink(target, link, target_is_directory=False):
        seen["target"] = Path(target)
        seen["link"] = Path(link)

    monkeypatch.setattr(os, "symlink", fake_symlink)
    vhosts.enable_site("app.test")
    assert seen["target"] == Path("..") / "sites-available" / "app.test.conf"
    assert seen["link"] == enabled / "app.test.conf"

    def denied(*_args, **_kwargs):
        raise OSError("denied")

    monkeypatch.setattr(os, "symlink", denied)
    with pytest.raises(vhosts.ApacheError, match="enable"):
        vhosts.enable_site("shop.test")


def test_create_writes_the_site_and_a_failed_reload_removes_it(monkeypatch, tmp_path):
    available, enabled, hosts = _layout(monkeypatch, tmp_path)
    made = []
    monkeypatch.setattr(vhosts, "ensure_folder", lambda folder: made.append(folder))
    monkeypatch.setattr(vhosts, "reload_apache", lambda: None)

    vhosts.create_vhost("app.test", "/var/www/app")
    text = (available / "app.test.conf").read_text(encoding="utf-8")
    assert "ServerName app.test" in text
    assert "DocumentRoot /var/www/app" in text
    assert (enabled / "app.test.conf").is_symlink() or (enabled / "app.test.conf").is_file()
    assert "127.0.0.1 app.test # lamplight" in hosts.read_text(encoding="utf-8")
    assert made == ["/var/www/app"]

    def boom():
        raise vhosts.ApacheError("Syntax error on line 3")

    monkeypatch.setattr(vhosts, "reload_apache", boom)
    with pytest.raises(vhosts.ApacheError, match="Syntax error"):
        vhosts.create_vhost("shop.test", "/var/www/shop")
    assert not (available / "shop.test.conf").exists()
    assert "shop.test" not in hosts.read_text(encoding="utf-8")


def test_set_folder_keeps_the_old_file_when_reload_fails(monkeypatch, tmp_path):
    available, _enabled, _hosts = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(vhosts, "ensure_folder", lambda folder: None)
    monkeypatch.setattr(
        vhosts, "reload_apache", lambda: (_ for _ in ()).throw(vhosts.ApacheError("nope"))
    )
    original = (available / "000-default.conf").read_text(encoding="utf-8")
    with pytest.raises(vhosts.ApacheError, match="nope"):
        vhosts.set_folder("000-default", "/srv/site")
    assert (available / "000-default.conf").read_text(encoding="utf-8") == original

    monkeypatch.setattr(vhosts, "reload_apache", lambda: None)
    vhosts.set_folder("000-default", "/srv/site")
    updated = (available / "000-default.conf").read_text(encoding="utf-8")
    assert "DocumentRoot /srv/site" in updated
    assert "#ServerName www.example.com" in updated


def test_the_default_site_cannot_be_deleted_and_another_can(monkeypatch, tmp_path):
    available, enabled, hosts = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(vhosts, "ensure_folder", lambda folder: None)
    monkeypatch.setattr(vhosts, "reload_apache", lambda: None)
    vhosts.create_vhost("app.test", "/var/www/app")
    folder = tmp_path / "untouched"
    folder.mkdir()
    logs = tmp_path / "apache2"
    logs.mkdir()
    monkeypatch.setattr(vhosts, "APACHE_LOG_DIR", logs)
    for name in ("app.test-access.log", "app.test-error.log", "error.log", "access.log"):
        (logs / name).write_text("line\n", encoding="utf-8")
    outside = tmp_path / "outside.log"
    outside.write_text("keep\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot be deleted"):
        vhosts.delete_vhost("000-default")
    assert (available / "000-default.conf").is_file()

    vhosts.delete_vhost("app.test")
    assert not (available / "app.test.conf").exists()
    assert not (enabled / "app.test.conf").exists()
    assert "app.test" not in hosts.read_text(encoding="utf-8")
    assert "127.0.0.1 localhost" in hosts.read_text(encoding="utf-8")
    assert folder.is_dir()
    assert not (logs / "app.test-access.log").exists()
    assert not (logs / "app.test-error.log").exists()
    assert (logs / "error.log").is_file()
    assert (logs / "access.log").is_file()
    assert outside.is_file()


def test_a_failed_delete_puts_the_site_back(monkeypatch, tmp_path):
    available, _enabled, hosts = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(vhosts, "ensure_folder", lambda folder: None)
    monkeypatch.setattr(vhosts, "reload_apache", lambda: None)
    vhosts.create_vhost("app.test", "/var/www/app")
    original = (available / "app.test.conf").read_text(encoding="utf-8")
    logs = tmp_path / "apache2"
    logs.mkdir()
    monkeypatch.setattr(vhosts, "APACHE_LOG_DIR", logs)
    access = logs / "app.test-access.log"
    access.write_text("line\n", encoding="utf-8")

    def boom():
        raise vhosts.ApacheError("reload failed")

    monkeypatch.setattr(vhosts, "reload_apache", boom)
    with pytest.raises(vhosts.ApacheError, match="reload failed"):
        vhosts.delete_vhost("app.test")
    assert (available / "app.test.conf").read_text(encoding="utf-8") == original
    assert "127.0.0.1 app.test # lamplight" in hosts.read_text(encoding="utf-8")
    assert access.is_file()


def test_log_paths_stay_inside_the_apache_log_directory(tmp_path, monkeypatch):
    logs = tmp_path / "apache2"
    logs.mkdir()
    monkeypatch.setattr(vhosts, "APACHE_LOG_DIR", logs)
    (logs / "app.test-access.log").write_text("line\n", encoding="utf-8")
    (logs / "error.log").write_text("shared\n", encoding="utf-8")
    outside = tmp_path / "secret.log"
    outside.write_text("nope\n", encoding="utf-8")
    text = (
        "ErrorLog ${APACHE_LOG_DIR}/app.test-error.log\n"
        "CustomLog ${APACHE_LOG_DIR}/app.test-access.log combined\n"
        "ErrorLog ${APACHE_LOG_DIR}/error.log\n"
        f"CustomLog {outside} combined\n"
        'ErrorLog "|/usr/bin/rotatelogs /tmp/x 86400"\n'
    )
    assert vhosts.log_paths(text) == [logs / "app.test-access.log"]


def test_changes_require_root(monkeypatch, tmp_path):
    _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(systemops, "is_root", lambda: False)
    with pytest.raises(PermissionError, match="must run as root"):
        vhosts.create_vhost("app.test", "/var/www/app")
    with pytest.raises(PermissionError, match="must run as root"):
        vhosts.set_folder("000-default", "/srv/site")
    with pytest.raises(PermissionError, match="must run as root"):
        vhosts.delete_vhost("app.test")


def test_ensure_folder_creates_a_directory_and_refuses_a_file(tmp_path):
    target = tmp_path / "sites" / "app"
    vhosts.ensure_folder(str(target))
    assert target.is_dir()
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(vhosts.ApacheError, match="is a file"):
        vhosts.ensure_folder(str(file_path))


def test_reload_checks_config_and_skips_a_stopped_server(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "configtest":
            return subprocess.CompletedProcess(argv, 0, "", "Syntax OK")
        if "is-active" in argv:
            return subprocess.CompletedProcess(argv, 3, "", "inactive")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(systemops, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(systemops, "run", fake_run)
    vhosts.reload_apache()
    assert calls[0][1] == "configtest"
    assert ["systemctl", "reload", "apache2"] not in calls

    def bad_config(argv, **kwargs):
        if argv[1] == "configtest":
            return subprocess.CompletedProcess(argv, 1, "", "AH00526: Syntax error on line 4")
        raise AssertionError(argv)

    monkeypatch.setattr(systemops, "run", bad_config)
    with pytest.raises(vhosts.ApacheError, match="Syntax error"):
        vhosts.reload_apache()


def test_reload_reloads_a_running_server(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(systemops, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(systemops, "run", fake_run)
    vhosts.reload_apache()
    assert calls[-1] == ["systemctl", "reload", "apache2"]
