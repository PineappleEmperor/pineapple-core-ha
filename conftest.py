"""Shared fixtures for the integration test suite.

Lives at the repo root, not in tests/ — see the import note below.
"""

from collections.abc import Generator

# MUST stay the first import, and this file MUST be at the repo root.
# pytest-homeassistant-custom-component bundles its own `custom_components`
# package under testing_config/ and binds the bare name `custom_components` to
# it while its plugin loads. Home Assistant discovers custom integrations with a
# plain `import custom_components` (see homeassistant.loader._get_custom_components),
# so whichever binding wins decides whether HA can see this repo's integration at
# all. A root conftest is imported before the plugin, so this import claims the
# name first. Without it every setup test fails with "Integration not found",
# which reads as a broken test rather than missing wiring.
# Being at the root also puts the repo on sys.path, so no `pythonpath` setting
# is needed and `pytest` works from any directory.
import custom_components  # noqa: F401
import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Let Home Assistant load integrations from custom_components/ in every test."""
    yield
