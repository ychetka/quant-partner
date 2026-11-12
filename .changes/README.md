# .changes/ - per-PR change entries

Every PR adds ONE file here: `NNNN-slug.md` (issue/PR number + short
slug). Copy `_template.md`, fill the frontmatter, keep the content
public-safe - this repository is public and these entries feed the
published CHANGELOG/release notes.

## Enforcement (issue #82)

The entry is gated, not just conventional: `tools/check_changes_entry.sh`
runs in the CI lint job on every pull request and as a pre-merge guard
inside `.devin/scripts/repo_merge.sh`. A PR that changes files merges
only when it carries ONE of:

- a new `.changes/<slug>.md` fragment (tooling/chore changes use
  `user_visible: false` - the record still lands in the Codebase
  section);
- a `CHANGELOG.md` change - release-prep branches fold fragments into
  the changelog directly instead of carrying a pending file;
- the `no-changes-entry` marker in the PR title, body, or a label -
  the documented opt-out for pure docs/ops changes that produce no
  release notes.

Version model (issue #95, canonical): the release tag derives from
`[project].version` in `pyproject.toml` on the base head -
`repo_merge.sh` cuts the release automatically when a merge changes
it. A PR that should produce a release bumps `version` on its own
item branch before merge; `tools/release_notes.sh --version X.Y.Z
--apply` folds pending fragments into `CHANGELOG.md` as part of that
bump. PRs without a version bump merge without a release on purpose.
Parallel-PR version collisions are resolved by rebasing the losing
branch onto the merged bump.

Required frontmatter fields (indented continuation lines fold into
the previous field's value):

- `type`: fix | feat | refactor | chore | docs
- `scope`: the module/subsystem the change touches - one of
  `server`, `backends`, `review`, `partners`, `tools`, `tests`,
  `docs`, `ci` (extend the list here when a new module lands)
- `user_visible`: true | false - does an operator/integrator notice?
- `breaking`: true | false - does anything consuming the MCP tool
  surface or CLI need to change?
- `user_summary`: one sentence for the User-facing release section
  (required when user_visible is true; "-" otherwise)
- `dev_summary`: one sentence for the Codebase section
- `why`: motivation / decision link in one or two sentences
- `refs`: task IDs, issue/PR numbers, directive or decision links

ADR rule: `type: feat`, `breaking: true`, or a cross-module refactor
requires a real ADR under `docs/adr/` (see its README). For
fix/docs/chore this file IS the micro-ADR - make `why` carry the
decision.

At release time `tools/release_notes.sh --apply` folds all pending
entries into `CHANGELOG.md` and deletes the consumed files, so this
directory only ever holds unreleased changes.
