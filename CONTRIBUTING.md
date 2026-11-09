# Contributing

## Flow

issue -> branch `issue_<NNN>_v<X.Y.Z>` -> implementation -> tests ->
independent review -> PR -> merge -> release.

Everything that lands in the repository - commits, PRs, issues, code,
change entries, ADRs - is written in English and is public-safe.

## Change entries (`.changes/`)

Every PR adds exactly one `.changes/NNNN-slug.md` file - see
`.changes/README.md` for the required frontmatter
(`type`/`scope`/`user_visible`/`breaking`/`user_summary`/
`dev_summary`/`why`/`refs`). This file is the reviewed source of the
changelog; keep it accurate and public-safe.

ADR rule: `type: feat`, `breaking: true`, or a cross-module refactor
requires an ADR under `docs/adr/` (copy `_template.md`, next number).
For `fix`/`docs`/`chore` the `.changes` file itself is the micro-ADR -
its `why` must carry the decision. The PR template's "ADR required"
checkbox declares which path was taken.

## Release notes

Pending `.changes/` entries ARE the "since last tag" set - the
directory is emptied at every release, so no tag arithmetic exists.

- `tools/release_notes.sh` prints the generated notes:
  `### User-facing` (only `user_visible: true`, grouped by type) and
  `### Codebase` (all entries grouped by scope with `dev_summary` +
  `why`).
- `tools/release_notes.sh --version X.Y.Z --apply` is the release-prep
  step: it inserts the generated `## [X.Y.Z] - <date>` section into
  `CHANGELOG.md` under the title and deletes the consumed entry files.
  Capture the notes BEFORE `--apply` (entries are consumed afterwards)
  and hand them to the release script so the GitHub release body is
  identical to the changelog section:

  ```bash
  NOTES="$(tools/release_notes.sh --version X.Y.Z)"
  tools/release_notes.sh --version X.Y.Z --apply
  # commit CHANGELOG.md + the emptied .changes/ on the release branch
  .devin/scripts/repo_release.sh patch --notes "$NOTES"
  ```

  Without `--notes`, `repo_release.sh` falls back to
  `gh release create --generate-notes`, which produces auto-notes that
  diverge from `CHANGELOG.md`.

## Tests

`python3 -m pytest tests -v` must stay green; behavior changes land
with tests. `ruff check .` and `ruff format --check .` must pass, and
pyright must stay at the `PYRIGHT_ERROR_BASELINE` hard-zero gate.
