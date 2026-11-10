---
type: feat
scope: partners
user_visible: true
breaking: false
user_summary: Auto-review now reports provider token usage and an estimated per-review cost in the posted review body, the job summary and per-run usage.json workflow artifacts.
dev_summary: Plumbs Gemini usageMetadata through CompletionResult.usage into the advisory artifact's new optional usage object (summed across the repair retry), and adds usage/cost reporting to tools/pr_review.py plus a usage summary step and usage.json artifact upload in the auto_review workflow.
why: The auto-review layer needed observable spend without a state backend; only provider-reported counters are used, so artifacts without usage stay schema-valid and costs remain estimates checked against AI Studio quota.
refs: "issue #74"
---
