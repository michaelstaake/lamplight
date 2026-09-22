"""The fixed set of components Lamplight knows how to install.

One entry per component. Adding software to Lamplight should mean adding a
Component here and, if it is not a plain apt package, a branch in installer.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .php import DEFAULT_PACKAGES


@dataclass(frozen=True)
class Link:
    label: str
    url: str
    hint: str = ""


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    summary: str
    kind: str  # "apt" or "binary"
    packages: tuple[str, ...] = ()
    extra_packages: tuple[str, ...] = ()
    service: str | None = None
    ports: tuple[int, ...] = ()
    # TCP ports the panel will open and close in ufw. Deliberately empty for
    # everything but the web server: a database or a mail catcher reachable
    # from the LAN is not something a button should offer.
    firewall_ports: tuple[int, ...] = ()
    requires: tuple[str, ...] = ()
    links: tuple[Link, ...] = ()
    binary_name: str | None = None
    reload_after_install: tuple[str, ...] = field(default=())

    @property
    def all_packages(self) -> tuple[str, ...]:
        return self.packages + self.extra_packages


COMPONENTS: tuple[Component, ...] = (
    Component(
        id="apache",
        name="Apache",
        summary="The web server. Serves /var/www/html on port 80 once installed.",
        kind="apt",
        packages=("apache2",),
        service="apache2",
        ports=(80, 443),
        firewall_ports=(80, 443),
        links=(Link("Default site", "http://127.0.0.1/", "Document root /var/www/html"),),
    ),
    Component(
        id="php",
        name="PHP",
        summary="The PHP runtime, the Apache module, and a starting set of extensions.",
        kind="apt",
        packages=("php", "libapache2-mod-php", "php-cli"),
        # The default selection only. What actually gets installed is whatever is
        # ticked on the PHP page — see php.py and AppPaths.php_path.
        extra_packages=DEFAULT_PACKAGES,
        requires=("apache",),
        reload_after_install=("apache2",),
    ),
    Component(
        id="mariadb",
        name="MariaDB",
        summary="The SQL database. Ubuntu 26.04 ships MariaDB 11.8 in the main archive.",
        kind="apt",
        packages=("mariadb-server",),
        extra_packages=("mariadb-client",),
        service="mariadb",
        ports=(3306,),
    ),
    Component(
        id="phpmyadmin",
        name="phpMyAdmin",
        summary="Browser UI for MariaDB. Distro package, wired into Apache during install.",
        kind="apt",
        packages=("phpmyadmin",),
        requires=("apache", "php", "mariadb"),
        links=(Link("phpMyAdmin", "http://127.0.0.1/phpmyadmin/", "Log in as your Unix user"),),
        reload_after_install=("apache2",),
    ),
    Component(
        id="mailpit",
        name="Mailpit",
        summary="Local SMTP catcher with a web inbox. Nothing you send can leave the machine.",
        kind="binary",
        binary_name="mailpit",
        service="lamplight-mailpit",
        ports=(1025, 8025),
        links=(Link("Inbox", "http://127.0.0.1:8025", "Everything your apps 'sent'"),),
    ),
)

COMPONENT_IDS: tuple[str, ...] = tuple(c.id for c in COMPONENTS)

_BY_ID = {c.id: c for c in COMPONENTS}


def get_component(component_id: str) -> Component | None:
    return _BY_ID.get(component_id)
