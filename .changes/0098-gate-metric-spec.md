---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: adds design spec `.devin/spec_text_gate_metric.md` for the local text-quality gate metric (partner-logprob perplexity tier with caching, multi-register corpus packs, veto + logistic composite, Pangram validation protocol, honest-capability boundaries).
why: the Shared Person internal gate measured the wrong thing (44 'Possibly human' on a Pangram-100% draft); the replacement needs a written spec before implementation, and developer decisions moved the perplexity tier to partner logprobs and made corpora multi-register.
refs: "issue #98"
---
