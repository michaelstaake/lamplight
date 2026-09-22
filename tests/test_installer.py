import hashlib
import tarfile
from pathlib import Path

import pytest

from lamplight import db, systemops
from lamplight import installer as installer_module
from lamplight.installer import MAILPIT_SHA256, MAILPIT_UNIT, MAILPIT_VERSION, Installer
from lamplight.jobs import JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore(db.connect(tmp_path / "jobs.sqlite3"))


@pytest.fixture
def installer(paths, store, monkeypatch):
    """A real Installer that believes it is root and never shells out.

    Every argv it builds still lands in the job log, which is what these tests
    read — the point is the command, not the effect.
    """
    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(systemops, "which", lambda name: "/usr/sbin/" + name)
    monkeypatch.setattr(
        Installer, "_run", lambda self, job_id, argv: self._log(job_id, "$ " + " ".join(argv)) or 0
    )
    return Installer(paths, store)


def logged(store, job_id):
    return store.get(job_id)["log"]


def test_unknown_component_raises(installer):
    with pytest.raises(ValueError, match="Unknown component"):
        installer.install(1, "mysql")


def test_serviceless_component_cannot_be_started(installer, store):
    job_id = store.create("php", "start")
    with pytest.raises(ValueError, match="no systemd service"):
        installer.service_action(job_id, "php", "start")


def test_unknown_service_action_raises(installer, store):
    job_id = store.create("apache", "explode")
    with pytest.raises(ValueError, match="Unknown service action"):
        installer.service_action(job_id, "apache", "explode")


def test_enable_and_disable_pass_now(installer, store):
    """--now is what makes enable/disable take effect immediately; it used to be
    built into a list that was then thrown away."""
    job_id = store.create("apache", "enable")
    installer.service_action(job_id, "apache", "enable")
    installer.service_action(job_id, "apache", "disable")
    log = logged(store, job_id)
    assert "systemctl enable --now apache2" in log
    assert "systemctl disable --now apache2" in log


def test_plain_service_actions_do_not_pass_now(installer, store):
    job_id = store.create("apache", "restart")
    installer.service_action(job_id, "apache", "restart")
    assert "systemctl restart apache2" in logged(store, job_id)


def test_install_runs_apt_with_the_component_packages(installer, store, monkeypatch):
    monkeypatch.setattr(
        installer_module.subprocess, "Popen", lambda *a, **k: pytest.fail("ran a real command")
    )
    job_id = store.create("apache", "install")
    installer.install(job_id, "apache")
    log = logged(store, job_id)
    assert "apt-get update" in log
    assert "apt-get install -y --no-install-recommends apache2" in log


def test_apt_remove_covers_extra_packages(installer, store):
    """mariadb-client is an extra package; leaving it behind was a real bug."""
    job_id = store.create("mariadb", "remove")
    installer.remove(job_id, "mariadb")
    assert "mariadb-server, mariadb-client" in logged(store, job_id)


def test_install_refuses_when_requirements_are_missing(installer, store, monkeypatch):
    monkeypatch.setattr(
        systemops, "package_states", lambda names: {n: systemops.PackageState() for n in names}
    )
    job_id = store.create("phpmyadmin", "install")
    with pytest.raises(RuntimeError, match="Install these first: apache, php, mariadb"):
        installer.install(job_id, "phpmyadmin")


def test_install_requires_root(paths, store, monkeypatch):
    monkeypatch.setattr(systemops, "is_root", lambda: False)
    real = Installer(paths, store)
    with pytest.raises(PermissionError, match="must run as root"):
        real.install(store.create("apache", "install"), "apache")


def test_the_firewall_is_refused_when_ufw_is_not_installed(paths, store, monkeypatch):
    """No ufw means the button would change nothing, so it fails loudly."""
    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(systemops, "which", lambda name: None)
    real = Installer(paths, store)
    with pytest.raises(RuntimeError, match="ufw is not installed"):
        real.firewall_action(store.create("apache", "firewall-open"), "apache", "firewall-open")


def test_mailpit_unit_binds_to_loopback_only():
    unit = MAILPIT_UNIT.format(binary="/usr/local/bin/mailpit", state_dir="lamplight-mailpit")
    assert "--listen 127.0.0.1:8025" in unit
    assert "--smtp 127.0.0.1:1025" in unit
    assert "DynamicUser=yes" in unit
    assert "0.0.0.0" not in unit


def test_pinned_checksums_look_like_sha256():
    assert MAILPIT_VERSION.startswith("v")
    assert set(MAILPIT_SHA256) == {"amd64", "arm64"}
    for digest in MAILPIT_SHA256.values():
        assert len(digest) == 64 and int(digest, 16) >= 0


def _fake_release(tmp_path: Path, payload: bytes) -> Path:
    binary = tmp_path / "mailpit"
    binary.write_bytes(payload)
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname="mailpit")
        tar.add(binary, arcname="README.md")
    return archive


def test_extract_pulls_out_only_the_binary(paths, store, tmp_path):
    archive = _fake_release(tmp_path, b"#!/bin/true\n")
    dest = tmp_path / "out" / "mailpit"
    Installer(paths, store)._extract_binary(
        store.create("mailpit", "install"), archive, "mailpit", dest
    )
    assert dest.read_bytes() == b"#!/bin/true\n"
    assert dest.stat().st_mode & 0o111
    assert not (dest.parent / "README.md").exists()


def test_download_rejects_a_tampered_archive(paths, store, monkeypatch, tmp_path):
    payload = b"not the real mailpit"

    class FakeResponse:
        def __init__(self):
            self._sent = False

        def read(self, _size):
            if self._sent:
                return b""
            self._sent = True
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(installer_module.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    real = Installer(paths, store)
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        real._download_mailpit(store.create("mailpit", "install"), "amd64")
    assert list(paths.download_dir.iterdir()) == [], "the bad download must not be left behind"


def test_download_accepts_a_matching_archive(paths, store, monkeypatch):
    payload = b"the real mailpit, honest"
    monkeypatch.setitem(MAILPIT_SHA256, "amd64", hashlib.sha256(payload).hexdigest())

    class FakeResponse:
        def __init__(self):
            self._sent = False

        def read(self, _size):
            if self._sent:
                return b""
            self._sent = True
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(installer_module.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    real = Installer(paths, store)
    archive = real._download_mailpit(store.create("mailpit", "install"), "amd64")
    assert archive.read_bytes() == payload
    archive.unlink()


def test_firewall_open_adds_a_rule_per_port(installer, store):
    job_id = store.create("apache", "firewall-open")
    installer.firewall_action(job_id, "apache", "firewall-open")
    log = logged(store, job_id)
    assert "ufw allow 80/tcp" in log
    assert "ufw allow 443/tcp" in log


def test_firewall_close_deletes_the_same_rules(installer, store):
    job_id = store.create("apache", "firewall-close")
    installer.firewall_action(job_id, "apache", "firewall-close")
    log = logged(store, job_id)
    assert "ufw delete allow 80/tcp" in log
    assert "ufw delete allow 443/tcp" in log


def test_firewall_is_refused_for_components_that_declare_no_ports(installer, store):
    job_id = store.create("mariadb", "firewall-open")
    with pytest.raises(ValueError, match="does not manage firewall ports"):
        installer.firewall_action(job_id, "mariadb", "firewall-open")


def test_unknown_firewall_action_raises(installer, store):
    job_id = store.create("apache", "firewall-melt")
    with pytest.raises(ValueError, match="Unknown firewall action"):
        installer.firewall_action(job_id, "apache", "firewall-melt")
