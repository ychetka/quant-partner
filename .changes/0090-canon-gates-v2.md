# type: fix | feat | refactor | chore | docs
type: chore
scope: tools
user_visible: false
breaking: false
user_summary: "-"
dev_summary: Adopted canonical fleet quality-gate set v2 byte-for-byte from ai_controller (12 files md5-verified): hardened gate scripts now sharing migration_pattern.sh, the rebuilt agent profiles and hazards docs, plus controller-manager/harness.md and its development-cycle.md mirror.
why: Issue #90 - post-incident unification. The canonical set merges the five independent incident-audit fixes (issue #88) into the strictest implementation with 53 regression tests; local variants from PR #89 are intentionally replaced so the fleet converges on one canonical source.
refs: "issue #90"
---

## Spec impact
- .devin/scripts/migration_pattern.sh is NEW - single source for the
  migration-path pattern, sourced by migration_gate.sh and ship_commit.sh.
- DRY_RUN_CMD env var is no longer honored; projects declare
  "migration_dry_run_cmd" in .devin/project.json instead (not declared
  here - no project.json change needed).
- controller-manager/harness.md is again the full two-committee spec
  with the director session model folded in; development-cycle.md is a
  byte-identical mirror with its own provenance header.
- No repo-specific need is broken by the canonical set.
