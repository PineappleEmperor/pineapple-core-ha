#!/usr/bin/env bash
# Skill-conformance audit: verifies the ha-integration skill was actually followed —
# canonical workflows present, action pins current, antipatterns absent, quality_scale
# present. Mechanical subset of Mode 4. Exit 1 on any FAIL. Runs locally and in CI.
set -uo pipefail

CC=$(ls -d custom_components/*/ 2>/dev/null | head -1)
fail=0
FAIL() { echo "❌ FAIL: $*"; fail=1; }
WARN() { echo "⚠️  WARN: $*"; }

# --- Canonical workflows present ---
# release.yml: absent -> HACS install fails with "Could not download" on a
# zip_release repo. quality_audit.yml: absent -> THIS script never runs in CI,
# and that is the one absence it can never report on a PR.
for w in pr-checks release_drafter semantic_release lint_pr \
         hacs_validate hassfest_validate python_validate \
         release quality_audit; do
  [ -f ".github/workflows/$w.yml" ] || FAIL "missing .github/workflows/$w.yml"
done
[ -f .github/release-drafter.yml ] || FAIL "missing .github/release-drafter.yml"
[ -f .github/dependabot.yml ]      || FAIL "missing .github/dependabot.yml"
[ -f .gitignore ]                  || FAIL "missing .gitignore (copy templates/.gitignore)"

# Build artefacts must never be tracked. A committed .pyc under templates/ is
# copied verbatim into every scaffolded repo — a stale compiled conftest, tagged
# for one Python/pytest version. This has happened: `git add -A` after a local
# pytest run committed three of them.
if git rev-parse --git-dir >/dev/null 2>&1; then
  tracked_pyc=$(git ls-files | grep -E '__pycache__|\.py[cod]$' || true)
  [ -n "$tracked_pyc" ] && { echo "$tracked_pyc"; FAIL "compiled Python artefacts are tracked (git rm --cached, and add them to .gitignore)"; }
fi

# --- Canonical scripts present (pr-checks.yml's version-gate job shells out to
# the gate; a missing script fails that job at runtime on every PR) ---
[ -f scripts/manifest_gate.py ]      || FAIL "missing scripts/manifest_gate.py (pr-checks.yml's version-gate shells out to it)"
[ -f tests/test_manifest_gate.py ]   || FAIL "missing tests/test_manifest_gate.py (the gate's logic must stay unit-tested)"
[ -f scripts/commit_summary.py ]     || FAIL "missing scripts/commit_summary.py (pr-checks.yml's commit-summary shells out to it)"
[ -f tests/test_commit_summary.py ]  || FAIL "missing tests/test_commit_summary.py (the classifier must stay unit-tested)"
# Classifier logic must NOT be inlined back into the workflow: an inline heredoc
# cannot be unit-tested, and a wrong classifier corrupts release notes silently
# rather than failing a build. That is how the semver-bump regression shipped.
grep -q 'MAINT = ' .github/workflows/pr-checks.yml 2>/dev/null \
  && FAIL "pr-checks.yml inlines the commit classifier (call scripts/commit_summary.py instead)"

# NOTE: this loop proves each workflow EXISTS, never that it MATCHES the skill's
# template — a consuming repo has no copy of templates/ to diff against. Content
# fidelity is the first item of the Mode 4 judgement checklist, run by an agent
# that does have the skill on disk. Green here is not evidence of a faithful copy.

# --- CI actually runs the tests ---
if [ -d tests ]; then
  [ -f requirements.test.txt ] \
    || FAIL "tests/ exists but requirements.test.txt is missing (pytest step cannot install the suite)"
  # Root conftest, not tests/conftest: it must import `custom_components` before
  # p-h-c-c binds that name to its own bundled package, or HA can't find the
  # integration and every setup test fails with "Integration not found".
  if [ -f conftest.py ]; then
    grep -q '^import custom_components' conftest.py \
      || FAIL "conftest.py does not import custom_components (HA will not discover the integration)"
    grep -q 'enable_custom_integrations' conftest.py \
      || FAIL "conftest.py does not pull in enable_custom_integrations"
  else
    FAIL "missing root conftest.py (must be at the repo root, not tests/conftest.py)"
  fi
  grep -qE 'asyncio_mode[[:space:]]*=[[:space:]]*"auto"' pyproject.toml 2>/dev/null \
    || FAIL "pyproject.toml missing asyncio_mode = \"auto\" (async tests never run)"
  grep -q 'pytest' .github/workflows/python_validate.yml 2>/dev/null \
    || FAIL "python_validate.yml has no pytest step (quality_scale 'done' rules would go unproven)"
else
  WARN "no tests/ directory — every quality_scale rule marked 'done' is unproven"
fi
if [ -f requirements.test.txt ]; then
  grep -qE 'pytest-homeassistant-custom-component[[:space:]]*==' requirements.test.txt \
    || WARN "pytest-homeassistant-custom-component is unpinned (it hard-pins the HA version the suite tests against)"
fi

# --- Action pins current (stale majors Dependabot would immediately bump) ---
# ⚠️ These majors are a snapshot, verified 2026-08-07. They rot silently: a rule
# written for "flag v1-v5" keeps passing a v6 pin long after v7 ships, so the
# check meant to catch staleness goes stale in the same place. Re-derive with:
#   for r in actions/checkout actions/setup-python softprops/action-gh-release \
#            amannn/action-semantic-pull-request release-drafter/release-drafter; do
#     echo "$r $(gh api repos/$r/releases/latest --jq .tag_name)"; done
# and update BOTH the pattern here and the pin in the templates. See the
# Freshness table in SKILL.md.
grep -rnE 'actions/checkout@v[1-6]\b'                    .github/workflows/ && FAIL "stale actions/checkout (use v7)"
grep -rnE 'actions/setup-python@v[1-6]\b'                .github/workflows/ && FAIL "stale actions/setup-python (use v7)"
grep -rnE 'softprops/action-gh-release@v[12]\b'          .github/workflows/ && FAIL "stale action-gh-release (use v3)"
grep -rnE 'amannn/action-semantic-pull-request@v[1-5]\b' .github/workflows/ && FAIL "stale semantic-pull-request (use v6)"
grep -rnE 'release-drafter/release-drafter(/autolabeler)?@v[1-6]\b' .github/workflows/ && FAIL "stale release-drafter (use v7)"

# --- Workflow correctness ---
grep -q "Remove superseded" .github/workflows/pr-checks.yml 2>/dev/null \
  || FAIL "pr-checks.yml missing the removal-only superseded-label step"
grep -q "dependabot\[bot\]" .github/workflows/pr-checks.yml 2>/dev/null \
  || WARN "pr-checks.yml may not exempt dependabot[bot] from the version gate"
grep -q "gh release list" .github/workflows/pr-checks.yml 2>/dev/null \
  || WARN "pr-checks.yml may not compare against the last published release"

# --- pr-checks.yml: ordering and pull_request_target safety ---
# Jobs that read labels must declare `needs: label`. Separate workflows cannot be
# sequenced at all (the autolabeler's `labeled` event is suppressed by the
# GITHUB_TOKEN anti-recursion rule), which is why these live in one workflow.
if [ -f .github/workflows/pr-checks.yml ]; then
  P=.github/workflows/pr-checks.yml
  grep -q 'pull_request_target' "$P" \
    || FAIL "pr-checks.yml must use pull_request_target (fork PRs get a read-only token otherwise)"
  [ "$(grep -c 'needs: label' "$P")" -ge 2 ] \
    || FAIL "pr-checks.yml: label-reading jobs must declare 'needs: label' (else they race the autolabeler)"
  grep -q "user.type != 'Bot'" "$P" \
    || FAIL "pr-checks.yml does not skip bot-authored PRs"
  # Any checkout under pull_request_target must pin the BASE, never the PR head:
  # the token is writable, so PR-authored code must never run.
  if grep -q 'actions/checkout' "$P"; then
    grep -q 'ref: ${{ github.event.pull_request.base.sha }}' "$P" \
      || FAIL "pr-checks.yml checks out without pinning base.sha (never run PR code under pull_request_target)"
    grep -q 'head.sha' "$P" && grep -A2 'actions/checkout' "$P" | grep -q 'head.sha' \
      && FAIL "pr-checks.yml checks out the PR head under pull_request_target"
  fi
  # Untrusted strings (PR title, the PR's own manifest version) must reach run: via
  # env, never `${{ }}` interpolation.
  # actions/checkout CLEARS the workspace, so a job that checks out after writing
  # a file there loses it. That shipped: the commit-summary job fetched subjects
  # into subjects.txt, then checked out, then read a file that no longer existed.
  python3 - "$P" <<'PYCO' || FAIL "pr-checks.yml: actions/checkout must be the FIRST step of its job (it clears the workspace)"
import sys, yaml
w = yaml.safe_load(open(sys.argv[1]))
bad = [j for j, jd in w["jobs"].items()
       if any("actions/checkout" in str(s.get("uses", "")) for s in jd["steps"])
       and "actions/checkout" not in str(jd["steps"][0].get("uses", ""))]
for j in bad:
    print(f"    job '{j}' checks out after another step has run")
sys.exit(1 if bad else 0)
PYCO
  python3 - "$P" <<'PYCHK' || FAIL "pr-checks.yml interpolates \${{ }} inside a run: block (use env:)"
import sys, re, yaml
w = yaml.safe_load(open(sys.argv[1]))
bad = [(j, s.get("name"), m)
       for j, jd in w["jobs"].items() for s in jd["steps"]
       for m in re.findall(r"\$\{\{\s*([^}]+?)\s*\}\}", s.get("run", ""))]
for b in bad:
    print(f"    {b}")
sys.exit(1 if bad else 0)
PYCHK
fi

# --- No HACS/hassfest check may be ignored ---
# `ignore:` disqualifies the repo from the HACS default store; it exists for
# debugging only. Empirically load-bearing: in eval scenario 01, BOTH control
# runs (skill withheld) reached for `ignore: brands` to make a failing check pass
# on day one, each rationalising it as temporary. Neither would have shipped to
# the default store. The rule was documented from the start and ungated until now.
for w in hacs_validate hassfest_validate; do
  f=".github/workflows/$w.yml"
  [ -f "$f" ] || continue
  grep -nE '^[[:space:]]*ignore:' "$f" \
    && FAIL "$w.yml sets ignore: — ignoring any check disqualifies the repo from the HACS default store"
done

# --- Exactly ONE labeler ---
# pr-checks.yml's `label` job is it. A second labeler (classically a
# release-drafter autolabeler job on pull_request) makes labels flap AND breaks
# pr-checks' `needs: label` ordering: title-check waits for the first labeler
# while the second is still applying labels. This drifted into the skill's own
# repo and went unnoticed until a manual template diff.
if [ -f .github/workflows/release_drafter.yml ]; then
  python3 - .github/workflows/release_drafter.yml <<'PYRD' || FAIL "release_drafter.yml must be push-only with no autolabeler job (pr-checks.yml is the sole labeler)"
import sys, yaml
w = yaml.safe_load(open(sys.argv[1]))
triggers = set((w.get(True) or w.get("on") or {}))
bad = []
if triggers - {"push", "workflow_dispatch"}:
    bad.append(f"triggers {sorted(triggers)} (expected push only)")
for name, jd in w.get("jobs", {}).items():
    if "label" in name.lower():
        bad.append(f"job '{name}' looks like a second labeler")
for b in bad:
    print(f"    {b}")
sys.exit(1 if bad else 0)
PYRD
fi

# No workflow may open PRs. create-dev-pr.yml is the superseded auto-opener: it cannot
# serve fork contributions (push never fires on a fork; a fork's pull_request token is
# read-only) and it overwrote human PR titles. PRs are opened by humans.
[ -f .github/workflows/create-dev-pr.yml ] \
  && FAIL "create-dev-pr.yml is superseded (PRs are opened manually; use pr-checks.yml)"
grep -rln 'gh pr create' .github/workflows/ 2>/dev/null \
  && FAIL "a workflow opens PRs with 'gh pr create' (PRs are opened manually)"

# --- Antipatterns in integration code (high-confidence) ---
if [ -n "$CC" ]; then
  ap() { grep -rnE "$1" "$CC" 2>/dev/null && FAIL "$2"; }
  ap 'discovery\.async_load_platform' "deprecated discovery.async_load_platform (use NotifyEntity / platform forward)"
  ap 'BaseNotificationService'         "deprecated BaseNotificationService (use NotifyEntity)"
  ap 'update_before_add=True'          "update_before_add=True (populate via property or _handle_coordinator_update)"
  ap 'OptionsFlowHandler'              "deprecated OptionsFlowHandler name (use OptionsFlow)"
  ap 'PlatformNotReady'                "PlatformNotReady in a config-entry integration (use ConfigEntryNotReady)"
  ap '_LOGGER\.[a-z]+\([[:space:]]*f"' "f-string in a logging call (use lazy % args — ruff G004)"
  ti=$(grep -rn '# type: ignore' "$CC" 2>/dev/null | grep -v 'import-untyped')
  [ -n "$ti" ] && { echo "$ti"; FAIL "bare # type: ignore (Platinum: only [import-untyped] with a reason)"; }
  grep -rq 'from __future__ import annotations' "$CC"__init__.py 2>/dev/null \
    || WARN "no 'from __future__ import annotations' in __init__.py"

  # --- quality_scale + manifest honesty ---
  if [ -f "${CC}quality_scale.yaml" ]; then
    # Existence proved nothing: a file containing one rule passed a real audit
    # while ~51 canonical rules were absent and that one rule was an unproven
    # `done`. Snapshot of the canonical set — keep in lockstep with the rule
    # lists in SKILL.md (see its Freshness table).
    python3 - "${CC}quality_scale.yaml" <<'PYQS' || FAIL "quality_scale.yaml does not enumerate the canonical rule set"
import sys, yaml
CANON = {
 "action-setup","appropriate-polling","brands","common-modules","config-flow-test-coverage",
 "config-flow","dependency-transparency","docs-actions","docs-high-level-description",
 "docs-installation-instructions","docs-removal-instructions","entity-event-setup",
 "entity-unique-id","has-entity-name","runtime-data","test-before-configure",
 "test-before-setup","unique-config-entry","config-entry-unloading","log-when-unavailable",
 "entity-unavailable","action-exceptions","reauthentication-flow","parallel-updates",
 "test-coverage","integration-owner","docs-installation-parameters",
 "docs-configuration-parameters","entity-translations","entity-device-class","devices",
 "entity-category","entity-disabled-by-default","discovery","stale-devices","diagnostics",
 "exception-translations","icon-translations","reconfiguration-flow","dynamic-devices",
 "discovery-update-info","repair-issues","docs-use-cases","docs-supported-devices",
 "docs-supported-functions","docs-data-update","docs-known-limitations","docs-troubleshooting",
 "docs-examples","async-dependency","inject-websession","strict-typing",
}
rules = (yaml.safe_load(open(sys.argv[1])) or {}).get("rules") or {}
missing = sorted(CANON - set(rules))
if missing:
    print(f"    {len(missing)} canonical rules absent, e.g. {missing[:6]}")
sys.exit(1 if missing else 0)
PYQS
  else
    FAIL "missing quality_scale.yaml"
  fi
  M="${CC}manifest.json"
  grep -q '"integration_type"' "$M" 2>/dev/null || FAIL "manifest.json missing integration_type"
  grep -q '"issue_tracker"'    "$M" 2>/dev/null || FAIL "manifest.json missing issue_tracker (HACS requires it)"
  # A manifest that claims config_flow without the module fails setup at runtime.
  if grep -q '"config_flow"[[:space:]]*:[[:space:]]*true' "$M" 2>/dev/null; then
    [ -f "${CC}config_flow.py" ] || FAIL "manifest declares config_flow: true but ${CC}config_flow.py is missing"
  fi
  # A panel integration declares `frontend` in dependencies; the frontend component's
  # pip requirement is NOT pulled in by `pip install homeassistant`, so without an
  # explicit pin every setup test fails in CI with "No module named 'hass_frontend'"
  # while usually passing locally (the package is already there from another install).
  if grep -qE '"(frontend|panel_custom)"' "$M" 2>/dev/null; then
    grep -qE '^[[:space:]]*home-assistant-frontend==' requirements.test.txt 2>/dev/null \
      || FAIL "manifest depends on frontend/panel_custom but requirements.test.txt has no home-assistant-frontend pin (every setup test will fail in CI with: No module named 'hass_frontend')"
  fi
  [ -f CLAUDE.md ]         || FAIL "missing CLAUDE.md (the skill's per-repo enforcement — without it no future session is told to re-invoke)"
  [ -f README.md ]         || FAIL "missing README.md (HACS 'information' and 'images' checks both need it)"
  [ -f pyrightconfig.json ] || WARN "missing pyrightconfig.json"
fi

# --- Coverage gaps closed 2026-08-11 -----------------------------------------
# Every check above was added reactively, one per bug. A cross-reference of the
# skill's stated rules against this script found five that were documented and
# never enforced; three had already been violated in the skill's own repo. These
# are those five.

# 1. Autolabeler rules must be TITLE-only. A `branch:` rule flaps whenever the
#    branch name disagrees with the commits (branch `chore/…`, commits `feat:`).
if [ -f .github/release-drafter.yml ]; then
  python3 - .github/release-drafter.yml <<'PYAL' || FAIL "release-drafter.yml autolabeler has non-title rules (title-only, or labels flap)"
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
bad = [r.get("label") for r in cfg.get("autolabeler", []) if set(r) - {"label", "title"}]
for b in bad:
    print(f"    rule '{b}' matches on something other than the title")
sys.exit(1 if bad else 0)
PYAL
fi

# 2. Docstrings are ONE line for functions and classes. MODULE docstrings are
#    exempt: SKILL.md's Code style constrains "public functions and classes", and a
#    file-level explanation of a load-bearing constraint is better placed in a module
#    docstring than demoted to a comment. Reported from the field — the rule was
#    stricter than the prose it enforced.
if [ -n "$CC" ]; then
  python3 - "$CC" <<'PYDS' || FAIL "multi-line docstring on a function or class in custom_components/ (single-line required; module docstrings are exempt)"
import ast, pathlib, sys
bad = []
for f in pathlib.Path(sys.argv[1]).rglob("*.py"):
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc and "\n" in doc.strip():
            name = getattr(node, "name", "<module>")
            print(f"    {f}:{getattr(node, 'lineno', 1)} {name}")
            bad.append(name)
sys.exit(1 if bad else 0)
PYDS
fi

# 3. The commit-msg hook must be present AND enabled. Shipping it is not enough:
#    a harness can inject AI-attribution trailers on every commit, and prose
#    alone loses that fight (which is why the hook exists).
if [ -f .githooks/commit-msg ]; then
  [ -x .githooks/commit-msg ] || FAIL ".githooks/commit-msg is not executable (chmod +x)"
  if git rev-parse --git-dir >/dev/null 2>&1; then
    [ "$(git config core.hooksPath 2>/dev/null)" = ".githooks" ] \
      || WARN "core.hooksPath is not .githooks — run: git config core.hooksPath .githooks"
  fi
else
  WARN "no .githooks/commit-msg (terse-subject + AI-trailer rejection)"
fi

# 4. Brand assets: exact square sizes, and the @2x variants. A present icon.png
#    with no icon@2x.png is the classic "icon shows only sometimes" bug — a
#    HiDPI client requests @2x, 404s, and falls back inconsistently.
if [ -n "$CC" ]; then
  [ -d "${CC}brand" ] || FAIL "missing ${CC}brand/ (HACS check-brands fails without icon.png)"
  python3 - "${CC}brand" <<'PYBR' || FAIL "brand assets missing or wrongly sized"
import pathlib, struct, sys

def size(p):
    b = p.read_bytes()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", b[16:24])

brand = pathlib.Path(sys.argv[1])
want = {"icon.png": (256, 256), "icon@2x.png": (512, 512)}
bad = False
for name, exp in want.items():
    f = brand / name
    if not f.exists():
        print(f"    missing {f}"); bad = True; continue
    got = size(f)
    if got != exp:
        print(f"    {f} is {got}, expected {exp}"); bad = True
for name in ("logo.png", "logo@2x.png"):
    if not (brand / name).exists():
        print(f"    missing {brand / name}"); bad = True
sys.exit(1 if bad else 0)
PYBR
fi

# 5. Self-diff, when this IS the skill repo. A consuming repo has no templates/
#    to compare against, but the skill repo carries them — and a second labeler
#    drifted into its own .github/ and survived months of prose review because
#    nothing ever ran this diff.
TMPL=$(ls -d plugins/*/skills/*/templates 2>/dev/null | head -1)
if [ -n "$TMPL" ] && [ -d "$TMPL/.github" ]; then
  # SEMANTIC comparison, not `diff`: block-vs-flow YAML sequences and quoted keys
  # are not drift, and a check that cries wolf over formatting gets ignored. Parsed
  # structures must match; comments are reported separately as a warning, because a
  # stale comment is how the corrected autolabeler vocabulary failed to propagate.
  python3 - "$TMPL/.github" .github <<'PYSD' || FAIL "this repo's .github/ diverges from its own templates/ (see Mode 4 sanctioned adaptations)"
import pathlib, sys, yaml

tmpl, repo = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
# Files this repo legitimately adapts; every other difference is drift.
SANCTIONED = {"release_drafter.yml"}   # reads a plugin manifest, not an HA one
bad = False
for tf in sorted(tmpl.rglob("*.yml")):
    rel = tf.relative_to(tmpl)
    if rel.name in SANCTIONED:
        continue
    rf = repo / rel
    if not rf.exists():
        print(f"    missing: .github/{rel}"); bad = True; continue
    if yaml.safe_load(tf.read_text()) != yaml.safe_load(rf.read_text()):
        print(f"    diverges: .github/{rel}"); bad = True
sys.exit(1 if bad else 0)
PYSD
fi

[ "$fail" = 0 ] && { echo "✅ skill audit passed"; exit 0; } || { echo "skill audit FAILED"; exit 1; }
