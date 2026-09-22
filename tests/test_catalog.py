import pytest

from lamplight import php
from lamplight.catalog import COMPONENT_IDS, COMPONENTS, get_component


def test_catalog_is_mariadb_and_mailpit():
    assert COMPONENT_IDS == ("apache", "php", "mariadb", "phpmyadmin", "mailpit")


def test_no_mysql_server_and_no_mailhog():
    packages = {pkg for c in COMPONENTS for pkg in c.all_packages}
    assert "mysql-server" not in packages
    assert not any("mailhog" in pkg for pkg in packages)
    # php-mysql and mariadb-client are the real Debian names for MariaDB tooling.
    assert {"php-mysql", "mariadb-client", "mariadb-server"} <= packages

    prose = " ".join(f"{c.name} {c.summary}" for c in COMPONENTS).lower()
    assert "mailhog" not in prose
    assert "mysql" not in prose


def test_requirements_reference_real_components():
    for component in COMPONENTS:
        for req in component.requires:
            assert get_component(req) is not None, f"{component.id} requires unknown {req}"


def test_phpmyadmin_needs_the_whole_stack():
    assert set(get_component("phpmyadmin").requires) == {"apache", "php", "mariadb"}


def test_requirements_are_declared_before_use():
    """A component may only require something earlier in the list, so the UI
    unblocks top to bottom."""
    seen = set()
    for component in COMPONENTS:
        assert set(component.requires) <= seen, f"{component.id} requires a later component"
        seen.add(component.id)


@pytest.mark.parametrize("component", COMPONENTS, ids=lambda c: c.id)
def test_every_component_is_installable(component):
    assert component.kind in {"apt", "binary"}
    if component.kind == "apt":
        assert component.packages
    else:
        assert component.binary_name


def test_php_extras_are_only_the_default_selection():
    """The catalog names the starting set; php.json is what actually gets
    installed, so these two must not drift apart."""
    assert get_component("php").extra_packages == php.DEFAULT_PACKAGES
    assert all(pkg.startswith(php.PACKAGE_PREFIX) for pkg in php.DEFAULT_PACKAGES)


def test_unknown_component_is_none():
    assert get_component("mysql") is None
    assert get_component("mailhog") is None


def test_only_the_web_server_offers_to_open_the_firewall():
    """A button that exposes MariaDB or Mailpit to the LAN is not something the
    panel should have. Apache is the only component with a public port."""
    opens = {c.id: c.firewall_ports for c in COMPONENTS if c.firewall_ports}
    assert opens == {"apache": (80, 443)}


def test_firewall_ports_are_ports_the_component_actually_uses():
    for component in COMPONENTS:
        assert set(component.firewall_ports) <= set(component.ports), component.id
