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
for w in create-dev-pr pr-labeler release_drafter semantic_release lint_pr \
         hacs_validate hassfest_validate python_validate check-manifest-version; do
  [ -f ".github/workflows/$w.yml" ] || FAIL "missing .github/workflows/$w.yml"
done
[ -f .github/release-drafter.yml ] || FAIL "missing .github/release-drafter.yml"
[ -f .github/dependabot.yml ]      || FAIL "missing .github/dependabot.yml"

# --- Action pins current (stale majors Dependabot would immediately bump) ---
grep -rnE 'actions/checkout@v[1-6]\b'                 .github/workflows/ && FAIL "stale actions/checkout (use v7)"
grep -rnE 'actions/setup-python@v[1-5]\b'             .github/workflows/ && FAIL "stale actions/setup-python (use v6)"
grep -rnE 'softprops/action-gh-release@v[12]\b'       .github/workflows/ && FAIL "stale action-gh-release (use v3)"
grep -rnE 'amannn/action-semantic-pull-request@v[1-5]\b' .github/workflows/ && FAIL "stale semantic-pull-request (use v6)"

# --- Workflow correctness ---
grep -q "Remove superseded" .github/workflows/pr-labeler.yml 2>/dev/null \
  || FAIL "pr-labeler.yml missing the removal-only superseded-label step"
grep -q "dependabot\[bot\]" .github/workflows/check-manifest-version.yml 2>/dev/null \
  || WARN "check-manifest-version may not exempt dependabot[bot]"
grep -q "gh release list" .github/workflows/check-manifest-version.yml 2>/dev/null \
  || WARN "check-manifest-version may not compare against the last published release"

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
  [ -f "${CC}quality_scale.yaml" ] || FAIL "missing quality_scale.yaml"
  M="${CC}manifest.json"
  grep -q '"integration_type"' "$M" 2>/dev/null || FAIL "manifest.json missing integration_type"
  grep -q '"issue_tracker"'    "$M" 2>/dev/null || FAIL "manifest.json missing issue_tracker (HACS requires it)"
fi

[ "$fail" = 0 ] && { echo "✅ skill audit passed"; exit 0; } || { echo "skill audit FAILED"; exit 1; }
