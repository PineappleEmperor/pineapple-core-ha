"""Per-instance naming derived from a Core base URL.

Two entries can point at two different Core deployments (`core.juicebox.casa`
and `budgets.juicebox.casa`). Anything user-visible or globally shared has to be
namespaced per entry or the two collide: the device and entity names in the HA
registry, and the notification `tag` the companion app dedups on — a shared tag
means one Core's notification silently replaces the other's.

The first host label is the distinguishing part of the URL, so both the human
title and the tag namespace derive from it.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse

from homeassistant.util import slugify

from .const import DOMAIN


def _is_ip(host: str) -> bool:
    """Whether a host is a bare IP literal rather than a domain name."""
    try:
        ip_address(host)
    except ValueError:
        return False
    return True


def _host_label(base_url: str) -> str:
    """Extract a base URL's distinguishing part: its first host label, or the IP."""
    raw = base_url if "//" in base_url else f"//{base_url}"
    host = (urlparse(raw).hostname or base_url).strip("[]")
    if _is_ip(host):
        return host
    return host.split(".")[0] or host


def instance_label(base_url: str) -> str:
    """Slug namespacing one instance's notification tags."""
    return slugify(_host_label(base_url)) or DOMAIN


def instance_title(base_url: str) -> str:
    """Human name for one instance — its config entry title and device name."""
    label = _host_label(base_url)
    if _is_ip(label):
        return label
    return label.replace("-", " ").title() or "Pineapple Core"
