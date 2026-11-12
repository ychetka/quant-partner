---
type: chore
scope: tools
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Sync canonical repo_merge.sh (v0.15.5): pre-merge version_not_bumped
  gate blocks shipping-path merges without a [project].version bump.
why: >-
  Developer ruling 2026-11-12 (no_version_change incident): every
  merged PR must bump the version unless exempt (non-shipping diff or
  no-release label). Ref devin-session-bridge#162.
---
