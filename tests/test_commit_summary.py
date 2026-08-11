"""Unit tests for scripts/commit_summary.py.

Load the standalone script by path — it is not an importable package.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "commit_summary",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "commit_summary.py",
)
cs = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(cs)


# --- classify ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("subject", "group", "desc"),
    [
        ("feat: add reconfigure flow", "feat", "add reconfigure flow"),
        ("feature: add thing", "feat", "add thing"),
        ("fix: close the session", "fix", "close the session"),
        ("chore: tidy", "maint", "tidy"),
        ("docs: explain", "maint", "explain"),
        ("refactor: split api.py", "maint", "split api.py"),
        ("perf: cache lookups", "maint", "cache lookups"),
        ("test: cover unload", "maint", "cover unload"),
        ("build: pin ruff", "maint", "pin ruff"),
        ("ci: add pytest step", "maint", "add pytest step"),
        ("style: reformat", "maint", "reformat"),
        # Breaking wins over the base type, with or without a scope.
        ("feat!: drop create-dev-pr", "breaking", "drop create-dev-pr"),
        ("fix!: change the payload shape", "breaking", "change the payload shape"),
        ("chore(deps)!: require python 3.14", "breaking", "require python 3.14"),
        ("feat(coordinator): add polling", "feat", "add polling"),
        # `revert:` is Conventional but maps to no autolabeler rule.
        ("revert: undo the flow change", "other", "undo the flow change"),
        # Case-insensitive type.
        ("FEAT: shout", "feat", "shout"),
        ("Fix: capitalised", "fix", "capitalised"),
        # No space after the colon.
        ("feat:no space", "feat", "no space"),
        # Extra whitespace is trimmed.
        ("fix:   padded   ", "fix", "padded"),
        # Not Conventional Commits at all.
        ("Merge branch 'main' into feat/x", "other", "Merge branch 'main' into feat/x"),
        ("WIP", "other", "WIP"),
        ("", "other", ""),
        # A scope containing a colon still parses (the group is [^)]*).
        ("feat(a:b): scoped", "feat", "scoped"),
        # Empty description keeps the raw subject rather than rendering "- ".
        ("feat:", "other", "feat:"),
        ("chore: ", "other", "chore:"),
    ],
)
def test_classify(subject: str, group: str, desc: str) -> None:
    """Each subject lands in the expected group with a clean description."""
    assert cs.classify(subject) == (group, desc)


# --- the version-bump filter (the regression that shipped) ------------------

@pytest.mark.parametrize(
    "subject",
    [
        "chore: bump manifest version to v5.0.1",
        "chore: bump plugin version to 5.0.1",
        "chore: bump version to 5.0.1",
        "chore: bump the manifest version",
        "chore: bump integration version to 1.2.3",
    ],
)
def test_release_plumbing_is_dropped(subject: str) -> None:
    """The manifest/plugin bump is plumbing, not a changelog entry."""
    assert cs.group([subject, "fix: real change"]) ["maint"] == []


@pytest.mark.parametrize(
    "subject",
    [
        "chore: bump actions/checkout from 6 to 7",
        # The shipped regression: `to v?\d+\.\d+` ate every semver dependency bump.
        "chore: bump actions/checkout from 6.0.0 to 7.0.1",
        "chore: bump pytest-homeassistant-custom-component from 0.13.350 to 0.13.354",
        "chore: bump homeassistant floor to 2026.8.0",
        "chore(deps): bump aiohttp from 3.9.0 to 3.10.1",
    ],
)
def test_dependency_bumps_survive(subject: str) -> None:
    """Dependabot's bumps are real changes and must reach the notes."""
    assert cs.group([subject])["maint"] == [subject.split(": ", 1)[1]]


# --- render -----------------------------------------------------------------

def test_single_commit_renders_nothing() -> None:
    """One bullet is the PR title minus its prefix — the block would add nothing."""
    assert cs.render(["feat: add reconfigure flow"]) == ""
    assert cs.render(["fix: close the session"]) == ""


def test_two_commits_still_render() -> None:
    """The block earns its place as soon as it says more than the title."""
    assert cs.render(["fix: one", "fix: two"]) == "  - one\n  - two"


def test_single_type_has_no_subheads() -> None:
    """One type -> the category heading above already says it; no sub-head."""
    out = cs.render(["fix: one", "fix: two"])
    assert out == "  - one\n  - two"
    assert "**" not in out


def test_multiple_types_get_subheads_in_severity_order() -> None:
    """Sub-heads appear only when they add information, hardest type first."""
    out = cs.render(["chore: c", "fix: b", "feat!: a", "feat: d"])
    assert out.splitlines() == [
        "  **🚨 Breaking**", "  - a",
        "  **🚀 Features**", "  - d",
        "  **🔧 Fixes**", "  - b",
        "  **🧰 Maintenance**", "  - c",
    ]


def test_empty_and_plumbing_only_input() -> None:
    """A PR with nothing but a version bump renders a placeholder, not junk."""
    assert cs.render([]) == ""
    assert cs.render(["chore: bump manifest version to v1.0.0"]) == ""
    assert cs.render(["", "   ", ""]) == ""


def test_duplicate_subjects_collapse() -> None:
    """A rebase can replay an identical subject; don't list it twice.

    Collapsing to one bullet then makes the block redundant, so it renders empty.
    """
    assert cs.render(["fix: same", "fix: same"]) == ""
    assert cs.render(["fix: same", "fix: same", "fix: other"]) == "  - same\n  - other"


def test_render_never_emits_an_empty_bullet() -> None:
    """Any input line must produce a bullet with visible text."""
    for line in cs.render(["feat:", "chore: ", "fix: real"]).splitlines():
        if line.strip().startswith("- "):
            assert line.strip()[2:].strip(), f"empty bullet from {line!r}"


# --- winning (drives the title suggestion) ----------------------------------

@pytest.mark.parametrize(
    ("subjects", "expected"),
    [
        (["feat: a"], "feat"),
        (["fix: a"], "fix"),
        (["chore: a"], "maint"),
        # Highest impact wins regardless of order.
        (["fix: a", "feat: b"], "feat"),
        (["feat: b", "fix: a"], "feat"),
        (["chore: a", "fix: b"], "fix"),
        (["fix: b", "chore: a"], "fix"),
        (["chore: a", "feat!: b", "fix: c"], "breaking"),
        (["feat: a", "feat!: b"], "breaking"),
        # No commits at all -> the most conservative suggestion.
        ([], "maint"),
        (["chore: bump manifest version to v1.0.0"], "maint"),
        # A lone unmappable subject.
        (["revert: undo"], "other"),
    ],
)
def test_winning(subjects: list[str], expected: str) -> None:
    """The suggested title type reflects the most impactful commit present."""
    assert cs.winning(subjects) == expected


def test_every_group_has_a_suggestion_and_heading() -> None:
    """No group can be reached that lacks a rendering or a suggestion."""
    for key in cs.ORDER:
        assert key in cs.HEADINGS
        assert key in cs.SUGGESTIONS
