---
type: feat
scope: partners
user_visible: false
breaking: false
user_summary: "-"
dev_summary: Gemini backend now sends generationConfig.thinkingConfig.thinkingLevel, defaulting to "high" and overridable via the partner generation block.
why: Partners always run thinking=high per directive; previously no thinkingConfig was sent at all so calls ran at the provider dynamic default.
refs: "issue #103"
---
