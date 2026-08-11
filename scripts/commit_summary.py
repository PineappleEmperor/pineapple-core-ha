#!/usr/bin/env python3
"""Group a PR's commit subjects by Conventional Commit type.

Used by the `commit-summary` job in .github/workflows/pr-checks.yml to build the
marked block in a PR body, and by the `title-check` job to suggest a title type.

Lives in a script, not inline in the workflow, so it can be unit-tested — an
inline heredoc cannot be, and a silently-wrong classifier corrupts release notes
without ever failing a build.
"""

from __future__ import annotations

import argparse
import re
import sys

TYPE = re.compile(r"^(?P<type>[a-zA-Z]+)(\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<desc>.*)$")

# Types the release-drafter autolabeler folds into `chore` -> 🧰 Maintenance.
MAINT = frozenset({"chore", "docs", "refactor", "perf", "test", "build", "ci", "style"})

# The manifest/plugin version bump is release plumbing, not a changelog entry.
# Anchored on the SHAPE ("bump … version"), not on "any bump mentioning something
# version-shaped": an earlier pattern ended in `to v?\d+\.\d+`, which silently ate
# `chore: bump actions/checkout from 6.0.0 to 7.0.1` — i.e. every semver dependency
# bump vanished from the notes.
BUMP = re.compile(
    r"^[a-z]+(\([^)]*\))?:\s*bump\s+(the\s+)?"
    r"((manifest|plugin|integration|skill)\s+)?version\b",
    re.I,
)

ORDER = ("breaking", "feat", "fix", "maint", "other")
HEADINGS = {
    "breaking": "  **🚨 Breaking**",
    "feat": "  **🚀 Features**",
    "fix": "  **🔧 Fixes**",
    "maint": "  **🧰 Maintenance**",
    "other": "  **📦 Other**",
}
# Suggested PR title type per winning commit group: (title, category, semver bump).
SUGGESTIONS = {
    "breaking": ("`feat!:` (or any `type!:`)", "🚨 Breaking Change", "major"),
    "feat": ("`feat:`", "🚀 Features", "minor"),
    "fix": ("`fix:`", "🔧 Fixes", "patch"),
    "maint": ("`chore:`", "🧰 Maintenance", "patch"),
    "other": ("`chore:`", "🧰 Maintenance", "patch"),
}


def classify(subject: str) -> tuple[str, str]:
    """Return (group, description) for one commit subject."""
    m = TYPE.match(subject)
    if not m:
        return "other", subject.strip()
    desc = m.group("desc").strip()
    if not desc:
        # `feat:` with no description carries no information; keep the raw subject
        # so it is visible rather than rendering an empty bullet.
        return "other", subject.strip()
    if m.group("bang"):
        return "breaking", desc
    t = m.group("type").lower()
    if t in ("feat", "feature"):
        return "feat", desc
    if t == "fix":
        return "fix", desc
    if t in MAINT:
        return "maint", desc
    return "other", desc


def group(subjects: list[str]) -> dict[str, list[str]]:
    """Group non-plumbing subjects by type, preserving order within each group."""
    groups: dict[str, list[str]] = {k: [] for k in ORDER}
    for s in subjects:
        s = s.strip()
        if not s or BUMP.match(s):
            continue
        key, desc = classify(s)
        if desc not in groups[key]:  # a rebase can duplicate a subject verbatim
            groups[key].append(desc)
    return groups


def render(subjects: list[str]) -> str:
    """The marked-block body, or "" when it would add nothing.

    A single bullet is always the PR title minus its type prefix, so the block
    just restates the heading above it. Measured on three published releases:
    every one carried at least one such block and none carried the multi-type
    case the sub-heads exist for. Emit nothing and let the caller drop the block.
    """
    groups = group(subjects)
    used = [k for k in ORDER if groups[k]]
    if not used:
        return ""
    if sum(len(groups[k]) for k in used) == 1:
        return ""
    lines: list[str] = []
    for key in used:
        # Sub-heads only when the PR spans >1 type: release-drafter already files
        # the PR under one category heading, so a lone sub-head duplicates it.
        if len(used) > 1:
            lines.append(HEADINGS[key])
        lines += [f"  - {d}" for d in groups[key]]
    return "\n".join(lines)


def winning(subjects: list[str]) -> str:
    """Highest-impact group present — the title type a PR's commits imply."""
    groups = group(subjects)
    for key in ORDER:
        if groups[key]:
            return key
    return "maint"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("render", "winning"), default="render")
    ap.add_argument("--subjects", default="-", help="file of commit subjects, or - for stdin")
    args = ap.parse_args()

    src = sys.stdin if args.subjects == "-" else open(args.subjects, encoding="utf-8")
    with src as fh:
        subjects = fh.read().splitlines()

    print(render(subjects) if args.mode == "render" else winning(subjects))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
