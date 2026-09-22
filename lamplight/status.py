"""What is installed right now, as a JSON-shaped payload for the UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import php, systemops
from .catalog import COMPONENTS, Component

AVAILABLE = "available"
BLOCKED = "blocked"
INSTALLED = "installed"
RUNNING = "running"
STOPPED = "stopped"

STATUS_TEXT = {
    AVAILABLE: "not installed",
    BLOCKED: "needs others",
    INSTALLED: "installed",
    RUNNING: "running",
    STOPPED: "stopped",
}

BINARY_DIR = Path("/usr/local/bin")
# The panel's own unit, so the dashboard can count Lamplight alongside the
# stack it installed.
LAMPLIGHT_UNIT = "lamplight"


def binary_path(component: Component) -> Path | None:
    """Where an installed binary component lives, or None if it is not installed."""
    if not component.binary_name:
        return None
    found = systemops.which(component.binary_name)
    if found:
        return Path(found)
    candidate = BINARY_DIR / component.binary_name
    return candidate if candidate.exists() else None


@dataclass(frozen=True)
class HostProbe:
    """One sweep of the host: per-component state, plus Lamplight's own memory."""

    components: dict[str, dict]
    own_memory: int | None = None


def _probe(php_config: php.PhpConfig | None = None) -> HostProbe:
    """One dpkg call and one systemctl call for the whole catalog.

    A pinned PHP version is installed as `php8.5` rather than the `php`
    metapackage, so the probe has to ask about those names or the panel
    reports PHP as missing and blocks phpMyAdmin.
    """
    php_runtime = php_config.runtime_packages if php_config is not None else php.DISTRO_RUNTIME
    wanted = {
        component.id: php_runtime if component.id == "php" else component.packages
        for component in COMPONENTS
        if component.kind == "apt"
    }
    packages = systemops.package_states(pkg for names in wanted.values() for pkg in names)
    # Lamplight's own unit rides along in the same call the catalog needs.
    services = systemops.service_states(
        [LAMPLIGHT_UNIT, *(component.service for component in COMPONENTS if component.service)]
    )
    # One `ufw status` for the catalog, and only when something declares ports.
    firewall = (
        systemops.firewall_state()
        if any(component.firewall_ports for component in COMPONENTS)
        else systemops.FirewallState()
    )

    state: dict[str, dict] = {}
    for component in COMPONENTS:
        if component.kind == "apt":
            names = wanted[component.id]
            installed = all(packages[pkg].installed for pkg in names)
            version = next((packages[pkg].version for pkg in names if packages[pkg].version), None)
            if installed and component.id == "php":
                version = systemops.command_version(["php", "-v"]) or version
        else:
            path = binary_path(component)
            installed = path is not None
            version = systemops.command_version([str(path), "version"]) if path else None
        service = services.get(component.service) if component.service else None
        state[component.id] = {
            "installed": installed,
            "version": version,
            "active": bool(service and service.active),
            "enabled": bool(service and service.enabled),
            "memory": service.memory if service and service.active else None,
            "firewall_active": firewall.readable and firewall.active,
            "firewall_allowed": bool(component.firewall_ports)
            and all(port in firewall.allowed_ports for port in component.firewall_ports),
        }
    own = services[LAMPLIGHT_UNIT].memory
    return HostProbe(components=state, own_memory=own if own is not None else _self_memory())


def _self_memory() -> int | None:
    """Lamplight's footprint when it is not running under its own unit."""
    return systemops.process_memory()


def _firewall_view(component: Component, state: dict) -> dict | None:
    """The firewall control for one component, or None when there is nothing to show.

    Absent means no button: either this component has no ports Lamplight will
    touch, or ufw is not running (or could not be read, which is what a
    non-root panel sees).
    """
    if not component.firewall_ports or not state.get("firewall_active"):
        return None
    ports = list(component.firewall_ports)
    return {
        "ports": ports,
        "ports_text": "/".join(str(port) for port in ports),
        "allowed": bool(state.get("firewall_allowed")),
    }


def _status_label(
    component: Component, installed: bool, active: bool, blocked_by: list[str]
) -> str:
    if not installed:
        return BLOCKED if blocked_by else AVAILABLE
    if not component.service:
        return INSTALLED
    return RUNNING if active else STOPPED


def _snapshot(component: Component, state: dict, installed_ids: set[str]) -> dict:
    blocked_by = [req for req in component.requires if req not in installed_ids]
    label = _status_label(component, state["installed"], state["active"], blocked_by)
    return {
        "id": component.id,
        "name": component.name,
        "summary": component.summary,
        "kind": component.kind,
        "packages": list(component.all_packages),
        "service": component.service,
        "ports": list(component.ports),
        "requires": list(component.requires),
        "links": [
            {"label": link.label, "url": link.url, "hint": link.hint} for link in component.links
        ],
        "firewall": _firewall_view(component, state),
        "installed": state["installed"],
        "active": state["active"],
        "enabled": state["enabled"],
        "memory": state.get("memory"),
        "version": state["version"],
        "blocked_by": blocked_by,
        "status": label,
        "status_text": STATUS_TEXT[label],
    }


def format_bytes(value: int) -> str:
    """Bytes as something readable at a glance: 940 KB, 412 MB, 1.4 GB."""
    size = float(max(value, 0))
    unit = "B"
    for candidate in ("KB", "MB", "GB", "TB"):
        if size < 1024:
            break
        size /= 1024
        unit = candidate
    if unit == "B":
        return f"{int(size)} B"
    return f"{size:.1f} {unit}" if size < 10 else f"{size:.0f} {unit}"


def memory_view(items: list[dict], own_memory: int | None) -> dict:
    """Resident memory of Lamplight and everything it runs, as one figure.

    Each number is a unit's whole cgroup, so Apache's already covers mod_php and
    phpMyAdmin — neither has a process of its own to measure. A component with
    no systemd unit, or a host where systemd is not accounting for memory,
    contributes nothing rather than a zero that looks like a measurement.
    """
    parts = []
    if own_memory is not None:
        parts.append({"name": "Lamplight", "bytes": own_memory})
    parts += [
        {"name": item["name"], "bytes": item["memory"]}
        for item in items
        if item["memory"] is not None
    ]
    total = sum(part["bytes"] for part in parts)
    return {
        "total": total,
        "text": format_bytes(total) if parts else "—",
        "parts": [part | {"text": format_bytes(part["bytes"])} for part in parts],
    }


def php_view(php_config: php.PhpConfig) -> dict:
    """Everything the PHP page shows beyond the generic component panel.

    The extension list is the selection itself — one tag per package, nothing
    else. Dropping a tag is how you uninstall one.
    """
    extensions = [
        {"name": name, "package": php.extension_package(name, php_config.version)}
        for name in sorted(php_config.extensions)
    ]

    sapis = php.sapis()
    in_effect = php.effective_options(sapis[0]) if sapis else {}
    options = []
    for option in php.OPTIONS:
        saved = php_config.options.get(option.name)
        current = in_effect.get(option.name, (None, None))[0]
        options.append(
            {
                "name": option.name,
                "label": option.label,
                "kind": option.kind,
                "choices": list(option.choices),
                "php_default": option.php_default,
                "value": saved,
                "current": current or saved or option.php_default,
            }
        )

    return {
        "version": php_config.version,
        "versions": list(php.SELECTABLE_VERSIONS),
        "extensions": extensions,
        "options": options,
        "managed_count": len(php_config.options),
        "modules": systemops.php_modules(),
        "sapis": [
            {
                "version": sapi.version,
                "name": sapi.name,
                "drop_in": str(sapi.drop_in),
                "managed": sapi.drop_in.exists(),
            }
            for sapi in sapis
        ],
    }


def dashboard(settings, php_config: php.PhpConfig | None = None) -> dict:
    probe = _probe(php_config)
    state = probe.components
    installed_ids = {cid for cid, item in state.items() if item["installed"]}
    items = [_snapshot(component, state[component.id], installed_ids) for component in COMPONENTS]
    if php_config is not None:
        # What PHP installs is a selection, not a fixed list — see php.py.
        base = php_config.runtime_packages
        for item in items:
            if item["id"] == "php":
                item["packages"] = list(base) + list(php_config.packages)
    host = systemops.host_info()
    return {
        "host": {
            "pretty_name": host.pretty_name,
            "id": host.id,
            "version_id": host.version_id,
            "supported": host.supported,
            "python": host.python,
            "privileged": host.privileged,
            "hostname": host.hostname,
        },
        "components": items,
        "counts": {
            "total": len(items),
            "installed": sum(1 for item in items if item["installed"]),
            "running": sum(1 for item in items if item["active"]),
        },
        "memory": memory_view(items, probe.own_memory),
        "document_root": settings.document_root,
    }
