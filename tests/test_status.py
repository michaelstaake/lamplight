import pytest

from lamplight import status, systemops
from lamplight.catalog import COMPONENTS, get_component
from lamplight.config import Settings
from lamplight.status import dashboard, format_bytes, memory_view

MB = 1024 * 1024


def blank_state():
    return {
        component.id: {
            "installed": False,
            "active": False,
            "enabled": False,
            "version": None,
            "memory": None,
            "firewall_active": True,
            "firewall_allowed": False,
        }
        for component in COMPONENTS
    }


def install(state, component_id):
    has_service = bool(get_component(component_id).service)
    state[component_id].update(
        installed=True, active=has_service, enabled=has_service, version="1.0"
    )
    return state


@pytest.fixture
def probe(monkeypatch):
    """A host whose state the test writes, without touching dpkg or systemd."""
    state = blank_state()
    monkeypatch.setattr(
        status, "_probe", lambda: status.HostProbe(components=state, own_memory=None)
    )
    return state


def view():
    """The dashboard's components, keyed by id. Needs the `probe` fixture."""
    return {item["id"]: item for item in dashboard(Settings())["components"]}


def test_dashboard_starts_empty(probe):
    assert dashboard(Settings())["counts"] == {"total": 5, "installed": 0, "running": 0}


def test_blocked_until_requirements_are_installed(probe):
    assert view()["php"]["status"] == "blocked"
    assert view()["php"]["blocked_by"] == ["apache"]

    install(probe, "apache")
    assert view()["php"]["status"] == "available"
    assert view()["php"]["blocked_by"] == []


def test_serviceless_components_are_installed_not_running(probe):
    """PHP has no unit of its own, so it must never claim to be 'running'."""
    install(install(probe, "apache"), "php")
    assert get_component("php").service is None
    assert view()["php"]["status"] == "installed"
    assert view()["apache"]["status"] == "running"


def test_stop_and_start_move_between_running_and_stopped(probe):
    install(probe, "mariadb")
    probe["mariadb"]["active"] = False
    assert view()["mariadb"]["status"] == "stopped"
    assert dashboard(Settings())["counts"] == {"total": 5, "installed": 1, "running": 0}

    probe["mariadb"]["active"] = True
    assert dashboard(Settings())["counts"]["running"] == 1


def test_remove_clears_version_and_unblocks_nothing(probe):
    install(probe, "apache")
    probe["apache"].update(installed=False, active=False, enabled=False, version=None)
    assert view()["apache"]["status"] == "available"
    assert view()["apache"]["version"] is None


def test_real_probe_reports_the_host_without_raising():
    payload = dashboard(Settings())
    assert len(payload["components"]) == 5
    assert payload["host"]["pretty_name"]
    assert payload["memory"]["total"] >= 0


def test_firewall_control_is_offered_only_where_ports_are_declared(probe):
    assert view()["apache"]["firewall"] == {
        "ports": [80, 443],
        "ports_text": "80/443",
        "allowed": False,
    }
    assert view()["mariadb"]["firewall"] is None


def test_firewall_button_toggles_with_the_rules(probe):
    probe["apache"]["firewall_allowed"] = True
    assert view()["apache"]["firewall"]["allowed"] is True

    probe["apache"]["firewall_allowed"] = False
    assert view()["apache"]["firewall"]["allowed"] is False


def test_no_firewall_control_when_ufw_is_not_running(probe):
    """A panel that cannot read ufw — or is looking at a host without it — must
    not offer a button that would silently do nothing."""
    probe["apache"]["firewall_active"] = False
    assert view()["apache"]["firewall"] is None


def test_ufw_status_parsing_reads_ports_not_profiles():
    from lamplight.systemops import _allowed_ports

    text = """Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
80,443/tcp                 ALLOW       Anywhere
Apache Full                ALLOW       Anywhere
3306                       DENY        Anywhere
80/tcp (v6)                ALLOW       Anywhere (v6)
"""
    assert _allowed_ports(text) == {22, 80, 443}


# -- memory ---------------------------------------------------------------


def test_format_bytes_stays_readable_at_every_scale():
    assert format_bytes(0) == "0 B"
    assert format_bytes(940 * 1024) == "940 KB"
    assert format_bytes(412 * MB) == "412 MB"
    assert format_bytes(3 * MB + 512 * 1024) == "3.5 MB"
    assert format_bytes(1024 * MB + 400 * MB) == "1.4 GB"


def test_memory_totals_lamplight_and_every_running_unit():
    items = [
        {"name": "Apache", "memory": 12 * MB},
        {"name": "PHP", "memory": None},
        {"name": "MariaDB", "memory": 100 * MB},
    ]
    payload = memory_view(items, 30 * MB)
    assert payload["total"] == 142 * MB
    assert payload["text"] == "142 MB"
    assert [part["name"] for part in payload["parts"]] == ["Lamplight", "Apache", "MariaDB"]


def test_nothing_measurable_reads_as_a_dash_rather_than_zero():
    """A host where systemd is not accounting for memory has no figure, and
    printing 0 B there would look like a measurement."""
    payload = memory_view([{"name": "Apache", "memory": None}], None)
    assert payload["parts"] == []
    assert payload["text"] == "—"


def test_a_stopped_unit_contributes_nothing(probe):
    probe["mariadb"].update(installed=True, active=False, memory=None)
    assert dashboard(Settings())["memory"]["parts"] == []


def test_systemd_non_answers_are_not_byte_counts():
    """MemoryCurrent answers `[not set]` or 2**64-1 with no cgroup accounting."""
    assert systemops._memory_value("[not set]") is None
    assert systemops._memory_value(str(2**64 - 1)) is None
    assert systemops._memory_value(None) is None
    assert systemops._memory_value("25165824") == 24 * MB


def test_lamplight_falls_back_to_its_own_rss_outside_systemd(monkeypatch):
    """Run from a checkout there is no lamplight.service to ask about."""
    monkeypatch.setattr(
        systemops, "service_states", lambda names: {n: systemops.ServiceState() for n in names}
    )
    monkeypatch.setattr(systemops, "process_memory", lambda: 30 * MB)
    monkeypatch.setattr(
        systemops, "package_states", lambda names: {n: systemops.PackageState() for n in names}
    )
    monkeypatch.setattr(systemops, "firewall_state", systemops.FirewallState)
    monkeypatch.setattr(status, "binary_path", lambda component: None)
    assert status._probe().own_memory == 30 * MB
