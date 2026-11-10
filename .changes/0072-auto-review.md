---
type: feat
scope: partners
user_visible: true
breaking: false
user_summary: Pull requests now get an automated advisory review posted by github-actions[bot] with an AUTO_REVIEW_VERDICT header (requires the GEMINI_API_KEY repository secret).
dev_summary: Adds the gemini-backed auto-reviewer partner (review toolset, skills/auto-reviewer/reviewer pack), tools/pr_review.py building the GitHub review payload, the auto_review workflow and docs.
why: The code-reviewer partner dies with the Copilot quota, so a dedicated Gemini instance keeps automated review working; publishing as an advisory COMMENT review preserves merge authority while giving inline findings. See ADR-0002.
refs: "issue #72, ADR-0002"
---
