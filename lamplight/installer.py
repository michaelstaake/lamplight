"""The side-effecting half: apt, systemctl, and the pinned Mailpit download.

Every command is streamed line by line into the job log so the UI shows the
same output you would have seen in a terminal.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import catalog, config, firewall, php, systemops
from .jobs import JobStore
from .status import binary_path

# Mailpit is not in the Ubuntu archive. Pin a release and its checksums so an
# install is reproducible and a tampered download is refused.
# Refresh both with scripts/update-mailpit.sh.
MAILPIT_VERSION = "v1.31.1"
MAILPIT_SHA256 = {
    "amd64": "87d2652abd7c17dc99029147ff62ac671afdf7fd76630f7faf5b14125211c709",
    "arm64": "be6a1f9dcf0ac0d7157ee777eac2b5351352b367bcae3c0a0f4854da60546884",
}
MAILPIT_URL = (
    "https://github.com/axllent/mailpit/releases/download/{version}/mailpit-linux-{arch}.tar.gz"
)

BINARY_DIR = Path("/usr/local/bin")
UNIT_DIR = Path("/etc/systemd/system")
DOWNLOAD_TIMEOUT = 180

SERVICE_ACTIONS = ("start", "stop", "restart", "enable", "disable")

MAILPIT_UNIT = """\
[Unit]
Description=Mailpit local mail catcher (managed by Lamplight)
Documentation=https://mailpit.axllent.org/
After=network.target

[Service]
Type=simple
ExecStart={binary} --listen 127.0.0.1:8025 --smtp 127.0.0.1:1025 \\
    --database %S/{state_dir}/mailpit.db --disable-version-check --quiet
Restart=on-failure
RestartSec=3
DynamicUser=yes
StateDirectory={state_dir}
NoNewPrivileges=yes
ProtectHome=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
"""
MAILPIT_STATE_DIR = "lamplight-mailpit"

PHPMYADMIN_PRESEED = """\
phpmyadmin phpmyadmin/reconfigure-webserver multiselect apache2
phpmyadmin phpmyadmin/dbconfig-install boolean true
phpmyadmin phpmyadmin/mysql/method select Unix socket
phpmyadmin phpmyadmin/mysql/admin-user string root
phpmyadmin phpmyadmin/mysql/admin-pass password
phpmyadmin phpmyadmin/mysql/app-pass password
phpmyadmin phpmyadmin/app-password-confirm password
"""


class Installer:
    def __init__(self, paths, store: JobStore) -> None:
        self.paths = paths
        self.store = store

    # -- plumbing ---------------------------------------------------------

    def _log(self, job_id: str, text: str) -> None:
        self.store.append_log(job_id, text if text.endswith("\n") else text + "\n")

    def _run(self, job_id: str, argv: list[str]) -> int:
        self._log(job_id, "$ " + " ".join(argv))
        env = os.environ | {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
        with subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        ) as proc:
            for line in proc.stdout or ():
                self._log(job_id, line.rstrip("\n"))
            return proc.wait()

    def _run_checked(self, job_id: str, argv: list[str]) -> None:
        code = self._run(job_id, argv)
        if code != 0:
            raise RuntimeError(f"{' '.join(argv[:2])} exited {code} — see the log above")

    def _require_root(self) -> None:
        if not systemops.is_root():
            raise PermissionError(
                "Lamplight must run as root to install packages and manage services. "
                "Use the systemd service (see the README)."
            )

    # -- entry points -----------------------------------------------------

    def install(self, job_id: str, component_id: str) -> None:
        component = self._component(component_id)
        self._require_root()
        missing = [req for req in component.requires if not self._installed(req)]
        if missing:
            raise RuntimeError("Install these first: " + ", ".join(missing))
        if component.kind == "apt":
            self._apt_install(job_id, component)
        else:
            self._install_mailpit(job_id, component)
        if component.id == "php":
            # Options chosen before PHP existed have somewhere to go now.
            self._write_php_drop_ins(job_id, php.load_config(self.paths).options)
        self._reload_units(job_id, component)
        self._log(job_id, f"{component.name} installed.")

    def remove(self, job_id: str, component_id: str) -> None:
        component = self._component(component_id)
        self._require_root()
        if component.id == "php":
            self._remove_php_drop_ins(job_id)
        if component.kind == "apt":
            self._apt_remove(job_id, component)
        else:
            self._remove_mailpit(job_id, component)
        self._log(job_id, f"{component.name} removed.")

    def service_action(self, job_id: str, component_id: str, action: str) -> None:
        component = self._component(component_id)
        if action not in SERVICE_ACTIONS:
            raise ValueError(f"Unknown service action: {action}")
        if not component.service:
            raise ValueError(f"{component.name} has no systemd service to {action}.")
        self._require_root()
        argv = ["systemctl", action, component.service]
        if action in {"enable", "disable"}:
            argv.insert(2, "--now")
        self._run_checked(job_id, argv)

    def firewall_action(self, job_id: str, component_id: str, action: str) -> None:
        """Open or close this component's ports in ufw."""
        component = self._component(component_id)
        if action not in firewall.FIREWALL_ACTIONS:
            raise ValueError(f"Unknown firewall action: {action}")
        if not component.firewall_ports:
            raise ValueError(f"Lamplight does not manage firewall ports for {component.name}.")
        if not systemops.which("ufw"):
            raise RuntimeError("ufw is not installed, so there is no firewall to change.")
        self._require_root()
        for port in component.firewall_ports:
            argv = firewall.argv(action, port)
            if action == firewall.FIREWALL_OPEN:
                self._run_checked(job_id, argv)
            elif self._run(job_id, argv) != 0:
                self._log(job_id, f"No {firewall.rule(port)} rule to delete — already closed.")
        self._run(job_id, ["ufw", "status"])

    # -- php --------------------------------------------------------------

    def set_php_extensions(self, job_id: str, desired: tuple[str, ...]) -> None:
        """Make the installed `php-*` packages match the selection.

        The selection is saved first: it is what the user asked for, and it is
        what a retry and the next `Install PHP` will use. The page shows ticked
        and installed separately, so a half-applied change stays visible.
        """
        previous = php.load_config(self.paths)
        php.save_config(self.paths, php.PhpConfig(extensions=desired, options=previous.options))
        self._log(job_id, "Selection saved: " + (", ".join(desired) or "none"))

        if not self._installed("php"):
            self._log(job_id, "PHP is not installed yet — this is what Install PHP will use.")
            return

        candidates = tuple(dict.fromkeys(desired + previous.extensions))
        states = systemops.package_states(php.PACKAGE_PREFIX + name for name in candidates)
        installed = {name: states[php.PACKAGE_PREFIX + name].installed for name in candidates}
        add = [name for name in desired if not installed[name]]
        # Only ever remove what Lamplight put there. A php-* package installed by
        # hand was never ticked here, so unticking it is not something the page
        # can offer, and apt-get remove is not something to guess at.
        drop = [name for name in previous.extensions if name not in desired and installed[name]]

        if not add and not drop:
            self._log(job_id, "Nothing to change — the installed extensions already match.")
            return

        self._require_root()
        if add:
            self._log(job_id, "Adding: " + ", ".join(add))
            self._run_checked(job_id, ["apt-get", "update"])
            self._run_checked(
                job_id,
                [
                    "apt-get",
                    "install",
                    "-y",
                    "--no-install-recommends",
                    *(php.PACKAGE_PREFIX + name for name in add),
                ],
            )
        if drop:
            self._log(job_id, "Removing: " + ", ".join(drop))
            self._run_checked(
                job_id, ["apt-get", "remove", "-y", *(php.PACKAGE_PREFIX + name for name in drop)]
            )
            # php-curl and friends are metapackages; the versioned module they
            # pull in only goes away with autoremove.
            self._run(job_id, ["apt-get", "autoremove", "-y"])
        self._reload_units(job_id, self._component("php"))

    def apply_php_options(self, job_id: str, options: dict[str, str]) -> None:
        """Save the option overrides and write them into every installed SAPI."""
        previous = php.load_config(self.paths)
        php.save_config(self.paths, php.PhpConfig(extensions=previous.extensions, options=options))
        self._log(job_id, f"{len(options)} option(s) saved to {self.paths.php_path}")
        self._write_php_drop_ins(job_id, options)
        self._reload_units(job_id, self._component("php"))

    def _write_php_drop_ins(self, job_id: str, options: dict[str, str]) -> None:
        targets = php.sapis()
        if not targets:
            self._log(
                job_id,
                f"No SAPI directories under {php.PHP_ETC} — nothing to write until PHP is there.",
            )
            return
        if not options:
            self._remove_php_drop_ins(job_id)
            return
        self._require_root()
        body = php.drop_in_text(options)
        for line in body.splitlines():
            if line and not line.startswith(";"):
                self._log(job_id, "  " + line)
        for sapi in targets:
            self._log(job_id, f"Writing {sapi.drop_in}")
            config.atomic_write(sapi.drop_in, body, mode=0o644)

    def _remove_php_drop_ins(self, job_id: str) -> None:
        present = [sapi for sapi in php.sapis() if sapi.drop_in.exists()]
        if not present:
            self._log(job_id, "No Lamplight drop-in in place; php.ini was never overridden.")
            return
        self._require_root()
        for sapi in present:
            self._log(job_id, f"Removing {sapi.drop_in}")
            sapi.drop_in.unlink()

    # -- apt --------------------------------------------------------------

    def _packages(self, component) -> list[str]:
        """What this component installs. PHP's extension list is editable, so it
        comes from php.json rather than from the catalog."""
        if component.id == "php":
            return list(component.packages) + list(php.load_config(self.paths).packages)
        return list(component.all_packages)

    def _apt_install(self, job_id: str, component) -> None:
        self._run_checked(job_id, ["apt-get", "update"])
        if component.id == "phpmyadmin":
            self._preseed_phpmyadmin(job_id)
        packages = self._packages(component)
        self._log(job_id, f"Installing: {', '.join(packages)}")
        self._run_checked(
            job_id, ["apt-get", "install", "-y", "--no-install-recommends", *packages]
        )
        if component.service:
            self._run_checked(job_id, ["systemctl", "enable", "--now", component.service])

    def _apt_remove(self, job_id: str, component) -> None:
        packages = self._packages(component)
        self._log(job_id, f"Removing: {', '.join(packages)}")
        self._log(job_id, "Configuration and data under /var/lib are left in place.")
        self._run_checked(job_id, ["apt-get", "remove", "-y", *packages])
        self._run(job_id, ["apt-get", "autoremove", "-y"])

    def _preseed_phpmyadmin(self, job_id: str) -> None:
        self._log(job_id, "Preseeding phpMyAdmin: web server apache2, database over unix socket")
        result = subprocess.run(
            ["debconf-set-selections"], input=PHPMYADMIN_PRESEED, text=True, capture_output=True
        )
        if result.returncode != 0:
            raise RuntimeError(
                "debconf-set-selections failed: "
                + (result.stderr or result.stdout or "no output").strip()
            )

    def _reload_units(self, job_id: str, component) -> None:
        for unit in component.reload_after_install:
            if systemops.service_exists(unit):
                self._run(job_id, ["systemctl", "reload", unit])

    # -- mailpit ----------------------------------------------------------

    def _install_mailpit(self, job_id: str, component) -> None:
        arch = self._arch()
        dest = BINARY_DIR / component.binary_name
        unit_name = component.service

        # Replacing a running binary in place fails with "Text file busy".
        if systemops.service_exists(unit_name):
            self._run(job_id, ["systemctl", "stop", unit_name])

        archive = self._download_mailpit(job_id, arch)
        try:
            self._extract_binary(job_id, archive, component.binary_name, dest)
        finally:
            archive.unlink(missing_ok=True)

        self._write_unit(
            job_id, unit_name, MAILPIT_UNIT.format(binary=dest, state_dir=MAILPIT_STATE_DIR)
        )
        self._run_checked(job_id, ["systemctl", "daemon-reload"])
        self._run_checked(job_id, ["systemctl", "enable", "--now", unit_name])
        self._log(job_id, "SMTP on 127.0.0.1:1025, inbox on http://127.0.0.1:8025")

    def _remove_mailpit(self, job_id: str, component) -> None:
        unit_name = component.service
        if systemops.service_exists(unit_name):
            self._run(job_id, ["systemctl", "disable", "--now", unit_name])
        unit_path = UNIT_DIR / f"{unit_name}.service"
        if unit_path.exists():
            unit_path.unlink()
            self._log(job_id, f"Removed {unit_path}")
            self._run(job_id, ["systemctl", "daemon-reload"])
        binary = BINARY_DIR / component.binary_name
        if binary.exists():
            binary.unlink()
            self._log(job_id, f"Removed {binary}")
        self._log(
            job_id,
            f"Captured mail is kept in /var/lib/{MAILPIT_STATE_DIR}. "
            "Delete it yourself if you want it gone.",
        )

    def _download_mailpit(self, job_id: str, arch: str) -> Path:
        url = MAILPIT_URL.format(version=MAILPIT_VERSION, arch=arch)
        expected = MAILPIT_SHA256[arch]
        self.paths.download_dir.mkdir(parents=True, exist_ok=True)
        self._log(job_id, f"Downloading {url}")
        handle, tmp_name = tempfile.mkstemp(dir=self.paths.download_dir, suffix=".tar.gz")
        archive = Path(tmp_name)
        digest = hashlib.sha256()
        try:
            with (
                os.fdopen(handle, "wb") as out,
                urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response,
            ):
                while chunk := response.read(1 << 16):
                    digest.update(chunk)
                    out.write(chunk)
        except urllib.error.URLError as exc:
            archive.unlink(missing_ok=True)
            raise RuntimeError(f"Could not download Mailpit: {exc.reason}") from exc
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        actual = digest.hexdigest()
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise RuntimeError(
                f"Checksum mismatch for {url}\n  expected {expected}\n  got      {actual}"
            )
        self._log(job_id, f"SHA256 {actual} — matches the pinned checksum")
        return archive

    def _extract_binary(self, job_id: str, archive: Path, name: str, dest: Path) -> None:
        """Pull exactly one file out of the tarball. Nothing else is written."""
        with tarfile.open(archive, "r:gz") as tar:
            member = next(
                (m for m in tar.getmembers() if m.isfile() and Path(m.name).name == name), None
            )
            if member is None:
                raise RuntimeError(f"No {name} binary inside {archive.name}")
            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError(f"Could not read {member.name} from the archive")
            dest.parent.mkdir(parents=True, exist_ok=True)
            staged = dest.with_name(dest.name + ".new")
            with open(staged, "wb") as out:
                while chunk := source.read(1 << 16):
                    out.write(chunk)
            staged.chmod(0o755)
            staged.replace(dest)
        self._log(job_id, f"Installed {dest}")

    def _write_unit(self, job_id: str, name: str, body: str) -> None:
        path = UNIT_DIR / f"{name}.service"
        self._log(job_id, f"Writing {path}")
        path.write_text(body, encoding="utf-8")
        path.chmod(0o644)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _arch() -> str:
        machine = platform.machine().lower()
        if machine in {"x86_64", "amd64"}:
            return "amd64"
        if machine in {"aarch64", "arm64"}:
            return "arm64"
        raise RuntimeError(f"No pinned Mailpit build for architecture {machine!r}")

    @staticmethod
    def _component(component_id: str):
        component = catalog.get_component(component_id)
        if component is None:
            raise ValueError(f"Unknown component: {component_id}")
        return component

    def _installed(self, component_id: str) -> bool:
        component = catalog.get_component(component_id)
        if component is None:
            return False
        if component.kind == "apt":
            states = systemops.package_states(component.packages)
            return all(states[pkg].installed for pkg in component.packages)
        return binary_path(component) is not None
