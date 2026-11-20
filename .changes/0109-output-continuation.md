---
type: feat
scope: backends
user_visible: false
breaking: false
user_summary: "-"
dev_summary: the Gemini backend now auto-continues responses cut off at `finishReason` MAX_TOKENS by appending the partial output and a "continue" user turn, bounded by `generation.max_continuations` (default 8) and the accumulated `candidates_tokens` budget.
why: partner outputs were silently truncated at the output-token cap; the Gemini web UI documents multi-part continuation and the backend now does the same client-side instead of returning a clipped answer.
refs: "issue #109"
---
