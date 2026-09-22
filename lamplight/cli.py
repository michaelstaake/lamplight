from __future__ import annotations

import argparse
import os
import sys

from . import __version__, config, systemops
from .app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lamplight",
        description="Localhost web UI for installing a LAMP stack on Debian and Ubuntu.",
    )
    parser.add_argument("--version", action="version", version=f"lamplight {__version__}")
    parser.add_argument("--host", help=f"bind address (default {config.DEFAULT_HOST})")
    parser.add_argument("--port", type=int, help=f"bind port (default {config.DEFAULT_PORT})")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode")
    return parser


def _env_port() -> int | None:
    raw = os.environ.get("LAMPLIGHT_PORT")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = config.AppPaths()
    paths.ensure()
    settings = config.load_settings(paths)
    token = config.load_or_create_token(paths)

    host = args.host or os.environ.get("LAMPLIGHT_HOST") or settings.host
    port = args.port or _env_port() or settings.port

    print(f"Lamplight {__version__}")
    print(f"Open this URL on this machine:\n  http://{host}:{port}/?token={token}")
    print(f"Token file: {paths.auth_path}")
    if not systemops.is_root():
        print(
            "Not running as root: status is read-only and installs will fail. "
            "Use the systemd service for a working panel.",
            file=sys.stderr,
        )

    if host not in config.LOOPBACK_HOSTS:
        print(
            f"Warning: binding to {host} exposes a panel that runs apt and systemctl as root. "
            "Bind to 127.0.0.1 unless something else is authenticating for you.",
            file=sys.stderr,
        )

    create_app(paths=paths).run(host=host, port=port, debug=args.debug, threaded=True)
    return 0


def print_token(argv: list[str] | None = None) -> int:
    """Entry point for the `lamplight-token` command."""
    argparse.ArgumentParser(
        prog="lamplight-token", description="Print the Lamplight access token."
    ).parse_args(argv)
    print(config.load_or_create_token(config.AppPaths()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
