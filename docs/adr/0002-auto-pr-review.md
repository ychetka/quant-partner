# ADR-0002: Automated PR review via a dedicated Gemini review partner

- Status: accepted
- Date: 2026-11-10
- Refs: issue #72, roadmap `.devin/roadmap_auto_review.md`

## Context

Pull requests need a consistent automated review pass, but the existing
`code-reviewer` partner runs on the `copilot-direct` backend and dies
with the Copilot quota - exactly when review coverage is most needed.
The published result also had to stay advisory: a bot `REQUEST_CHANGES`
event would visually block merges and can count in branch-protection
setups, while a bot `APPROVE` could be mistaken for a human sign-off.

## Decision

Ship an `auto-reviewer` partner instance on the `gemini` backend with
its own `review` toolset, `skills/auto-reviewer/reviewer` pack,
artifact cache and allowlisted `gemini` model alias pinning
`gemini-3.5-flash`. It consumes the unchanged `review_code` pipeline
(`partners/review/`); nothing about review logic is reimplemented.

`tools/pr_review.py` is the CI-facing runner: it feeds a unified diff to
`review_code`, folds the returned artifact into a GitHub review payload
(`AUTO_REVIEW_VERDICT:` body header, inline comments for diff-anchored
findings, everything else relocated into the body) and soft-fails by
default so a partner outage still yields a publishable advisory note.

`.github/workflows/auto_review.yml` publishes that payload as
`github-actions[bot]` with the built-in `GITHUB_TOKEN`, always with
event `COMMENT`; the workflow is per-PR cancelled, is not a required
check and soft-fails every fallible step.

## Consequences

- Auto-review survives Copilot quota exhaustion and is billed to a
  cheap Gemini model instead.
- The layer is strictly advisory: `COMMENT` plus a verdict header keeps
  merge authority with humans while preserving inline-comment UX.
- Publication needs no external secret beyond `GITHUB_TOKEN`; the only
  added repository secret is `GEMINI_API_KEY`, and its absence degrades
  to a skip notice rather than a failed check.
- New invariant: the runner may never print diff or secret material to
  stderr, and every partner failure still emits a valid `review.json`.

## Alternatives considered

- Reuse `code-reviewer` (copilot-direct): rejected - bound to the quota
  this layer exists to escape.
- A `reviewer` skill under an existing Gemini partner
  (`gemini-product`): rejected - it would share the toolset, quota
  accounting and default model of a product-consultation instance; a
  dedicated instance keeps blast radius and cost separate.
- Publishing `APPROVE`/`REQUEST_CHANGES` events: rejected for v1 - a
  bot `REQUEST_CHANGES` reads as blocking and may interact with branch
  protection; `COMMENT` carries the same information non-blockingly.
- Publishing under an external reviewer PAT: rejected - distributing a
  PAT as a secret for cosmetics is worse than the native
  `github-actions[bot]` identity.
