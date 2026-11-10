<!--
Copyright (c) 2026 Alexandr Varenic / ychetka.
SPDX-License-Identifier: MIT
-->

# Partner usage and routing map

## Overview

`quant-partner` is a small MCP bridge that lets an AI assistant consult specialized LLM partners through a consistent tool interface. One Python entrypoint (`partners/mcp_partner.py`) runs every partner instance; each instance combines a backend adapter, a base harness contract and pluggable skill packs. Configuration, secrets and the error contract are documented in [README.md](../README.md); this document describes what each partner is for, which tools it exposes and which partner to route a question to.

Every partner is advisory, stateless and text-in/text-out. No partner writes code, merges pull requests, publishes comments or keeps memory between calls - the caller supplies all context on every call and decides what to do with the answer.

## How to call a partner

Each partner is a separate MCP stdio server process. The instance is selected by the `PARTNER_ID` environment variable and the config file by `PARTNER_CONFIG` (default `partners/config.json`); see [README.md](../README.md) for client registration JSON and the `QP_AUTH_*` / `QP_MODEL_*` environment overrides. Register one server entry per partner you want to use.

The tool surface depends on the partner's configured `toolset`:

- Common tools (all non-review toolsets): `partner_info()`, `list_partner_models()`, `list_partner_skills()`.
- Ask tools (all non-review toolsets, canonical names): `ask_partner(question, context?, files?, skill?, role?, model?)`, `ask_partner_v2(...)`, `ask_partner_dialog(messages, context?, skill?, role?, model?)`.
- `code` toolset (`gh-copilot-partner`, `local-openai-partner`): the canonical `ask_partner*` tools plus the legacy aliases `ask_code_partner`, `ask_code_partner_v2`, `ask_code_partner_dialog` with the same signatures.
- `product` toolset (`gemini-product`): the canonical `ask_partner*` tools plus the legacy aliases `ask_product_partner`, `ask_product_partner_v2`, `ask_product_partner_dialog` with the same signatures.
- `advisory` toolset (`pm-partner`, `market-partner`, `strategy-partner`, `comms-partner`, `org-partner`, `media-partner`, `text-quality-partner`): `ask_partner(...)`, `ask_partner_v2(...)`, `ask_partner_dialog(...)` only - no extra aliases - plus the optional grounding fields `decision_to_make`, `options_considered`, `constraints` and `context_pack` described below.
- Any other configured `toolset` value routes to the same generic surface (the canonical `ask_partner*` tools, no aliases) but without the grounding fields - those are gated strictly on `toolset` being `advisory`.
- `review` toolset (`code-reviewer`, `auto-reviewer`): `partner_info()`, `list_partner_skills()`, `list_reviewer_models()`, `review_code(head_sha, base_sha, diff, model, strict_model?, skill?, context?, files?, review_id?)` - the conversational `ask_*` tools are not exposed.

The `*_v2` ask variants take the same arguments and return a JSON object `{"partner_id", "skill", "model_requested", "model_used", "model_match", "text"}` instead of bare text, exposing model provenance.

Context placement:

- `context` on `ask_*` tools carries a caller-supplied context/context-pack brief alongside the question.
- `context` on `*_dialog` tools is added as a leading user turn containing a `## Context` brief, inserted before the supplied history - history message contents pass through verbatim; `model` roles are normalized to `assistant` before sending. Dialogs are stateless: pass the full `messages` history on every call.
- `files` attaches bounded file excerpts (`[{name, content}]`); the server never reads the filesystem.
- `skill` selects an installed skill pack per call; when omitted, the partner's configured `default_skill` is used. `model` overrides the default model and `role` selects a named model role from the partner's `models` config.

Advisory grounding fields (`advisory` toolset only - other toolsets do not advertise them):

- `decision_to_make` (string) - the decision this call should help the caller make; composed as a `## Decision to make` section.
- `options_considered` (array of strings) - options already on the table; composed as a bulleted `## Options considered` section.
- `constraints` (array of strings) - hard constraints the answer must respect; composed as a bulleted `## Constraints` section.
- `context_pack` (string) - the caller-managed project brief; composed as a `## Context pack` section kept separate from `## Context` so the ad-hoc context and the project brief stay distinguishable.
- All four are optional on `ask_partner`, `ask_partner_v2` and `ask_partner_dialog`. On the `ask_*` tools they compose into the user message in the stable order question, `## Context`, the grounding sections, then `## File:` excerpts; on `*_dialog` they join the same leading user context turn as `context`.
- Fail-soft rule: an `ask_*` call carrying none of `decision_to_make`, `options_considered`, `constraints`, `context`, `context_pack` or `files` is ungrounded, and the composed message ends with an instruction to respond with exactly 2-3 numbered clarifying questions and nothing else. Any one grounding field suppresses the instruction. Dialog calls are exempt - the supplied `messages` history is itself grounding.

Skill naming: installed skill names use mixed styles across partners (`product-manager` vs `market-synthesis` vs `communication`), so callers should call `list_partner_skills` on the target partner instead of guessing a name.

## gh-copilot-partner

Engineering, architecture and debugging consultations through direct GitHub Copilot API access (`copilot-direct` backend). Senior-architect persona: reasons about stack choices, module boundaries, refactoring and debugging strategy, and challenges weak designs instead of agreeing.

- Toolset: `code` - canonical `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools; legacy aliases `ask_code_partner`, `ask_code_partner_v2`, `ask_code_partner_dialog` stay callable with the same signatures.
- Default skill: `software-architect`.
- Skills root: `skills/gh-copilot-partner`.

| Skill | When to use |
| --- | --- |
| `software-architect` | Use for system design, service decomposition, data flow, backend/frontend boundaries and architecture review. |
| `polyglot-developer` | Use for implementation planning, dependency choice, code structure, refactoring steps and language-specific trade-offs. |
| `code-reviewer` | Use for reviewing diffs, architecture patches, protocol code, error handling, security-sensitive paths and test plans. |

Call it when:

- You need an architecture or stack trade-off analyzed (sync vs async, monolith vs split, dependency choice).
- You want a debugging strategy for a failure with symptoms, logs and a current hypothesis.
- You need a refactoring or migration plan sequenced into safe steps.
- You want a conversational second look at a design doc, API boundary or patch.
- You need to pick between implementation approaches with operational cost in mind.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "Should retry/backoff live in the shared HTTP helper or in each backend adapter?", "context": "Stdlib-only MCP bridge; three backend adapters behind core/server.py; per-partner timeout_seconds; no third-party dependencies allowed.", "skill": "software-architect"}}}
```

Boundary notes: advisory only - it proposes designs, the caller implements and verifies. Direct Copilot access is compatibility-sensitive; trust `list_partner_models()` over hardcoded model names. Never send secrets; the harness itself refuses to request credentials.

## gemini-product

Product management, UX and release consultations through the Gemini API. Product-manager persona: turns ambiguous ideas into a valuable, usable and measurable product, prefers narrow testable MVPs and makes uncertainty explicit.

- Toolset: `product` - canonical `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools; legacy aliases `ask_product_partner`, `ask_product_partner_v2`, `ask_product_partner_dialog` stay callable with the same signatures.
- Default skill: `product-manager`.
- Skills root: `skills/gemini-product`.

| Skill | When to use |
| --- | --- |
| `product-manager` | Use for product requirements, feature evaluation, MVP cuts, user journeys, roadmap and value hypothesis. |
| `prioritization` | Use for roadmap ordering, MVP cuts, trade-off decisions, RICE-style scoring and deciding what to defer. |
| `ux-architect` | Use for tool UX, interaction flows, naming, API ergonomics, error states, onboarding and operator experience. |
| `spec-compliance-verifier` | Use to check an implementation diff against spec text and acceptance criteria; returns a strict JSON advisory artifact, never a merge decision. |
| `release-manager` | Use to draft release notes or changelog entries from supplied change summaries and PR lists, classify the semver bump signal and flag missing changelog coverage; advisory drafts only. |

Call it when:

- You need a feature idea framed as user value, MVP scope and success metrics.
- You want a roadmap or backlog ordered under explicit trade-offs.
- You need release notes or a changelog draft from a PR/change list, with a semver signal.
- You want an advisory drift check between spec text, acceptance criteria and a diff summary.
- You need UX review of tool surfaces: flows, naming, error states, onboarding.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "Draft release notes from this merged-PR list and classify the semver bump signal.", "context": "Merged since v0.1.15: #31 env overrides for partner auth, #34 reviewer cache TTL fix, #36 routing docs.", "skill": "release-manager"}}}
```

Boundary notes: advisory only. `spec-compliance-verifier` returns a strict JSON advisory artifact - never a merge decision. `prioritization` pins its own model in the manifest, so per-skill model overrides are intentional.

## code-reviewer

Strict advisory second-opinion review of unified diffs through the `review` toolset. Unlike the conversational partners it returns exactly one schema-valid JSON artifact per call (`status` is `ok`, `rejected` or `error`), with model provenance enforced by `strict_model` and an allowlist of model aliases instead of free-form model IDs.

- Toolset: `review` - `partner_info`, `list_partner_skills`, `list_reviewer_models`, `review_code`. No `ask_*` tools and no `list_partner_models`.
- Default skill: `code-reviewer`.
- Skills root: `skills/code-reviewer`.
- The full contract - required arguments, `strict_model` provenance, artifact caching, repair retry and input limits - is documented in [README.md](../README.md) under "Advisory code review".

| Skill | When to use |
| --- | --- |
| `code-reviewer` | Used by the review_code tool for an independent, non-binding review of a pull-request diff. |
| `security-reviewer` | Used by the review_code tool via its optional skill argument for an independent, non-binding security review of a pull-request diff. |

Call it when:

- You have a unified diff in hand and want an independent second opinion before merge.
- You want a defensive security pass on a diff (injection, authn/authz gaps, secret handling, deserialization, path traversal, SSRF, crypto misuse).
- You need a stable, schema-valid artifact (`ok`/`rejected`/`error`) that downstream automation can consume, not prose.
- You need the result pinned to an allowlisted model with provider-confirmed provenance.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "review_code", "arguments": {"head_sha": "<40-hex PR head sha>", "base_sha": "<40-hex PR base sha>", "diff": "diff --git a/partners/core/http.py b/partners/core/http.py\n...", "model": "claude", "skill": "security-reviewer", "context": "Second-opinion security pass on the outbound request path."}}}
```

Boundary notes: `advisory` is always `true` - the artifact never authorizes a merge, never publishes reviews or comments, and every finding is anchored to a path and RIGHT-side line in the supplied diff. Oversized, unsafe or secret-like input is rejected before any provider access.

## auto-reviewer

Gemini-backed instance of the same strict advisory `review` toolset, purpose-built for automated pull-request review. It exists so CI auto-review keeps working when the `copilot-direct` backend behind `code-reviewer` hits its quota; it keeps a separate toolset, model alias and artifact cache from `code-reviewer`.

- Toolset: `review` - `partner_info`, `list_partner_skills`, `list_reviewer_models`, `review_code`. No `ask_*` tools and no `list_partner_models`.
- Default skill: `reviewer`.
- Skills root: `skills/auto-reviewer`.
- Backend: `gemini`; the single allowlisted model alias `gemini` pins `gemini-3.5-flash` (`match: exact`).
- The caller-facing runner `tools/pr_review.py` invokes `review_code` and folds the artifact into a GitHub PR-review payload; see [README.md](../README.md) under "Auto PR review".

| Skill | When to use |
| --- | --- |
| `reviewer` | Used by the auto-review CI runner (tools/pr_review.py) through the review_code tool for an independent, non-binding pull-request review. |

Call it when:

- You run the `auto_review` GitHub workflow or `tools/pr_review.py` and need the reviewed artifact behind `AUTO_REVIEW_VERDICT`.
- You want the same fail-closed artifact contract as `code-reviewer` served by the Gemini backend instead of Copilot.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "review_code", "arguments": {"head_sha": "<40-hex PR head sha>", "base_sha": "<40-hex PR base sha>", "diff": "diff --git a/tools/pr_review.py b/tools/pr_review.py\n...", "model": "gemini"}}}
```

Boundary notes: same advisory guarantees as `code-reviewer` - `advisory` is always `true`, findings anchor to RIGHT-side diff lines only, and oversized, unsafe or secret-like input is rejected before any provider access. The instance never posts to GitHub itself; publication is the workflow's job via `GITHUB_TOKEN`.

## pm-partner

Pragmatic B2B product-management advisory through the Gemini API. Turns ambiguous product ideas into a focused, testable and economically grounded plan: Value Proposition Canvas, MVP scoping, backlog prioritization and unit economics with explicit formulas.

- Toolset: `advisory` - `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools.
- Default skill: `product-manager`.
- Skills root: `skills/pm-partner`.

| Skill | When to use |
| --- | --- |
| `product-manager` | Use for product requirements, feature evaluation, MVP scope, RICE/ICE/MoSCoW prioritization and LTV/CAC/payback/churn/ROI reasoning. |

Call it when:

- You need a Value Proposition Canvas: customer jobs, pains and gains mapped to features.
- You want an MVP scope cut that validates the riskiest hypothesis first.
- You need backlog items scored or tiered with RICE, ICE or MoSCoW.
- You want unit-economics reasoning: LTV, CAC, LTV/CAC, payback period, churn, ROI - with formulas shown.
- You need missing inputs surfaced as explicit questions or labeled assumptions.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "Score these five backlog items with RICE and propose the MVP cut.", "context": "Items with caller-supplied reach/impact/confidence/effort estimates: ... Target segment: mid-market finance teams.", "skill": "product-manager"}}}
```

Boundary notes: works only on caller-supplied metrics, inputs and assumptions - it never invents numbers, benchmarks or market data, and asks or states a labeled assumption when an input is missing.

## market-partner

Market-analysis advisory through the Gemini API. Turns caller-supplied market signals into structured analysis: segmentation, personas, competitor matrices and TAM/SAM/SOM estimation. It helps the caller think clearly about a market; it does not research one.

- Toolset: `advisory` - `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools.
- Default skill: `market-synthesis`.
- Skills root: `skills/market-partner`.

| Skill | When to use |
| --- | --- |
| `market-synthesis` | Use for competitor matrices, market segmentation, personas, trend synthesis and market sizing built from signals the caller provides. |

Call it when:

- You have a competitor list and attributes and want a comparison matrix.
- You need market segmentation or personas built from observed signals.
- You want a TAM/SAM/SOM estimate from your own assumptions, with formulas.
- You need a trend synthesis or a structured read of signals you collected.
- You want the list of exactly which data to collect next to firm up the analysis.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "Build a competitor matrix from these signals and estimate TAM bottom-up.", "context": "Caller-supplied competitors: A (pricing, segment, differentiators), B (...); assumed target accounts: 1200; assumed ARPA: $400/mo.", "skill": "market-synthesis"}}}
```

Boundary notes - the no-live-data boundary is explicit in its contract: NO live data, NO web access, NO automatic competitor monitoring and no "Big Data" feed. Every output is synthesized only from signals supplied in `question`, `context` and `files`; TAM/SAM/SOM figures are estimations from caller assumptions and are labeled as such, never as observed numbers.

## strategy-partner

Strategy-consulting advisory through the Gemini API. Provides structured strategic thinking: named frameworks on request (SWOT, PESTEL, Porter's Five Forces, BCG matrix), growth-hypothesis generation and risk registers, with explicit assumptions and confidence labels.

- Toolset: `advisory` - `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools.
- Default skill: `strategy-consultant`.
- Skills root: `skills/strategy-partner`.

| Skill | When to use |
| --- | --- |
| `strategy-consultant` | Use for SWOT, PESTEL, Porter's Five Forces or BCG analysis, growth-hypothesis generation, strategic options and risk register drafting. |

Call it when:

- You want a named framework applied: SWOT, PESTEL, Porter's Five Forces or BCG.
- You need growth hypotheses stated as testable levers with confirming metrics.
- You want a risk register: risk, likelihood, impact, early indicator, mitigation, owner.
- You need strategic options compared with explicit assumptions and confidence labels.
- You want a strategy challenged for unexamined assumptions.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "Run a Porter's Five Forces pass on this market and draft a risk register for the expansion option.", "context": "Caller-supplied market inputs, current position and the two options under consideration: ...", "skill": "strategy-consultant"}}}
```

Boundary notes: frameworks and assumptions are reasoning aids, not verified market facts - there is no live data or research access and every output is built only from caller-supplied inputs, with confidence labels separating evidence from judgment.

## comms-partner

Communication and interface partner for long-running projects through the Gemini API. Drafts investor updates and stakeholder messaging, adapts tone deliberately between registers (investor-formal versus founder-supportive) and runs Socratic dialogs that surface what the caller actually knows and decides.

- Toolset: `advisory` - `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools.
- Default skill: `communication`.
- Skills root: `skills/comms-partner`.

| Skill | When to use |
| --- | --- |
| `communication` | Use for investor updates, stakeholder messaging, founder conversations, interview-style elicitation and anything that needs the project brief as memory. |
| `retro-review` | Use for a retrospective review of decision-ledger slices: marks each past recommendation validated, regretted or pending with reasoning and summarizes adoption patterns. |

Call it when:

- You need an investor update, stakeholder message or announcement drafted honestly.
- You want tone adapted to an audience: investor-formal (precise, metric-anchored) versus founder-supportive (direct, honest, no flattery).
- You want a Socratic dialog - one focused question at a time - to elicit a decision or articulate a position.
- You want contradictions between the current question and the project brief flagged.
- You need the closest thing to long-term project memory: pass the brief every call.
- You want a retro-review of past partner recommendations: pass a slice of the `decisions.jsonl` ledger (kept by `tools/decision_ledger.py`) in `context` or `files`.

Example (dialog with a context pack):

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner_dialog", "arguments": {"context": "<contents of the filled partners/context-pack/CONTEXT_PACK.md brief>", "messages": [{"role": "user", "text": "Help me prepare the monthly investor update."}], "skill": "communication"}}}
```

Context-pack protocol: the server is stateless and keeps no memory between calls. Persistent context works through the caller-managed context pack - fill [partners/context-pack/CONTEXT_PACK.md](../partners/context-pack/CONTEXT_PACK.md) once per venture and pass it in the `context` field of each call. The partner treats the supplied brief as the memory substitute and the single source of truth about the venture, and says so when no brief was provided instead of pretending continuity.

Boundary notes: drafts are honest and verifiable - no inflated claims, no invented metrics, no promises the brief does not support. Treat the brief as sensitive: it leaves the caller's machine to the configured provider.

## org-partner

Organizational design advisory for AI-native companies through the Gemini API. COO/business-architect persona: designs org structures, responsibility zones and span of control for companies built around AI digital employees (agents, session directors, partner fleets) with humans placed deliberately on the critical boundaries.

- Toolset: `advisory` - `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools.
- Default skill: `org-design`.
- Skills root: `skills/org-partner`.

| Skill | When to use |
| --- | --- |
| `org-design` | Use for org structures and org charts, zones of responsibility, span-of-control design for mixed human+agent teams, agents-as-roles modeling, critical-boundary placement of humans and escalation paths. |
| `operating-model` | Use for committee, board and council design, decision rituals and cadences, RACI matrices, gates and quorums, incident channels and escalation to the CEO. |
| `role-architecture` | Use for job descriptions for human and AI digital-employee roles, responsibility boundaries and hand-off contracts between roles with inputs, outputs and SLA-style expectations. |

Call it when:

- You need an org structure for a company mixing humans and AI digital employees: org chart, zones of responsibility, span of control.
- You want committees, boards or councils designed with mandate, cadence, quorum, RACI and gates.
- You need an incident channel with a severity ladder and escalation to the CEO.
- You need job descriptions for human and digital-employee roles with explicit responsibility boundaries.
- You want hand-off contracts between roles: inputs, outputs and SLA-style expectations.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "Design an org structure for a 3-human, 12-agent company and place humans on the critical boundaries.", "context": "Caller-supplied facts: humans cover CEO, sales lead and ops lead; the agent fleet runs support, research and drafting; external commitments and payouts must stay human-owned.", "skill": "org-design"}}}
```

Boundary notes: works only on caller-supplied org facts and constraints - no live benchmarks, no industry databases and no proprietary org data. Designs are advisory drafts; legal, labor, tax and HR implications need qualified human validation before adoption.

## local-openai-partner

Disabled-by-default partner that talks to any OpenAI-compatible HTTP endpoint through the `openai-compatible` backend - self-hosted models, local proxies or corporate gateways. It shares the `gh-copilot-partner` harness contract and skill packs, so it answers the same engineering persona once pointed at an endpoint.

- Toolset: `code` - canonical `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools; legacy aliases `ask_code_partner`, `ask_code_partner_v2`, `ask_code_partner_dialog` stay callable with the same signatures.
- Default skill: `software-architect`.
- Skills root: `skills/gh-copilot-partner` (shared with `gh-copilot-partner` via `skills_root`).
- Disabled in `partners/config.example.json`; enable it only in a local `partners/config.json`.

| Skill | When to use |
| --- | --- |
| `software-architect` | Use for system design, service decomposition, data flow, backend/frontend boundaries and architecture review. |
| `polyglot-developer` | Use for implementation planning, dependency choice, code structure, refactoring steps and language-specific trade-offs. |
| `code-reviewer` | Use for reviewing diffs, architecture patches, protocol code, error handling, security-sensitive paths and test plans. |

Call it when:

- You want the code-partner tool surface backed by an endpoint you control (Ollama, vLLM, a proxy or an internal gateway).
- You need a reference config for wiring a custom OpenAI-compatible deployment: `options.openai.base_url`, optional `auth.api_key`, `model` and `generation`.
- You want no-secret testing of the full pipeline against `tools/fake_openai_server.py`.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "Critique this module boundary proposal for the ingest pipeline.", "context": "Offline transcript ingest tool; stdlib-only; bounded local cache keyed by content hash.", "skill": "polyglot-developer"}}}
```

Boundary notes: `options.openai.base_url` can be any host, so enabling this partner sends prompts and file context to that endpoint - enable it only for endpoints you intend. Endpoint contract and the `model_used` echo caveat are documented in [README.md](../README.md); the bundled fake server provides deterministic loopback testing without secrets.

## Routing map

| Question archetype | Partner | Skill | Tool | Notes |
| --- | --- | --- | --- | --- |
| Architecture / stack trade-off | `gh-copilot-partner` | `software-architect` | `ask_partner` | System design, boundaries, decomposition |
| Implementation plan / dependency choice | `gh-copilot-partner` | `polyglot-developer` | `ask_partner` | Language and library trade-offs |
| Debugging strategy | `gh-copilot-partner` | `polyglot-developer` | `ask_partner` | Paste symptoms, logs and hypothesis in `question`/`context` |
| Conversational code review | `gh-copilot-partner` | `code-reviewer` | `ask_partner` | Prose review of a pasted patch or design |
| Second-opinion diff review (strict artifact) | `code-reviewer` | `code-reviewer` | `review_code` | Unified diff in hand; JSON artifact, `ok`/`rejected`/`error` |
| Security review of a diff | `code-reviewer` | `security-reviewer` | `review_code` | `skill` argument selects the pack |
| Product spec / MVP scope | `pm-partner` | `product-manager` | `ask_partner` | B2B framing with unit economics; `gemini-product`/`product-manager` as alternative |
| Backlog prioritization (RICE/ICE/MoSCoW) | `pm-partner` | `product-manager` | `ask_partner` | `gemini-product`/`prioritization` as alternative |
| Unit economics (LTV/CAC/payback/churn/ROI) | `pm-partner` | `product-manager` | `ask_partner` | Formulas shown; missing inputs become questions |
| PR/issue/release triage, release notes | `gemini-product` | `release-manager` | `ask_partner` | Changelog drafts and semver signal; advisory only |
| Spec compliance check (diff vs spec) | `gemini-product` | `spec-compliance-verifier` | `ask_partner` | Strict JSON advisory artifact, never a merge decision |
| UX / interaction / API ergonomics | `gemini-product` | `ux-architect` | `ask_partner` | Flows, naming, error states, onboarding |
| Roadmap ordering / scope cuts | `gemini-product` | `prioritization` | `ask_partner` | Pins its own manifest model |
| Market sizing / competitor matrix | `market-partner` | `market-synthesis` | `ask_partner` | Caller supplies signals; TAM/SAM/SOM labeled estimations |
| Segmentation / personas / trend synthesis | `market-partner` | `market-synthesis` | `ask_partner` | No live data or monitoring |
| Strategy frameworks (SWOT/PESTEL/Porter5/BCG) | `strategy-partner` | `strategy-consultant` | `ask_partner` | Named framework on request only |
| Growth hypotheses / risk register | `strategy-partner` | `strategy-consultant` | `ask_partner` | Assumptions carry confidence labels |
| Investor update / stakeholder communication | `comms-partner` | `communication` | `ask_partner` | Pass the context pack in `context` |
| Founder coaching / elicitation | `comms-partner` | `communication` | `ask_partner_dialog` | Socratic: one focused question at a time |
| Long-term project memory | `comms-partner` | `communication` | `ask_partner` / `ask_partner_dialog` | Caller-managed context pack every call |
| Retro-review of past partner recommendations | `comms-partner` | `retro-review` | `ask_partner` | Paste the `decisions.jsonl` slice in `context`; outcome marks are advisory |
| Org structure / span of control | `org-partner` | `org-design` | `ask_partner` | Mixed human+agent orgs; humans on critical boundaries |
| Committee / RACI / quorum design | `org-partner` | `operating-model` | `ask_partner` | Decision rituals, gates and incident escalation to the CEO |
| Role descriptions / hand-off contracts | `org-partner` | `role-architecture` | `ask_partner` | Human and digital-employee JDs; inputs/outputs/SLA |
| Video / meeting transcript Q&A | `media-partner` | `media-analysis` | `ask_partner` | Transcript via `context`/`files` from `tools/media_ingest.py` |
| Decision extraction / meeting summary | `media-partner` | `media-analysis` | `ask_partner` | [mm:ss] citations required; oversized transcripts are caller-chunked |
| Pre-publication draft gate / AI-detection triage | `text-quality-partner` | `text-quality` | `ask_partner` | Caller supplies the detector payload (vendor scores, flagged_spans, uniqueness) and thresholds |
| Flagged-span rework for RU/EN drafts | `text-quality-partner` | `text-quality` | `ask_partner` | Per-span rewrite instructions; verdict maps to caller thresholds |
| Self-hosted / OpenAI-compatible endpoint | `local-openai-partner` | `software-architect` | `ask_partner` | Disabled by default; enable only for endpoints you control |

Decision rules:

- A unified diff in hand and a strict, automation-consumable artifact - use the `review` toolset on `code-reviewer`; for a conversational read of a patch, `gh-copilot-partner` with the `code-reviewer` skill instead.
- A business question - pick the specialist by domain: `pm-partner` for product economics, `market-partner` for market synthesis, `strategy-partner` for frameworks and risk, `org-partner` for org structure, decision systems and role contracts, `gemini-product` for UX, release and spec-drift work.
- A conversation, a draft or anything that needs the venture brief - `comms-partner` with the context pack in `context`.
- An engineering question - `gh-copilot-partner` (or `local-openai-partner` when a self-hosted endpoint is configured).
- A question about what was said in a recording - `media-partner` over a transcript produced by `tools/media_ingest.py` (or a caller-supplied one) passed through `context` or `files`.
- A draft to clear a pre-publication gate on AI-detector and uniqueness readings - `text-quality-partner` over a caller-supplied detector payload; without a payload it can only give a slop-pattern read, not a gate verdict.
- When partners overlap - for example `pm-partner` versus `gemini-product` on MVP scope - prefer `pm-partner` for economics-heavy B2B framing and `gemini-product` for UX, release-note and spec-compliance work.

## media-partner

Transcript-analysis advisory through the Gemini API. Answers questions about recorded meetings, talks and discussions strictly from a caller-supplied transcript: transcript Q&A, decision extraction and meeting summarization with [mm:ss] timestamp citations.

- Toolset: `advisory` - `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools.
- Default skill: `media-analysis`.
- Skills root: `skills/media-partner`.
- Transcripts are produced offline by `tools/media_ingest.py` (not part of the MCP server): `from-file` runs local whisper.cpp ASR or normalizes a caller-supplied transcript file (plain text, SRT, VTT or loose JSON), `from-youtube` pulls platform captions through `yt-dlp` or downloads the audio for local ASR (`--asr`), and `from-web-page` downloads page media through `yt-dlp` for local ASR - all emit a schema-validated `transcript.json`, kept in a bounded cache keyed by content hash (0600 files, hours-scale TTL). Pass the transcript through `context` or `files` on ordinary calls - the partner stays text-in/text-out and never caches answers.

| Skill | When to use |
| --- | --- |
| `media-analysis` | Use for transcript Q&A, decision extraction and meeting summarization with [mm:ss] timestamp citations over a supplied transcript. |

Call it when:

- You have a transcript (from `tools/media_ingest.py` or your own source) and want Q&A over what participants said.
- You need the decisions and action items extracted from a meeting, separated from discussion and banter.
- You want a meeting summary organized by topic with timestamp citations.
- You want to locate a quote or a moment, cited as [mm:ss].

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "What was decided about the launch date, and who disagreed?", "context": "<transcript.json segments produced by tools/media_ingest.py>", "skill": "media-analysis"}}}
```

Boundary notes: analyzes transcripts only - it never watches video and never listens to audio, and says so. Speaker identity is reported as unknown when the transcript's speaker field is null; when present, `speaker` values such as `S0`/`S1` are per-file turn tags from whisper.cpp tinydiarize, never identity-stable across files. Every moment-bound claim needs a [mm:ss] citation; oversized transcripts are the caller's responsibility to chunk and nothing is silently truncated - for oversized transcripts and multi-file corpora, callers may use the offline companions `tools/transcript_chunker.py` (segment-aligned, citation-preserving chunks) and `tools/transcript_rag.py` (BM25 retrieval over chunk indexes) to supply bounded context. The transcript itself may carry automated-transcription errors and is treated as untrusted caller-supplied content.

Visual claims arrive only indirectly: the optional offline adapter `tools/visual_adapter.py` can enrich a transcript's `annotations[]` with timestamped visual observations (Gemini Files API, marked `source: "gemini-files-api"`). The partner reports them strictly with attribution - "per visual analysis" / "the visual adapter reports" - and never as its own perception; with an empty or absent `annotations[]` it has no visual information at all and says so.

Honest capability: transcript provenance is labeled, not equalized. A `platform_captions` trust flag means the text came from third-party platform captions fetched by `yt-dlp` - quality is unknown and can include auto-caption errors; `self_transcribed` means local whisper.cpp ASR ran on the caller's machine or a caller-supplied transcript was normalized. ASR is local-only: no hosted transcription service is ever invoked, so accuracy is bounded by the local whisper.cpp model the caller provides, and the partner can only analyze whatever text actually arrived.

## text-quality-partner

Pre-publication draft gate advisory for Russian- and English-language text through the Gemini API. Triage persona: reads the caller-supplied detector payload - vendor AI-detection scores, flagged spans, uniqueness percent and gate thresholds - alongside the draft text, then returns a pass, borderline or fail verdict with concrete rework instructions for each flagged span.

- Toolset: `advisory` - `ask_partner`, `ask_partner_v2`, `ask_partner_dialog` plus the common tools.
- Default skill: `text-quality`.
- Skills root: `skills/text-quality-partner`.
- Detector payload contract (supplied via `question`, `context` or `context_pack`): `language` (`ru`/`en`), `detector_results` (per-vendor `ai_score` 0-100 plus optional `flagged_spans`), `uniqueness` (vendor, `uniqueness_percent`, optional `matched_urls`), `thresholds` (defaults `ai_score` below 20, `uniqueness_percent` at or above 85) and the draft text itself.

| Skill | When to use |
| --- | --- |
| `text-quality` | Use for pre-publication draft gate triage and per-flagged-span rework guidance over a caller-supplied detector payload in RU or EN drafts. |

Call it when:

- You have AI-detector readings (pangram, gptzero or other vendors) and a uniqueness reading for a draft and want a gate verdict against your thresholds.
- You need concrete rework instructions for each flagged span - named slop patterns (hedging stacks, uniform rhythm, generic transitions, passive chains, template openers), not "make it sound human".
- Vendor readings disagree and you want the conflict weighed instead of averaged away.
- You want a RU/EN-aware read of machine-flavored writing in a draft.

Example:

```json
{"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "ask_partner", "arguments": {"question": "Gate this draft and give rework steps for the flagged spans.", "context": "language: en; detector_results: [{vendor: pangram, ai_score: 34, flagged_spans: [...]}, {vendor: gptzero, ai_score: 18}]; uniqueness: {vendor: content-watch, uniqueness_percent: 92}; thresholds: ai_score < 20, uniqueness >= 85; draft: <draft text>", "skill": "text-quality"}}}
```

Boundary notes: the partner never computes AI-likelihood or uniqueness scores itself - every score is a caller-supplied third-party reading treated as evidence, not ground truth. Verdicts are advisory and map to the caller's thresholds; the publish or hold decision belongs to the caller's gate and no publication, moderation or platform outcome is ever guaranteed. With no detector payload supplied it names the missing evidence and can offer only a slop-pattern read, clearly labeled as unverifiable against the gate.

## Capability boundaries

- Advisory only: no partner merges, publishes, executes or authorizes anything. The caller decides what reaches code or stakeholders.
- Stateless: no server-side memory between calls; pass `question`, `context`, `files` and full `messages` history every time.
- No web access and no live data: partners reason only over caller-supplied signals. There is no automatic competitor or metric monitoring behind any of them.
- Long-term memory is caller-managed: context packs (template at [partners/context-pack/CONTEXT_PACK.md](../partners/context-pack/CONTEXT_PACK.md)) are the memory substitute.
- No secrets in calls: never send tokens, credentials or personal data in questions, context or files. `partners/config.json` holds secrets locally and is gitignored.
- Supplied material is treated as untrusted content; briefs, diffs and transcripts may be sensitive - they are forwarded to the configured provider backend. For `local-openai-partner`, that means whatever `options.openai.base_url` points at.
- Estimates such as TAM/SAM/SOM are labeled estimations from caller assumptions, not observed data.
