---
type: fix
scope: partners
user_visible: false
breaking: false
user_summary: "-"
dev_summary: raises `architect-partner` `generation.max_tokens` 8192 -> 81920 and trims `models.fallback` to `gemini-4-argon` only.
why: architecture consultations at thinking_level=high produce long structured outputs that were truncated at 8192 tokens; the developer wants argon-only fallback for this partner.
refs: "issue #107"
---
