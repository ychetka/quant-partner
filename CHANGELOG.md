# Changelog

Generated from `.changes/` per-PR entries at release time by
`tools/release_notes.sh --version X.Y.Z --apply`. Entries before
v0.1.33 predate the convention; see `git tag`/GitHub Releases for
the earlier history.

## [0.1.34] - 2026-11-09

### User-facing

#### feat
- Release notes are now generated from per-PR `.changes/` entries, which also feed CHANGELOG.md at release time. `tools` (issue #69, ADR-0001)

### Codebase

#### tools
- [feat] Adopts the fleet changes-mechanism convention - `.changes/` per-PR frontmatter entries, `tools/release_notes.sh` aggregation (User-facing + Codebase sections), `docs/adr/` rules, PR-template checklist and the release-prep step. Why: Hand-assembled release notes drifted from what each PR actually shipped; a machine-readable entry per PR makes release notes generated, reviewable and identical across orchestrated repos. (issue #69, ADR-0001)
