# ADR-0001: Adopt the fleet changes-mechanism convention

- Status: accepted
- Date: 2026-11-09
- Refs: issue #69, orchestrator directive (fleet rollout, pilot repo #2)

## Context

Release notes were hand-assembled per release and drifted from what each
PR actually shipped; reviewers had no uniform machine-readable record of
intent. The fleet-wide convention shipped in sessions-bridge v0.5.1 is
copyable verbatim, and this repository is pilot repo #2 of the rollout.

## Decision

Adopt the changes-mechanism convention unchanged in shape:

- Every PR adds exactly one `.changes/NNNN-slug.md` frontmatter entry
  (`type`/`scope`/`user_visible`/`breaking`/`user_summary`/
  `dev_summary`/`why`/`refs`), which is the reviewed source of truth
  for release notes.
- `tools/release_notes.sh` (copied verbatim) aggregates pending entries
  into `### User-facing` and `### Codebase` sections;
  `--version X.Y.Z --apply` folds them into `CHANGELOG.md` and deletes
  the consumed files as the release-prep step before tagging.
- `docs/adr/` holds numbered ADRs; `type: feat`, `breaking: true`, or
  cross-module refactors require one, while fix/docs/chore entries act
  as their own micro-ADRs via `why`.
- The PR template carries the `.changes` + "ADR required" checkboxes.

Port-specific adjustments are limited to the scope vocabulary
(`server`, `backends`, `review`, `partners`, `tools`, `docs`, `ci`),
the repo's own release script name in CONTRIBUTING.md, and the
test-plan commands in the PR template.

## Consequences

- Release notes become generated artifacts, reviewable per-PR instead of
  reconstructed at tag time.
- `.changes/` only ever holds unreleased entries, so "since the last
  tag" needs no tag arithmetic.
- Contributors must write one extra small file per PR and tick the ADR
  checkbox honestly; a missing entry is a review failure, not a CI
  failure (no lint gate exists yet).
- `CHANGELOG.md` grows one section per release, newest on top.

## Alternatives considered

- Keep hand-written release notes: rejected - already drifting, not
  reviewable per-PR, inconsistent across orchestrated repos.
- Generate notes from PR titles/bodies: rejected - titles describe
  mechanics, not user impact; the frontmatter entry captures intent at
  the moment of change, before merge-time loss of context.
- Commitizen/conventional-commit machinery: rejected - heavier tooling
  for the same outcome; the spec needed no new dependencies.
