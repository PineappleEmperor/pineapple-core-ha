"""Unit tests for the manifest version gate decision logic."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "manifest_gate", Path(__file__).parents[1] / "scripts" / "manifest_gate.py"
)
assert _SPEC and _SPEC.loader
manifest_gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(manifest_gate)
evaluate = manifest_gate.evaluate


def ok(*args, **kwargs) -> bool:
    return evaluate(*args, **kwargs)[0]


def test_unchanged_vs_last_release_fails() -> None:
    assert not ok("1.1.0", "1.1.0", "1.1.0", ["fix"])

def test_feature_minor_bump_passes() -> None:
    assert ok("1.1.0", "1.1.0", "1.2.0", ["feature"])

def test_feature_only_patch_under_bumps() -> None:
    assert not ok("1.1.0", "1.1.0", "1.1.1", ["feature"])

def test_chore_rides_in_cycle_minor() -> None:  # the shipped regression
    assert ok("1.1.0", "1.2.0", "1.2.0", ["chore"])

def test_chore_overbump_beyond_cycle_fails() -> None:
    assert not ok("1.1.0", "1.2.0", "2.0.0", ["chore"])

def test_breaking_major_passes() -> None:
    assert ok("1.1.0", "1.2.0", "2.0.0", ["xfeat"])

def test_prerelease_only_needs_to_differ() -> None:
    assert ok("1.1.0", "1.1.0", "2.0.0rc1", ["feature"])
    assert not ok("2.0.0rc1", "2.0.0rc1", "2.0.0rc1", ["feature"])

def test_final_graduates_prerelease() -> None:  # 2.0.0rc19 -> 2.0.0, even feature-labelled
    assert ok("2.0.0rc19", "2.0.0rc20", "2.0.0", ["feature"])
    assert not ok("2.0.0", "2.0.0", "2.0.0", ["feature"])  # already final -> still must bump

def test_dependabot_exempt() -> None:
    assert ok("1.1.0", "1.1.0", "1.1.0", [], dependabot=True)

def test_no_managed_label_passes_when_changed() -> None:
    assert ok("1.1.0", "1.1.0", "1.1.5", [])
