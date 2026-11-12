---
type: chore
scope: tools
user_visible: false
breaking: false
user_summary: "-"
dev_summary: Add minimal pyproject.toml ([project].version = "0.2.2")
  as the canonical version source consumed by repo_release.sh /
  repo_merge.sh, and update .changes/README.md to the
  version-on-branch bump model.
why: The canonical release scripts derive the release tag from
  [project].version on the base head and fail closed when
  pyproject.toml is absent - this repo had none, which broke the
  release path structurally (orchestrator task 302).
refs: "issue #95, orchestrator task 302"
---
