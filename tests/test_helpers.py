"""Per-instance naming derived from a base URL."""

from __future__ import annotations

import pytest

from custom_components.pineapple_core.helpers import instance_label, instance_title


@pytest.mark.parametrize(
    ("base_url", "label", "title"),
    [
        ("https://core.juicebox.casa", "core", "Core"),
        ("https://budgets.juicebox.casa", "budgets", "Budgets"),
        ("https://budgets.juicebox.casa/", "budgets", "Budgets"),
        ("http://core.test:8123/api", "core", "Core"),
        ("core.juicebox.casa", "core", "Core"),  # scheme-less, as typed
        ("https://home-core.example.com", "home_core", "Home Core"),
        ("http://192.168.1.5:8080", "192_168_1_5", "192.168.1.5"),  # IPs stay whole
        ("http://localhost:9000", "localhost", "Localhost"),
    ],
)
def test_label_and_title_from_base_url(base_url: str, label: str, title: str) -> None:
    """Both names come from the URL's first host label."""
    assert instance_label(base_url) == label
    assert instance_title(base_url) == title
