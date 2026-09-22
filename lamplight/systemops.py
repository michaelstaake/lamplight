"""Read-only queries about the host: dpkg, systemd, /etc/os-release.

Everything here is batched. One dashboard render asks about eight packages and
three units, and doing that one subprocess at a time was the slowest thing in
the app.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 20


@dataclass(frozen=True)
class PackageState:
    installed: bool = False
    version: str | None = None


@dataclass(frozen=True)
class FirewallState:
    """What ufw reports, or all-false when it cannot be asked.

    `ufw status` refuses to answer to a non-root user, and a panel that cannot
    read the firewall must not offer to change it — hence `readable`.
    """

    installed: bool = False
    readable: bool = False
    active: bool = False
    allowed_ports: frozenset[int] = frozenset()


@dataclass(frozen=True)
class ServiceState:
    loaded: bool = False
    active: bool = False
    enabled: bool = False
    # Resident memory of the unit's whole cgroup, or None when systemd is not
    # accounting for it. Every process the unit forked counts, which is why
    # Apache's number already includes mod_php.
    memory: int | None = None


def run(
    argv: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell.

    `input` is the process's stdin. Callers that have a secret put it here
    rather than in `argv`, so it does not show up in the process list.
    """
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False, input=input
    )


def which(name: str) -> str | None:
    return shutil.which(name)


def package_states(names: Iterable[str]) -> dict[str, PackageState]:
    """Ask dpkg about every package in one call."""
    wanted = list(dict.fromkeys(names))
    states = {name: PackageState() for name in wanted}
    if not wanted or not which("dpkg-query"):
        return states
    try:
        result = run(["dpkg-query", "-W", "-f=${Package}\t${Status}\t${Version}\n", *wanted])
    except (OSError, subprocess.TimeoutExpired):
        return states
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, status, version = parts
        if name not in states:
            continue
        installed = status.strip() == "install ok installed"
        states[name] = PackageState(
            installed=installed, version=version.strip() or None if installed else None
        )
    return states


def service_states(names: Iterable[str]) -> dict[str, ServiceState]:
    """Ask systemd about every unit in one call."""
    wanted = [name for name in dict.fromkeys(names) if name]
    states = {name: ServiceState() for name in wanted}
    if not wanted or not which("systemctl"):
        return states
    units = [name if name.endswith(".service") else f"{name}.service" for name in wanted]
    try:
        result = run(
            [
                "systemctl",
                "show",
                *units,
                "--property=Id,LoadState,ActiveState,UnitFileState,MemoryCurrent",
            ]
        )
    except (OSError, subprocess.TimeoutExpired):
        return states
    for block in (result.stdout or "").split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.strip().splitlines() if "=" in line)
        unit_id = fields.get("Id", "")
        name = unit_id.removesuffix(".service")
        if name not in states:
            continue
        states[name] = ServiceState(
            loaded=fields.get("LoadState") == "loaded",
            active=fields.get("ActiveState") == "active",
            enabled=fields.get("UnitFileState", "").startswith("enabled"),
            memory=_memory_value(fields.get("MemoryCurrent")),
        )
    return states


def _memory_value(raw: str | None) -> int | None:
    """MemoryCurrent, or None when systemd has no figure to give.

    A unit with no cgroup accounting answers `[not set]` on some versions and
    2**64-1 on others. Neither is a byte count, so neither belongs in a total.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value < 2**63 else None


def process_memory() -> int | None:
    """This process's resident memory, for when Lamplight runs outside systemd."""
    try:
        text = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def service_exists(name: str) -> bool:
    return service_states([name])[name].loaded


def apt_packages_exist(names: Iterable[str]) -> dict[str, bool | None]:
    """Does apt know these package names? One apt-cache call for the lot.

    None means apt could not answer — no apt-cache, or it failed — which callers
    should treat as "let the install try", not as "no such package".
    """
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return {}
    if not which("apt-cache"):
        return dict.fromkeys(wanted, None)
    try:
        result = run(["apt-cache", "policy", *wanted])
    except (OSError, subprocess.TimeoutExpired):
        return dict.fromkeys(wanted, None)
    # Every package apt knows gets a block headed by "<name>:" at column zero.
    seen = {
        line[:-1]
        for line in (result.stdout or "").splitlines()
        if line.endswith(":") and line[:1].strip()
    }
    return {name: name in seen for name in wanted}


def firewall_state() -> FirewallState:
    if not which("ufw"):
        return FirewallState()
    try:
        result = run(["ufw", "status"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return FirewallState(installed=True)
    if result.returncode != 0:
        return FirewallState(installed=True)
    text = result.stdout or ""
    return FirewallState(
        installed=True,
        readable=True,
        active="Status: active" in text,
        allowed_ports=frozenset(_allowed_ports(text)),
    )


def _allowed_ports(text: str) -> set[int]:
    """TCP ports with an ALLOW rule in `ufw status` output.

    Rules read `80/tcp  ALLOW  Anywhere`, with a `(v6)` twin and sometimes a
    comma-joined port list. Application profiles ("Apache Full") name no port,
    so they are not counted — Lamplight adds and removes plain port rules.
    """
    ports: set[int] = set()
    for line in text.splitlines():
        if "ALLOW" not in line:
            continue
        target = line.split("ALLOW", 1)[0].replace("(v6)", "").strip()
        spec, _, proto = target.partition("/")
        if proto not in {"", "tcp"}:
            continue
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                ports.add(int(chunk))
    return ports


def php_modules() -> list[str]:
    """What `php -m` reports as loaded, or an empty list if PHP is not installed."""
    if not which("php"):
        return []
    try:
        result = run(["php", "-m"], timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return []
    modules = [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip() and not line.startswith("[")
    ]
    return sorted(dict.fromkeys(modules), key=str.lower)


def command_version(argv: list[str]) -> str | None:
    """First line of `cmd --version`-style output, or None if the command is missing."""
    if not which(argv[0]) and not Path(argv[0]).exists():
        return None
    try:
        result = run(argv, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    return text.splitlines()[0].strip() if text else None


def read_os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return data
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def _os_ids() -> set[str]:
    info = read_os_release()
    return {info.get("ID", ""), *info.get("ID_LIKE", "").split()}


def is_ubuntu_family() -> bool:
    """Ubuntu and derivatives that can use a Launchpad PPA. Zorin is one."""
    return "ubuntu" in _os_ids()


def is_debian_family() -> bool:
    return bool(_os_ids() & {"debian", "ubuntu"})


_CODENAME_RE = re.compile(r"^[a-z]+$")


def debian_codename() -> str:
    """VERSION_CODENAME from os-release, or raise if it is not a plain word.

    Used only to build the Debian Surý source line. A codename with anything
    other than letters would be rejected rather than written into sources.list.
    """
    name = read_os_release().get("VERSION_CODENAME", "")
    if not _CODENAME_RE.fullmatch(name):
        raise RuntimeError("Could not read a Debian codename from /etc/os-release.")
    return name


def is_supported() -> bool:
    return platform.system() == "Linux" and is_debian_family()


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


@dataclass(frozen=True)
class HostInfo:
    pretty_name: str
    id: str
    version_id: str
    supported: bool
    python: str
    privileged: bool
    hostname: str


def host_info() -> HostInfo:
    info = read_os_release()
    return HostInfo(
        pretty_name=info.get("PRETTY_NAME") or platform.platform(),
        id=info.get("ID", "unknown"),
        version_id=info.get("VERSION_ID", ""),
        supported=is_supported(),
        python=platform.python_version(),
        privileged=is_root(),
        hostname=platform.node(),
    )
