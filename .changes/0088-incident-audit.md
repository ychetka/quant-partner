# type: fix | feat | refactor | chore | docs
type: fix
scope: tools
user_visible: false
breaking: false
user_summary: "-"
dev_summary: Incident audit of unreviewed rollouts (PRs #86/#87): hardened commit-wrapper gates (forged-sentinel smuggling, consume-once migration stamp, HEAD-bound roll-forward notes, NUL-safe file lists) and restored the displaced two-committee spec as controller-manager/development-cycle.md with honest harness provenance.
why: Issue #88 - PRs #86 and #87 merged without independent review. Audit found a real hygiene bypass (a multi-line commit message could forge the @@SUBJECTS_END@@ sentinel and smuggle unscanned content under a fake exempt path), a reusable migration stamp, agent-writable roll-forward waivers, and a false provenance claim in controller-manager/harness.md that left .windsurfrules references dangling.
refs: "issue #88"
---

## Spec impact
- controller-manager/harness.md keeps the director session model verbatim
  with a corrected provenance header.
- controller-manager/development-cycle.md restores the canonical
  two-committee spec (verbatim adoption of the ai_controller copy).
- .windsurfrules references repointed; .devin/project.json gains
  cyrillic_paths entries for .windsurfrules and controller-manager/.
- Gate scripts: message/diff hygiene checked separately; migration stamp
  consumed on commit; roll-forward note must be tracked in HEAD and name
  the migration; paired rollback must be non-empty; NUL-safe staged-file
  parsing; project.json path passed to Python via env.
