"""Fixtures that stand a whole app up against a machine that is not real.

Only the two places that touch the host are replaced — the status probe and the
installer that shells out to apt and systemctl. Routes, auth, jobs, templates
and the job database are the real ones, so a test that passes here exercises
what ships.
"""

import subprocess

import pytest

from lamplight import config, logsources, php, status, systemops, vhosts
from lamplight.app import create_app
from lamplight.catalog import COMPONENTS, get_component
from lamplight.firewall import FIREWALL_CLOSE, FIREWALL_OPEN

# Stand-in figures, so the dashboard has something to add up without a cgroup
# behind it.
SERVICE_MEMORY = 24 * 1024 * 1024
OWN_MEMORY = 30 * 1024 * 1024
JOURNAL_LINE = "Sep 11 08:00:00 host unit[1]: ready\n"


@pytest.fixture
def paths(tmp_path):
    made = config.AppPaths(data_dir=tmp_path / "data", config_dir=tmp_path / "cfg")
    made.ensure()
    return made


def _blank() -> dict:
    return {
        "installed": False,
        "active": False,
        "enabled": False,
        "version": None,
        "memory": None,
        # ufw answers here, so the firewall button is reachable in tests.
        "firewall_active": True,
        "firewall_allowed": False,
    }


class FakeHost:
    """A machine with nothing on it that remembers what the panel did to it."""

    def __init__(self, apache_logs, mysql_logs):
        self.components = {component.id: _blank() for component in COMPONENTS}
        self.apache_logs = apache_logs
        self.mysql_logs = mysql_logs

    def probe(self, php_config=None) -> status.HostProbe:
        return status.HostProbe(components=self.components, own_memory=OWN_MEMORY)

    def apply(self, component_id: str, action: str) -> None:
        slot = self.components[component_id]
        has_service = bool(get_component(component_id).service)
        if action == "install":
            slot.update(installed=True, active=has_service, enabled=has_service, version="1.0")
        elif action == "remove":
            slot.update(installed=False, active=False, enabled=False, version=None)
        elif action in {"start", "restart"}:
            slot["active"] = True
        elif action == "stop":
            slot["active"] = False
        elif action == "enable":
            slot.update(enabled=True, active=True)
        elif action == "disable":
            slot.update(enabled=False, active=False)
        elif action == FIREWALL_OPEN:
            slot["firewall_allowed"] = True
        elif action == FIREWALL_CLOSE:
            slot["firewall_allowed"] = False
        slot["memory"] = SERVICE_MEMORY if slot["active"] else None


class FakeInstaller:
    """The same calls the app makes on Installer, with no apt and no systemctl.

    The real one is covered directly in test_installer.py and test_php.py; here
    it only has to move the fake host and write something to the job log.
    """

    host: FakeHost  # bound per test by the `host` fixture

    def __init__(self, paths, store):
        self.paths = paths
        self.store = store

    def install(self, job_id, component_id):
        self._act(job_id, component_id, "install")

    def remove(self, job_id, component_id):
        self._act(job_id, component_id, "remove")

    def service_action(self, job_id, component_id, action):
        self._act(job_id, component_id, action)

    def firewall_action(self, job_id, component_id, action):
        self._act(job_id, component_id, action)

    def set_php_extensions(self, job_id, desired):
        previous = php.load_config(self.paths)
        php.save_config(
            self.paths,
            php.PhpConfig(extensions=desired, options=previous.options, version=previous.version),
        )
        self._log(job_id, "[job] extensions: " + (", ".join(desired) or "none"))

    def apply_php_options(self, job_id, options):
        previous = php.load_config(self.paths)
        php.save_config(
            self.paths,
            php.PhpConfig(
                extensions=previous.extensions, options=options, version=previous.version
            ),
        )
        self._log(job_id, "[job] options: " + (", ".join(options) or "none"))

    def set_php_version(self, job_id, version):
        previous = php.load_config(self.paths)
        php.save_config(
            self.paths,
            php.PhpConfig(
                extensions=previous.extensions, options=previous.options, version=version
            ),
        )
        self._log(job_id, "[job] version: " + (version or "distro"))

    def _act(self, job_id, component_id, action):
        self._log(job_id, f"[job] {action} {component_id}")
        self.host.apply(component_id, action)
        self._log(job_id, "[job] done")

    def _log(self, job_id, text):
        self.store.append_log(job_id, text + "\n")


def _seed_logs(tmp_path):
    """A stand-in /var/log/apache2 and /var/log/mysql with something in them."""
    apache = tmp_path / "log-apache"
    mysql = tmp_path / "log-mysql"
    apache.mkdir()
    mysql.mkdir()
    (apache / "error.log").write_text("apache error line\n", encoding="utf-8")
    (apache / "access.log").write_text('127.0.0.1 - - "GET / HTTP/1.1" 200\n', encoding="utf-8")
    (mysql / "error.log").write_text("mariadb error line\n", encoding="utf-8")
    return apache, mysql


@pytest.fixture
def host(monkeypatch, tmp_path):
    apache_logs, mysql_logs = _seed_logs(tmp_path)
    fake = FakeHost(apache_logs, mysql_logs)

    monkeypatch.setattr(status, "_probe", fake.probe)
    monkeypatch.setattr(
        "lamplight.app.Installer", type("BoundInstaller", (FakeInstaller,), {"host": fake})
    )

    # The PHP page must not read this machine's /etc/php or its dpkg database.
    monkeypatch.setattr(php, "PHP_ETC", tmp_path / "etc-php")
    monkeypatch.setattr(
        systemops, "package_states", lambda names: {n: systemops.PackageState() for n in names}
    )
    monkeypatch.setattr(systemops, "php_modules", list)

    monkeypatch.setattr(logsources, "APACHE_LOG_DIR", apache_logs)
    monkeypatch.setattr(logsources, "MYSQL_LOG_DIR", mysql_logs)
    real_which = systemops.which
    monkeypatch.setattr(
        systemops,
        "which",
        lambda name: "/usr/bin/journalctl" if name == "journalctl" else real_which(name),
    )
    real_run = systemops.run

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "journalctl":
            return subprocess.CompletedProcess(argv, 0, JOURNAL_LINE, "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(systemops, "run", fake_run)

    # Apache vhosts are files. Point them at this temp tree so a test never
    # reads or writes the machine's /etc/apache2 or /etc/hosts.
    sites_available = tmp_path / "sites-available"
    sites_enabled = tmp_path / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()
    default_conf = (
        "<VirtualHost *:80>\n"
        "\t# default site\n"
        "\tDocumentRoot /var/www/html\n"
        "\tErrorLog ${APACHE_LOG_DIR}/error.log\n"
        "\tCustomLog ${APACHE_LOG_DIR}/access.log combined\n"
        "</VirtualHost>\n"
    )
    (sites_available / "000-default.conf").write_text(default_conf, encoding="utf-8")
    (sites_enabled / "000-default.conf").write_text(default_conf, encoding="utf-8")
    (tmp_path / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    monkeypatch.setattr(vhosts, "SITES_AVAILABLE", sites_available)
    monkeypatch.setattr(vhosts, "SITES_ENABLED", sites_enabled)
    monkeypatch.setattr(vhosts, "HOSTS_FILE", tmp_path / "hosts")
    monkeypatch.setattr(vhosts, "ensure_folder", lambda folder: None)
    monkeypatch.setattr(
        vhosts,
        "enable_site",
        lambda site_id: (sites_enabled / f"{site_id}.conf").write_text(
            "enabled\n", encoding="utf-8"
        ),
    )
    monkeypatch.setattr(vhosts, "reload_apache", lambda: None)
    return fake


@pytest.fixture
def app(paths, host):
    made = create_app(paths=paths)
    made.config["TESTING"] = True
    return made


@pytest.fixture
def client(app):
    """A client that has already unlocked the panel with the real token."""
    with app.test_client() as opened:
        opened.get(f"/?token={app.config['TOKEN']}")
        yield opened


def wait_for_job(client, job_id, tries=100):
    """Poll until the job leaves the running state."""
    import time

    for _ in range(tries):
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] in {"success", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")
