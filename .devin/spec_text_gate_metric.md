# Spec: local text-quality proxy metric (gate replacement for shared_person)

Status: DESIGN SPEC v2 (task 306 + developer decisions 2026-11-12) -
for the Shared Person session to implement; nothing here runs in
quant-partner. Basis: `.devin/research_pangram_humanizer_plan.md` +
partner consult (gh-copilot-partner/software-architect,
claude-sonnet-5). Trigger: the old internal gate scored a draft 44
"Possibly human" where Pangram returned AI Generated 100% - it
measured the wrong thing and saturated at the wrong end.

v2 changes per developer directive: (a) perplexity tier is now
PRIMARY = ready-made models via Quant Partner backends (Gemini
logprobs / Copilot models); local KenLM/Qwen-GGUF is fallback only;
(b) reference corpus = developer's own texts (style_corpus) + Habr
scrape, and the architecture must support MULTIPLE
corpora/registers (algo-trading register coming via Trade AI
Analitic).

## 0. Scope and honest framing

This metric is a PRE-GATE PROXY, not a detector and not a Pangram
replacement. It exists to catch obviously-AI drafts cheaply before
the (paid, external) Pangram call and to give text-quality-partner
rework targets. What it MUST NOT promise:

- "green = human". A passing score means "below our measured AI
  suspicion threshold", nothing more. Publication gate stays:
  Pangram score + human review.
- Pangram-score equivalence. It correlates with detector behavior
  at best; it shares no internals with Pangram and can diverge in
  both directions. Borderline results are advisory, never a
  verdict.
- Cross-vendor validity. Tuning it to match Pangram too closely
  re-creates single-vendor overfit one level down (transfer-check
  rule from the humanizer plan still applies).

## 1. Signals

Four orthogonal feature families; each reports a normalized feature
value plus a `degraded` flag when it cannot be computed honestly.

### S1. Perplexity / log-likelihood profile

PRIMARY path = partner logprob calls (developer decision
2026-11-12). Ready-made models beat self-hosted small LMs on RU
quality, and the partner layer already exists.

- **Tier A (primary): Gemini `logprobs`/`topLogprobs` via the
  gemini-product backend** (generateContent response logprobs;
  alternative: Copilot models through the copilot backend if its
  API exposes logprobs - capability check is an implementation
  precondition). Feed the doc, request token logprobs, reduce to
  per-sentence and per-doc mean log-likelihood.
- **Result caching is mandatory**: cache by content-hash of the
  input doc (same convention as the media-partner transcript
  artifacts) so repeat scoring of unchanged text is free and
  re-runs are idempotent. Keyed per {model, register-corpus
  version} - a stale cache entry after a model or corpus change
  must not leak.
- **Cost accounting hook**: every logprob call emits a
  usage-record event in the issue-#74 style (tokens scored, model,
  cache-hit flag) - this signal now spends partner quota, so the
  billing trail is non-optional.
- **Timeouts + bounded retries** per repo invariants (explicit
  timeout, backoff, max attempts, no retry on 4xx); a partner
  outage degrades to the fallback tier, it does not stall the
  gate.
- **Fallback tier (budget/efficiency overflow or partner down)**:
  KenLM 5-gram trained on the ACTIVE register corpus (see 1.5),
  then optionally Qwen2.5-0.5B GGUF via llama.cpp for the
  ambiguous band. Local tier is explicitly second-class: when it
  runs, output carries `confidence=low` and `lm_tier="local"`.
- Rejected for primary: mGPT-1.3B / ruGPT-3-small (~3-8s/doc CPU,
  heavy RAM), Llama-3.2-1B (weaker RU tokenizer fertility). They
  remain acceptable fallback candidates only if Qwen2.5
  underperforms empirically.
- Feature: `loglik_z` = z-score of doc mean log-likelihood
  against the reference-corpus distribution OF THE SAME REGISTER.
  If neither partner nor local tier is available: `degraded=true`,
  feature excluded, `confidence=low`.

### 1.5. Multi-register corpora (developer decision)

- Corpus `style_corpus` = the developer's own texts, continuously
  expanding, PLUS a Habr scrape in the conversational-technical
  blog register - that register is the primary target. A second
  register (algo-trading, project Trade AI Analitic) is expected;
  the design must not assume one corpus.
- Architecture: `corpus` is a first-class config entity. Each
  registered corpus ships its own: reference loglik distribution
  (mean/std for `loglik_z`), sentence-length distribution
  baseline, KenLM fallback model, and optionally a register-tuned
  connective weight table. New register = drop in a corpus pack,
  no code change.
- Register assignment: doc declares its register explicitly
  (front-matter/flag). Auto-detect heuristic is OPTIONAL: argmin
  of per-corpus KenLM perplexity; when heuristics decide, the
  report carries `register_source="auto"` for audit.
- KenLM corpus-quality note: building from the developer's own
  texts + Habr directly fixes the register-mismatch risk flagged
  in the consult (generic wiki corpus penalizes legitimate
  technical jargon). Keep corpus packs versioned so `loglik_z`
  stays comparable over time.

### S2. Burstiness

- Variance (and IQR) of per-sentence log-likelihood from whichever
  tier produced S1. Human text spikes; LLM text is smooth.
- Feature: `burst_var`, normalized against the same register
  corpus.

### S3. Structural monotony (pure stats, no LM - always runs)

- Sentence-length distribution: mean, stdev, skewness; share of
  sentences inside the 14-22-word "LLM band".
- Paragraph-shape regularity: how many paragraphs match the
  topic->mechanism->example->mini-summary 4-beat arc.
- Connective frequency: hits per 1000 words on the AI-connective
  list (RU examples, romanized: "Bolee togo", "Krome togo",
  "Tem ne menee", "Vazhno ponimat", "Takim obrazom",
  "Vo-pervyh/vo-vtoryh", ...).
- Clause-parallelism heuristic: repeated opening constructions,
  symmetric enumeration patterns.
- Calqued-syntax counters (RU-specific, from the humanizer plan):
  "yavlyaetsya/predstavlyaet soboy" copulas, inanimate "svoy"
  possessives, -sya passive stacks, genitive chains >=4.
- Feature: `mono_score` - weighted mix, tuned on the labeled set
  per register where material.

### S4. Lexical AI-dictionary (always runs, near-zero cost)

- Extend the existing banned-phrase dictionary from the humanizer
  playbook priorities (template openers/closers, throat-clearing,
  hedging stacks, encyclopedic framings). Maintain as versioned
  wordlist file; hits carry the matched span for explainability.
- Feature: `lex_hits_per_1k`.

## 2. Composite score

Shape: **rare high-precision vetoes + logistic regression** - not a
plain weighted sum (linear sums go confidently wrong when one
feature is garbage) and not veto-driven scoring (step-function
brittleness at the boundary).

1. Veto rules fire first. Rare by design; each logs `veto_reason`.
   Example candidate: `lex_hits_per_1k > EXTREME` (calibrated on
   the labeled set, not guessed) -> `p_ai` ceiling + forced review.
2. Logistic regression over {loglik_z, burst_var, mono_score,
   lex_hits_per_1k} trained on the labeled batch. Probabilistic
   [0,1] output, inspectable coefficients, and - key failure-mode
   fix vs the old gate - saturation at both ends is learned from
   data, so an obviously-AI draft cannot land in a soft "possibly
   human" middle.
3. Fit is per-register where enough labels exist; otherwise a
   shared model with register as a categorical input - do NOT
   silently pool registers that behave differently.

Output contract (per doc):

```
{ "p_ai": float 0-1,
  "veto_triggered": bool, "veto_reason": str|null,
  "features": {loglik_z, burst_var, mono_score, lex_hits_per_1k},
  "register": str, "register_source": "declared"|"auto",
  "lm_tier": "partner"|"local"|"none",
  "confidence": "high"|"low" }   # low when a tier is degraded
```

Gate bands (initial, recalibrate after validation): `p_ai < 0.35`
pass-pre-gate; `0.35-0.7` -> rework loop with text-quality-partner;
`> 0.7` -> obvious-AI, skip the Pangram call, send to rework or
human. Bands are thresholds for ACTION, not truth claims.

## 3. Validation protocol vs Pangram

Offline only - Pangram is the validation oracle, never a runtime
dependency.

1. **Labeled batch**: >=60-80 docs, balanced: AI-generated RU
   drafts from the pipeline vs known-human RU technical articles
   (code-stripped by the SAME strip path - strip artifacts must
   not confound the comparison). Plus the human control set from
   the humanizer plan's baseline step. Label the register per doc.
2. Score every doc with the composite AND Pangram; compute
   **Spearman rho** between `p_ai` and Pangram's AI probability.
   At n=60-80 the 95% CI is roughly +-0.15-0.2 - this is a
   go/no-go screen, not a precise calibration; the eval set must
   GROW over time and rho re-checked periodically.
3. **Acceptance threshold: rho >= 0.6** to adopt as a pre-gate.
   Below that, diagnose which feature family correlates worst
   instead of adding features blindly. If Pangram's own RU
   accuracy is unverified, treat rho as "agreement with another
   proxy", keep manual spot-checks in calibration, and prefer
   rho >= 0.7 or a second detector as co-oracle before trusting
   it operationally.
4. Failure audit: the old gate's defect was floor-saturation
   (44 vs 100%). The labeled set MUST include several
   Pangram-100% docs and the metric must score them at the high
   end - this specific regression is a release blocker for the
   new metric.
5. Cache hygiene in validation: bypass the S1 cache or include
   cache-miss forcing - measured rho must reflect real scoring,
   not cache replays.
6. Operational safety: document FP/FN cost asymmetry - a human
   flagged as AI wastes an edit pass; AI passed as human wastes
   a Pangram call. Veto aggressiveness follows that ratio, not
   a default guess.

## 4. Implementation order (for Shared Person)

1. Verify the primary-path precondition: gemini-product backend
   exposes logprobs (else Copilot backend; else fall to local
   tier as interim primary and report the gap).
2. S3/S4 feature extractors (no model dependency, immediately
   sanity-checkable on known samples).
3. Corpus pack infrastructure + first pack: style_corpus +
   Habr scrape, versioned, with reference stats computed.
4. S1 partner-tier scorer with content-hash cache + cost-hook +
   timeout/retry contract.
5. KenLM fallback per corpus pack (second-class, flags
   confidence=low); Qwen2.5-0.5B GGUF only if a gap measurement
   says local ambiguity-band scoring is needed offline.
6. Assemble the labeled batch (reuse Pangram-scored drafts +
   human Habr controls).
7. Feature vectors -> logistic fit -> rho vs Pangram.
8. rho >= 0.6: ship as pre-gate with confidence flags + bands;
   else: feature-level diagnosis, iterate.

## 5. Honest-boundary clauses to ship WITH the metric

- Every report carries `confidence`, `lm_tier`, and the
  degraded-feature list.
- Pipeline docs state: "pre-gate proxy, not a detector; Pangram +
  human remain the publication gate."
- No public-facing claim of AI-detection accuracy - it is an
  internal heuristic calibrated against one vendor on one corpus.
- The partner logprob path shares infrastructure with generation
  calls but is a scoring pass, not drafting - keep its logs
  separate so cost attribution stays clean.

## Consult log

- 2026-11-12 gh-copilot-partner/software-architect (claude-sonnet-5):
  two-tier LM design, logistic-over-linear composite, n>=60-80 and
  rho>=0.6 validation, KenLM corpus-register warning, CI caveat on
  rho at small n. v2 applied developer override making the partner
  logprob path primary (was the local tier in v1).
