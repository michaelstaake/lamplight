"""Create, retarget, and delete Apache vhosts.

Debian keeps each site in `/etc/apache2/sites-available` and enables it with a
symlink in `sites-enabled`. The default site is `000-default`. This page lists
the enabled sites, and it will not delete that one.

A name or a folder is written into a config file and into `/etc/hosts`, so both
are checked before they get anywhere near a file. A folder is one absolute
path with no spaces, which means it cannot become a second directive.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from pathlib import Path

from . import systemops

SITES_AVAILABLE = Path("/etc/apache2/sites-available")
SITES_ENABLED = Path("/etc/apache2/sites-enabled")
HOSTS_FILE = Path("/etc/hosts")

DEFAULT_ID = "000-default"
DEFAULT_NAME = "default"
# Only lines carrying this marker are removed. A line the operator wrote stays.
HOSTS_MARKER = "# lamplight"

ROOT_REQUIRED = (
    "Lamplight must run as root to install packages and manage services. "
    "Use the systemd service (see the README)."
)

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_NAME_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
_SITE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# One absolute path. Spaces and quotes are out, so the value cannot break out
# of the DocumentRoot line it is written into.
_FOLDER_RE = re.compile(r"^/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
_DOCROOT_RE = re.compile(r"^([ \t]*DocumentRoot[ \t]+)(\S+)", re.MULTILINE)
_SERVER_RE = re.compile(r"^[ \t]*ServerName[ \t]+(\S+)", re.MULTILINE)


class ApacheError(RuntimeError):
    """Apache, or the files in front of it, refused the operation."""


def clean_name(raw: object) -> str:
    """A ServerName this page is willing to create."""
    if not isinstance(raw, str):
        raise ValueError("name must be text")
    name = raw.strip().lower().rstrip(".")
    if name in {DEFAULT_NAME, DEFAULT_ID}:
        raise ValueError("the default site already exists")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("name must be a hostname, like app.test")
    return name


def clean_folder(raw: object) -> str:
    """An absolute document root that can sit in one Apache directive."""
    if not isinstance(raw, str):
        raise ValueError("folder must be text")
    folder = raw.strip().rstrip("/")
    if not _FOLDER_RE.fullmatch(folder) or any(part in {".", ".."} for part in folder.split("/")):
        raise ValueError("folder must be an absolute path without spaces")
    return folder


def clean_site_id(raw: object) -> str:
    """A sites-available file name, without the `.conf`."""
    if not isinstance(raw, str):
        raise ValueError("vhost must be text")
    site_id = raw.strip()
    if ".." in site_id or not _SITE_ID_RE.fullmatch(site_id):
        raise ValueError("invalid vhost")
    return site_id


def parse_site(site_id: str, text: str) -> dict:
    """The name and folder one site file contributes to the list."""
    ident = clean_site_id(site_id)
    document = _DOCROOT_RE.search(text)
    server = _SERVER_RE.search(text)
    folder = _unquote(document.group(2)) if document else ""
    default = ident == DEFAULT_ID
    if default:
        name = DEFAULT_NAME
    elif server:
        name = _unquote(server.group(1))
    else:
        name = ident
    return {"id": ident, "name": name, "folder": folder, "default": default}


def replace_document_root(text: str, folder: str) -> str:
    """Swap the first DocumentRoot path, leaving the rest of the file as it was."""
    root = clean_folder(folder)
    match = _DOCROOT_RE.search(text)
    if match is None:
        raise ApacheError("This vhost has no DocumentRoot")
    start, end = match.span(2)
    return text[:start] + root + text[end:]


def render_vhost(name: str, folder: str) -> str:
    """A port-80 site file. The name is also the log file name, and it is already clean."""
    site = clean_name(name)
    root = clean_folder(folder)
    return (
        "<VirtualHost *:80>\n"
        f"\tServerName {site}\n"
        f"\tDocumentRoot {root}\n"
        "\n"
        f"\tErrorLog ${{APACHE_LOG_DIR}}/{site}-error.log\n"
        f"\tCustomLog ${{APACHE_LOG_DIR}}/{site}-access.log combined\n"
        "</VirtualHost>\n"
    )


def hosts_with(text: str, name: str) -> str:
    """`text` plus a loopback line for `name`, unless some line already maps it."""
    site = clean_name(name)
    for existing in text.splitlines():
        parts = existing.split()
        if len(parts) >= 2 and parts[0] == "127.0.0.1" and parts[1] == site:
            return _terminated(text)
    return _terminated(text) + f"127.0.0.1 {site} {HOSTS_MARKER}\n"


def hosts_without(text: str, name: str) -> str:
    """Drop the loopback line this page added for `name`. Other lines stay."""
    if not isinstance(name, str) or not name or any(char.isspace() for char in name):
        return text
    kept = []
    for existing in text.splitlines():
        parts = existing.split()
        if parts[:2] == ["127.0.0.1", name] and existing.strip().endswith(HOSTS_MARKER):
            continue
        kept.append(existing)
    if not kept:
        return ""
    return "\n".join(kept) + "\n"


def list_vhosts() -> list[dict]:
    """Enabled sites, default first."""
    if not SITES_ENABLED.is_dir():
        raise ApacheError("Apache sites are not available")
    found: list[dict] = []
    for entry in SITES_ENABLED.iterdir():
        if not entry.name.endswith(".conf"):
            continue
        try:
            site_id = clean_site_id(entry.name[: -len(".conf")])
            _path, text = _read_config(site_id)
        except (ApacheError, ValueError, OSError):
            continue
        found.append(parse_site(site_id, text))
    return sorted(found, key=lambda item: (not item["default"], item["name"].lower()))


def create_vhost(name: object, folder: object) -> None:
    """Write a site, enable it, and point the name at loopback."""
    site = clean_name(name)
    root = clean_folder(folder)
    _require_root()
    _ensure_sites()
    if _name_taken(site):
        raise ValueError(f"{site} already exists")
    available = _available_path(site)
    previous_hosts = _read_hosts()
    _write(available, render_vhost(site, root))
    enabled = False
    hosts_written = False
    try:
        enable_site(site)
        enabled = True
        ensure_folder(root)
        _write(HOSTS_FILE, hosts_with(previous_hosts, site))
        hosts_written = True
        reload_apache()
    except Exception:
        if hosts_written:
            _write(HOSTS_FILE, previous_hosts)
        if enabled:
            _remove(_enabled_path(site))
        _remove(available)
        raise


def set_folder(site_id: object, folder: object) -> None:
    """Point one existing vhost at a different folder."""
    ident = clean_site_id(site_id)
    root = clean_folder(folder)
    _require_root()
    if ident not in {item["id"] for item in list_vhosts()}:
        raise ValueError("no such vhost")
    path, original = _read_config(ident)
    updated = replace_document_root(original, root)
    if updated != original:
        _write(path, updated)
    try:
        ensure_folder(root)
        if updated != original:
            reload_apache()
    except Exception:
        if updated != original:
            _write(path, original)
        raise


def delete_vhost(site_id: object) -> None:
    """Remove a site's config and the hosts line added for it. The folder stays."""
    ident = clean_site_id(site_id)
    _require_root()
    if ident == DEFAULT_ID:
        raise ValueError("the default site cannot be deleted")
    listed = {item["id"]: item for item in list_vhosts()}
    item = listed.get(ident)
    if item is None:
        raise ValueError("no such vhost")

    available = _available_path(ident)
    enabled = _enabled_path(ident)
    available_text = _snapshot(available, SITES_AVAILABLE)
    enabled_text, enabled_link = _snapshot_enabled(enabled)
    previous_hosts = _read_hosts()
    revised_hosts = hosts_without(previous_hosts, item["name"])

    _remove(enabled)
    if available_text is not None:
        _remove(available)
    if revised_hosts != previous_hosts:
        _write(HOSTS_FILE, revised_hosts)
    try:
        reload_apache()
    except Exception:
        if available_text is not None:
            _write(available, available_text)
        _restore_enabled(enabled, enabled_text, enabled_link)
        if revised_hosts != previous_hosts:
            _write(HOSTS_FILE, previous_hosts)
        raise


def enable_site(site_id: str) -> None:
    """The same relative symlink `a2ensite` makes."""
    ident = clean_site_id(site_id)
    link = _enabled_path(ident)
    if os.path.lexists(link):
        return
    target = Path("..") / "sites-available" / f"{ident}.conf"
    try:
        os.symlink(target, link)
    except OSError:
        raise ApacheError("Could not enable the vhost") from None


def ensure_folder(folder: str) -> None:
    """Create the document root if it is not there yet. Never deletes it."""
    path = Path(folder)
    if path.is_file():
        raise ApacheError(f"{folder} is a file")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ApacheError(f"Could not create {folder}") from None


def reload_apache() -> None:
    """Ask Apache to reread its config when it is running. A bad config is an error."""
    if not systemops.is_root():
        raise PermissionError(ROOT_REQUIRED)
    binary = systemops.which("apache2ctl")
    if not binary:
        raise ApacheError("apache2ctl is not installed")
    try:
        checked = systemops.run([binary, "configtest"])
    except (OSError, subprocess.TimeoutExpired):
        raise ApacheError("Could not check the Apache configuration") from None
    if checked.returncode != 0:
        raise ApacheError(_last_line(checked, "Apache rejected the configuration"))
    if not systemops.which("systemctl"):
        raise ApacheError("systemctl is not installed")
    try:
        active = systemops.run(["systemctl", "is-active", "--quiet", "apache2"])
    except (OSError, subprocess.TimeoutExpired):
        raise ApacheError("Could not tell whether Apache is running") from None
    if active.returncode != 0:
        return
    try:
        reloaded = systemops.run(["systemctl", "reload", "apache2"])
    except (OSError, subprocess.TimeoutExpired):
        raise ApacheError("Could not reload Apache") from None
    if reloaded.returncode != 0:
        raise ApacheError(_last_line(reloaded, "Apache did not reload"))


def _require_root() -> None:
    if not systemops.is_root():
        raise PermissionError(ROOT_REQUIRED)


def _ensure_sites() -> None:
    if not SITES_AVAILABLE.is_dir() or not SITES_ENABLED.is_dir():
        raise ApacheError("Apache sites are not available")


def _name_taken(name: str) -> bool:
    if name in {DEFAULT_ID, DEFAULT_NAME}:
        return True
    for item in list_vhosts():
        if item["id"] == name or item["name"] == name:
            return True
    return os.path.lexists(_available_path(name))


def _available_path(site_id: str) -> Path:
    return SITES_AVAILABLE / f"{clean_site_id(site_id)}.conf"


def _enabled_path(site_id: str) -> Path:
    return SITES_ENABLED / f"{clean_site_id(site_id)}.conf"


def _contained(path: Path, root: Path) -> bool:
    """A regular file inside `root`. A symlink only counts when its target is too."""
    try:
        if path.is_symlink():
            resolved = path.resolve()
            return resolved.is_file() and resolved.is_relative_to(root.resolve())
        return path.is_file() and path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _read_config(site_id: str) -> tuple[Path, str]:
    available = _available_path(site_id)
    enabled = _enabled_path(site_id)
    if _contained(available, SITES_AVAILABLE):
        return available, available.read_text(encoding="utf-8")
    if enabled.is_symlink():
        try:
            resolved = enabled.resolve()
        except OSError:
            resolved = None
        if (
            resolved is not None
            and resolved.is_file()
            and resolved.is_relative_to(SITES_AVAILABLE.resolve())
        ):
            return resolved, resolved.read_text(encoding="utf-8")
    if _contained(enabled, SITES_ENABLED):
        return enabled, enabled.read_text(encoding="utf-8")
    raise ApacheError(f"Could not read {site_id}")


def _snapshot(path: Path, root: Path) -> str | None:
    if not _contained(path, root):
        return None
    return path.read_text(encoding="utf-8")


def _snapshot_enabled(path: Path) -> tuple[str | None, str | None]:
    if path.is_symlink():
        try:
            return None, os.readlink(path)
        except OSError:
            return None, None
    if _contained(path, SITES_ENABLED):
        return path.read_text(encoding="utf-8"), None
    return None, None


def _restore_enabled(path: Path, text: str | None, link: str | None) -> None:
    if link is not None:
        try:
            os.symlink(link, path)
        except OSError:
            raise ApacheError("Could not restore the vhost") from None
        return
    if text is not None:
        _write(path, text)


def _read_hosts() -> str:
    try:
        return HOSTS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        raise ApacheError("Could not read /etc/hosts") from None


def _write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.lamplight-tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o644)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise ApacheError(f"Could not write {path.name}") from None


def _remove(path: Path) -> None:
    if os.path.lexists(path):
        path.unlink()


def _terminated(text: str) -> str:
    if not text or text.endswith("\n"):
        return text
    return text + "\n"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _last_line(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    text = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
    if not text:
        return fallback
    return text.splitlines()[-1].strip() or fallback
