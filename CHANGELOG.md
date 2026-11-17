# Changelog

Generated from `.changes/` per-PR entries at release time by
`tools/release_notes.sh --version X.Y.Z --apply`. Entries before
v0.1.33 predate the convention; see `git tag`/GitHub Releases for
the earlier history.

## [0.2.3] - 2026-11-17

### User-facing

_No user-visible changes._

### Codebase

#### ci
- [chore] Coverage workflow gains a ratchet - a hard "Enforce coverage floor" step reads coverage.json and fails the job below COVERAGE_TOTAL_FLOOR (85), COVERAGE_HTTP_FLOOR (53) or COVERAGE_COPILOT_FLOOR (60), with a notice when the total runs more than 2 points above the floor. Why: Issue #93 - the coverage job was fully report-only (every step continue-on-error), so regressions could merge silently. A fail-closed ratchet step with per-file floors for the two coverage-debt modules keeps the signal without blocking on the known debt. (issue #93)

#### docs
- [docs] adds design spec `.devin/spec_text_gate_metric.md` for the local text-quality gate metric (partner-logprob perplexity tier with caching, multi-register corpus packs, veto + logistic composite, Pangram validation protocol, honest-capability boundaries). Why: the Shared Person internal gate measured the wrong thing (44 'Possibly human' on a Pangram-100% draft); the replacement needs a written spec before implementation, and developer decisions moved the perplexity tier to partner logprobs and made corpora multi-register. (issue #98)

#### partners
- [feat] adds a `software-architect` skill pack to `gemini-product` pinned to model `gemini-4-carbon` (fallback `gemini-4-argon`), with README updated to six packs. Why: developer wants the new Gemini 4 Carbon model for detailed software architecture consultations; a per-skill manifest model pin scopes the new model to this pack without changing gemini-product's default. (issue #101)
- [feat] Gemini backend now sends generationConfig.thinkingConfig.thinkingLevel, defaulting to "high" and overridable via the partner generation block. Why: Partners always run thinking=high per directive; previously no thinkingConfig was sent at all so calls ran at the provider dynamic default. (issue #103)
- [feat] adds an `architect-partner` advisory partner (harness, `software-architect` and `architecture-auditor` skill packs, config entries and PARTNERS.md section) pinned to `gemini-4-carbon` with `thinking_level` high. Why: developer wants a dedicated system-architecture partner on Gemini 4 Carbon with thinking high, analogous to gemini-product but with the advisory toolset and its own skills root. (issue #105)

#### tools
- [fix] Incident audit of unreviewed rollouts (PRs #86/#87): hardened commit-wrapper gates (forged-sentinel smuggling in both ship_commit and --diff-base paths, consume-once migration stamp, HEAD-bound roll-forward notes, NUL-safe file lists) and restored the displaced two-committee spec as controller-manager/development-cycle.md with honest harness provenance. Why: Issue #88 - PRs #86 and #87 merged without independent review. Audit found a real hygiene bypass (a multi-line commit message could forge the @@SUBJECTS_END@@ sentinel and smuggle unscanned content under a fake exempt path), a reusable migration stamp, agent-writable roll-forward waivers, and a false provenance claim in controller-manager/harness.md that left .windsurfrules references dangling. (issue #88)
- [chore] Adopted canonical fleet quality-gate set v2 byte-for-byte from ai_controller (12 files md5-verified): hardened gate scripts now sharing migration_pattern.sh, the rebuilt agent profiles and hazards docs, plus controller-manager/harness.md and its development-cycle.md mirror. Why: Issue #90 - post-incident unification. The canonical set merges the five independent incident-audit fixes (issue #88) into the strictest implementation with 53 regression tests; local variants from PR #89 are intentionally replaced so the fleet converges on one canonical source. (issue #90)
- [chore] Add minimal pyproject.toml ([project].version = "0.2.2") as the canonical version source consumed by repo_release.sh / repo_merge.sh, and update .changes/README.md to the version-on-branch bump model. Why: The canonical release scripts derive the release tag from [project].version on the base head and fail closed when pyproject.toml is absent - this repo had none, which broke the release path structurally (orchestrator task 302). (issue #95, orchestrator task 302)
- [chore] Canonical fleet quality gates rollout - hazards docs, writer/reviewer/judge profiles, and commit-wrapper gates with negative tests. Why: Shared language-neutral reliability rules and mechanical enforcement (precommit re-grounding, whitelist scope, migration gate) replace drifting per-repo prompt lists; canonical source lives in ai_controller templates. (issue #84)
- [chore] >- Sync canonical repo_merge.sh (v0.15.5): pre-merge version_not_bumped gate blocks shipping-path merges without a [project].version bump. Why: >- Developer ruling 2026-11-12 (no_version_change incident): every merged PR must bump the version unless exempt (non-shipping diff or no-release label). Ref devin-session-bridge#162.

## [0.2.0] - 2026-11-11

### User-facing

#### feat
- Worker sessions now follow the director session model: short event-driven turns, one background executor, mandatory checkpoints. `docs` (issue #85, task 262)

### Codebase

#### docs
- [feat] Adopted canonical director session model section into controller-manager/harness.md; mid-turn inject proven impossible (canonical devin-session-bridge issue #137). Why: Canonical rollout of the director session model (task 262). A live ACP probe proved session/prompt is rejected mid-turn, so the event-driven model is the only viable variant. (issue #85, task 262)

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
