"""Paths, the settings file, and the access token.

Settings, the PHP selection, and the token live in JSON next to each other
rather than in SQLite so that you can read them with `cat` while the service is
running.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

APP_NAME = "lamplight"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3847
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_BYTES = 24
MIN_TOKEN_LENGTH = 16


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def _running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def default_data_dir() -> Path:
    system = Path("/var/lib/lamplight")
    user = Path.home() / ".local/share/lamplight"
    return _env_path("LAMPLIGHT_DATA", system if _running_as_root() else user)


def default_config_dir() -> Path:
    system = Path("/etc/lamplight")
    user = Path.home() / ".config/lamplight"
    return _env_path("LAMPLIGHT_CONFIG", system if _running_as_root() else user)


@dataclass
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    document_root: str = "/var/www/html"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        """Build Settings from untrusted JSON, falling back to defaults per field."""
        defaults = cls()
        host = data.get("host")
        port = data.get("port")
        document_root = data.get("document_root")
        return cls(
            host=host if isinstance(host, str) and host else defaults.host,
            port=port if isinstance(port, int) and 1 <= port <= 65535 else defaults.port,
            document_root=(
                document_root.strip()
                if isinstance(document_root, str) and document_root.strip()
                else defaults.document_root
            ),
        )


class AppPaths:
    def __init__(self, data_dir: Path | None = None, config_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.config_dir = Path(config_dir) if config_dir else default_config_dir()
        self.db_path = self.data_dir / "lamplight.sqlite3"
        self.settings_path = self.config_dir / "settings.json"
        self.php_path = self.config_dir / "php.json"
        self.auth_path = self.config_dir / "auth.json"
        self.download_dir = self.data_dir / "downloads"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)


def load_settings(paths: AppPaths) -> Settings:
    try:
        data = json.loads(paths.settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        return Settings.from_dict(data)
    settings = Settings()
    save_settings(paths, settings)
    return settings


def save_settings(paths: AppPaths, settings: Settings) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.settings_path, settings.to_json() + "\n", mode=0o644)


def load_or_create_token(paths: AppPaths) -> str:
    try:
        data = json.loads(paths.auth_path.read_text(encoding="utf-8"))
        token = data.get("token") if isinstance(data, dict) else None
        if isinstance(token, str) and len(token) >= MIN_TOKEN_LENGTH:
            return token
    except (OSError, json.JSONDecodeError):
        pass
    token = secrets.token_urlsafe(TOKEN_BYTES)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.auth_path, json.dumps({"token": token}, indent=2) + "\n", mode=0o600)
    return token


def atomic_write(path: Path, text: str, *, mode: int) -> None:
    """Write atomically, creating the file with `mode` before any content lands."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, mode)
    tmp.replace(path)
