"""Opening and closing a component's ports in ufw.

Its own module because both halves of the app need it and neither can import
the other: status.py renders the button, installer.py runs the command, and
installer.py already imports status.
"""

from __future__ import annotations

FIREWALL_OPEN = "firewall-open"
FIREWALL_CLOSE = "firewall-close"
FIREWALL_ACTIONS = (FIREWALL_OPEN, FIREWALL_CLOSE)

PROTOCOL = "tcp"


def rule(port: int) -> str:
    return f"{port}/{PROTOCOL}"


def argv(action: str, port: int) -> list[str]:
    """The ufw command for one port.

    `ufw allow` is idempotent; `ufw delete allow` is not, and exits non-zero
    when the rule was never there — which the caller treats as already-closed.
    """
    if action == FIREWALL_OPEN:
        return ["ufw", "allow", rule(port)]
    if action == FIREWALL_CLOSE:
        return ["ufw", "delete", "allow", rule(port)]
    raise ValueError(f"Unknown firewall action: {action}")
