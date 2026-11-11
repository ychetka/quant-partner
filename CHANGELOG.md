# Changelog

Generated from `.changes/` per-PR entries at release time by
`tools/release_notes.sh --version X.Y.Z --apply`. Entries before
v0.1.33 predate the convention; see `git tag`/GitHub Releases for
the earlier history.

## [0.1.40] - 2026-11-11

### User-facing

#### feat
- PRs changing files must now carry a release-notes record (fragment, CHANGELOG change, or no-changes-entry marker) - enforced by CI and the merge gate. `tools` (issue #82, canonical devin-session-bridge #135)

### Codebase

#### tools
- [feat] tools/check_changes_entry.sh runs in lint on pull_request and inside repo_merge.sh as a pre-merge guard (missing_changes_entry). Why: Canonical rollout of the gate that prevents merges without a release record (sessions-bridge PRs #128/#132 version_drift precedent). (issue #82, canonical devin-session-bridge #135)

## [0.1.36] - 2026-11-10

### User-facing

#### feat
- Auto-review now reports provider token usage and an estimated per-review cost in the posted review body, the job summary and per-run usage.json workflow artifacts. `partners` (issue #74)

### Codebase

#### partners
- [feat] Plumbs Gemini usageMetadata through CompletionResult.usage into the advisory artifact's new optional usage object (summed across the repair retry), and adds usage/cost reporting to tools/pr_review.py plus a usage summary step and usage.json artifact upload in the auto_review workflow. Why: The auto-review layer needed observable spend without a state backend; only provider-reported counters are used, so artifacts without usage stay schema-valid and costs remain estimates checked against AI Studio quota. (issue #74)

## [0.1.35] - 2026-11-10

### User-facing

#### feat
- Pull requests now get an automated advisory review posted by github-actions[bot] with an AUTO_REVIEW_VERDICT header (requires the GEMINI_API_KEY repository secret). `partners` (issue #72, ADR-0002)

### Codebase

#### partners
- [feat] Adds the gemini-backed auto-reviewer partner (review toolset, skills/auto-reviewer/reviewer pack), tools/pr_review.py building the GitHub review payload, the auto_review workflow and docs. Why: The code-reviewer partner dies with the Copilot quota, so a dedicated Gemini instance keeps automated review working; publishing as an advisory COMMENT review preserves merge authority while giving inline findings. See ADR-0002. (issue #72, ADR-0002)

## [0.1.34] - 2026-11-09

### User-facing

#### feat
- Release notes are now generated from per-PR `.changes/` entries, which also feed CHANGELOG.md at release time. `tools` (issue #69, ADR-0001)

### Codebase

#### tools
- [feat] Adopts the fleet changes-mechanism convention - `.changes/` per-PR frontmatter entries, `tools/release_notes.sh` aggregation (User-facing + Codebase sections), `docs/adr/` rules, PR-template checklist and the release-prep step. Why: Hand-assembled release notes drifted from what each PR actually shipped; a machine-readable entry per PR makes release notes generated, reviewable and identical across orchestrated repos. (issue #69, ADR-0001)
