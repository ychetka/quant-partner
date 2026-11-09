---
type: feat
scope: tools
user_visible: true
breaking: false
user_summary: Release notes are now generated from per-PR `.changes/`
  entries, which also feed CHANGELOG.md at release time.
dev_summary: Adopts the fleet changes-mechanism convention - `.changes/`
  per-PR frontmatter entries, `tools/release_notes.sh` aggregation
  (User-facing + Codebase sections), `docs/adr/` rules, PR-template
  checklist and the release-prep step.
why: Hand-assembled release notes drifted from what each PR actually
  shipped; a machine-readable entry per PR makes release notes
  generated, reviewable and identical across orchestrated repos.
refs: issue #69, ADR-0001
---
