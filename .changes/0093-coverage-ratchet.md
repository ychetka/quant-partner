---
# type: fix | feat | refactor | chore | docs
type: chore
scope: ci
user_visible: false
breaking: false
user_summary: "-"
dev_summary: Coverage workflow gains a ratchet - a hard "Enforce coverage floor" step reads coverage.json and fails the job below COVERAGE_TOTAL_FLOOR (85), COVERAGE_HTTP_FLOOR (53) or COVERAGE_COPILOT_FLOOR (60), with a notice when the total runs more than 2 points above the floor.
why: Issue #93 - the coverage job was fully report-only (every step continue-on-error), so regressions could merge silently. A fail-closed ratchet step with per-file floors for the two coverage-debt modules keeps the signal without blocking on the known debt.
refs: "issue #93"
---

## Spec impact
- .github/workflows/coverage.yml: new job env vars COVERAGE_TOTAL_FLOOR /
  COVERAGE_HTTP_FLOOR / COVERAGE_COPILOT_FLOOR and a final hard step that
  parses coverage.json with python3 stdlib only; all earlier steps stay
  continue-on-error.
- README.md Development section documents the floor values, semantics and
  the bump procedure (re-measure on main, raise env in the same PR).
