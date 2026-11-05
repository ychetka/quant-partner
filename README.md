<!--
Copyright (c) 2026 Alexandr Varenic / ychetka.
SPDX-License-Identifier: MIT
-->

# quant-partner

`quant-partner` is a small MCP bridge that lets an AI assistant consult specialized LLM partners through a consistent tool interface.

One Python entrypoint runs multiple partner instances. Each instance has its own backend, base harness and pluggable skills:

- `gh-copilot-partner` - code, architecture, stack, review and debugging consultations through direct GitHub Copilot API access.
- `gemini-product` - product management, UX architecture, prioritization, release-note drafting and advisory spec compliance verification through the Gemini API.
- `code-reviewer` - advisory second-opinion review of unified diffs with strict model provenance.
- `pm-partner` - B2B product management advisory: Value Proposition Canvas, MVP scoping, backlog prioritization and unit economics (`advisory` toolset, Gemini API).
- `market-partner` - market analysis advisory: competitor matrices, segmentation and TAM/SAM/SOM estimation from caller-supplied signals (`advisory` toolset, Gemini API).
- `strategy-partner` - strategy consulting advisory: named frameworks (SWOT/PESTEL/Porter's Five Forces/BCG), growth hypotheses and risk registers (`advisory` toolset, Gemini API).
- `comms-partner` - communication advisory for long-running projects: adaptive tone, Socratic dialog and caller-managed context packs (`advisory` toolset, Gemini API).
- `openai-compatible` - optional generic backend for any OpenAI-compatible endpoint.

Further docs: [docs/PARTNERS.md](docs/PARTNERS.md) - per-partner usage, tool surfaces, examples and the orchestration routing map.

## MVP architecture

```text
AI assistant
  > MCP stdio server instance
  > partners/config.json partner section
  > base harness + selected skill prompt
  > backend adapter
  > provider REST API
```

The same `partners/mcp_partner.py` process implements both partners. The instance is selected by `PARTNER_ID`; the config path is selected by `PARTNER_CONFIG`.

## Layout

```text
partners/
  mcp_partner.py                  MCP stdio entrypoint
  config.example.json             public config template
  config.json                     local config with secrets, not committed
  core/                           config, prompting, errors, JSON-RPC, tool dispatch
  backends/                       copilot-direct, gemini, openai-compatible
  review/                         advisory review service, artifact schema, cache, provenance
  harness/                        base partner contracts
  skills/<partner>/<skill>/       manifest.json + prompt.txt
  context-pack/                   caller-managed startup/project brief template
tools/
  mcp_smoke.py                    local JSON-RPC smoke client
  fake_openai_server.py           deterministic loopback OpenAI-compatible endpoint
tests/                            stdlib unit tests
```

## Configuration

Copy the example and fill local secrets:

```bash
cp partners/config.example.json partners/config.json
```

Edit `partners/config.json`. Secrets are intentionally stored in the local JSON config for this product. The file is ignored by git; use placeholders in `config.example.json` only.

For `gh-copilot-partner`, `auth.github_oauth_token` must be an OAuth user token accepted by the GitHub Copilot auth flow (`gho_` or `ghu_` in typical flows). A generic GitHub token may not be sufficient: access depends on the OAuth app, token scopes and Copilot plan. The server exchanges it for a short-lived Copilot session token, discovers `api_base`, then calls `/models` and the supported completion endpoint.

For `gemini-product`, `auth.gemini_api_key` is a Google AI Studio API key.

### Environment overrides

Auth secrets and the primary model can also be supplied through partner-scoped environment variables. Variable names use two unambiguous families; the partner id and the auth key are normalized to uppercase with every non-alphanumeric character replaced by `_`:

- `QP_AUTH_<PARTNER_ID>_<AUTH_KEY>` - provides or overrides `partners.<partner_id>.auth.<auth_key>` (for example `github_oauth_token` becomes `GITHUB_OAUTH_TOKEN`). The known keys `github_oauth_token`, `gemini_api_key` and `api_key` are read even when absent from the file auth object; any other key already declared in the file auth object can be overridden too.
- `QP_MODEL_<PARTNER_ID>` - overrides `partners.<partner_id>.model`.

Exact examples:

- `QP_AUTH_GH_COPILOT_PARTNER_GITHUB_OAUTH_TOKEN`
- `QP_AUTH_GEMINI_PRODUCT_GEMINI_API_KEY`
- `QP_AUTH_LOCAL_OPENAI_PARTNER_API_KEY`
- `QP_MODEL_GH_COPILOT_PARTNER`

A non-empty environment value wins over `partners/config.json`; empty or whitespace-only values are ignored. `partners/config.json` stays local and gitignored either way - the variables only change where a value is resolved from, never where it is stored. There are no generic overrides for `models`, `fallback_models`, `generation` or `options`.

The `auth` object may be omitted from a partner section when every needed secret comes from the environment; a non-object `auth` value is still rejected. To keep the variable names unambiguous, a config is rejected as `invalid_config` when two partner ids - or two auth keys of the loaded partner - normalize to the same token, or when any two `(partner_id, auth_key)` pairs across all configured partners would produce the same `QP_AUTH_` variable name.

## MCP client registration

Add two servers to your MCP client configuration, replacing the path placeholders:

```json
{
  "mcpServers": {
    "gh-copilot-partner": {
      "command": "python3",
      "args": ["<repo>/partners/mcp_partner.py"],
      "env": {
        "PARTNER_ID": "gh-copilot-partner",
        "PARTNER_CONFIG": "<repo>/partners/config.json"
      },
      "disabled": false
    },
    "gemini-product": {
      "command": "python3",
      "args": ["<repo>/partners/mcp_partner.py"],
      "env": {
        "PARTNER_ID": "gemini-product",
        "PARTNER_CONFIG": "<repo>/partners/config.json"
      },
      "disabled": false
    }
  }
}
```

## Tools

Common tools:

- `partner_info()` - backend, default model, active skill, installed skills and auth status without secrets.
- `list_partner_models()` - provider models available to the configured account.
- `list_partner_skills()` - installed skill packs.

Ask tools (canonical names, advertised by every non-`review` toolset - `gh-copilot-partner`, `gemini-product`, `pm-partner`, `market-partner`, `strategy-partner`, `comms-partner` and any other partner configured with a non-review toolset):

- `ask_partner(question, context?, files?, skill?, role?, model?)`
- `ask_partner_v2(question, context?, files?, skill?, role?, model?)`
- `ask_partner_dialog(messages, context?, skill?, role?, model?)`

The `code` toolset (`gh-copilot-partner`, `local-openai-partner`) and the `product` toolset (`gemini-product`) also advertise their historical names as legacy aliases of the canonical ask tools, with the same signatures:

- `ask_code_partner`, `ask_code_partner_v2`, `ask_code_partner_dialog`
- `ask_product_partner`, `ask_product_partner_v2`, `ask_product_partner_dialog`

The `*_v2` ask tools accept the same arguments as the base ask tools but return a structured JSON object instead of bare text: `{"partner_id", "skill", "model_requested", "model_used", "model_match", "text"}`. `model_requested` is the candidate model that produced the answer, `model_used` is the provider-reported served model (or `null` when the backend reports none), and `model_match` is `true`/`false`/`null` - `null` when `model_used` is absent.

`code-reviewer` tools (`toolset: "review"`):

- `list_reviewer_models()`
- `review_code(head_sha, base_sha, diff, model, strict_model?, skill?, context?, files?, review_id?)`

Dialog tools are stateless: pass the whole message history in each call. The optional `context` argument carries a caller-supplied context/context-pack brief and is sent as a leading user turn with a `## Context` section, inserted before the message history.

## Advisory business partners

`pm-partner`, `market-partner`, `strategy-partner` and `comms-partner` are four enabled-by-default Gemini-backed advisory instances sharing the generic `advisory` toolset. Each has its own harness contract and `skills_root`, so per-role skill packs can evolve without collisions. They answer through `ask_partner*` with prose, framework-style output rather than a strict artifact.

Capability boundaries:

- The partners reason offline and statelessly. There is no web access, no live market data, no automatic competitor monitoring and no "Big Data" feed behind any of them: outputs are frameworks and hypotheses synthesized from the signals the caller supplies in `question`, `context` and `files`.
- Long-term memory is caller-managed. `comms-partner` (and any partner) accepts a startup/project brief in the `context` field of each call and treats it as the memory substitute; the server never stores session state. A fill-in template lives at `partners/context-pack/CONTEXT_PACK.md`.
- All outputs are advisory only: nothing is published, merged or executed, and estimates such as TAM/SAM/SOM are labeled estimations from caller assumptions, not observed data.

## Advisory code review

The `code-reviewer` partner exposes a strict review toolset instead of the conversational `ask_*` tools.

- `list_reviewer_models()` returns the allowlisted model aliases, their pinned backend model IDs and the configured input limits. It is metadata only and performs no provider access.
- `review_code` runs one advisory second-opinion review and always returns a schema-valid JSON artifact (`status` is `ok`, `rejected` or `error`).

Required `review_code` arguments: `head_sha` and `base_sha` (40-hex SHAs echoed into the artifact), `diff` (a unified diff) and `model`. The `model` argument must be an alias from `options.review.allowed_models` - free-form provider IDs are rejected before any backend access.

With `strict_model` (default `true`) the call fails closed unless live model discovery confirms the pinned backend model and the provider-reported `model_used` matches it; the artifact `provenance` records `model_alias`, `model_requested`, `model_used`, `model_match` and `discovery_confirmed`. Setting `strict_model: false` downgrades a mismatch to a warning with `model_match: false`.

The optional `skill` argument selects an installed review skill pack from `list_partner_skills`; when omitted, the configured `default_skill` is used. An unknown skill name fails closed with `status=error`, `invalid_request` before any provider access. The resolved skill name participates in the `idempotency_key` material, so identical inputs under different skills never share a cache entry. The `security-reviewer` pack installed under `skills/code-reviewer/` performs an advisory defensive security review of the supplied diff.

Optional `files` entries are bounded extra context: a repo-relative path string acts as a label only, while `{name, content}` objects attach inline text. All file contents are caller-supplied; the server never reads the filesystem.

The artifact is advisory only (`advisory: true`): it never authorizes a merge and never publishes comments or reviews anywhere. Input validation, path checks and outbound secret scanning all run before any provider access, so oversized, unsafe or secret-like input is rejected without a backend call. Only `status=ok` artifacts are cached, keyed by an `idempotency_key` derived from the review inputs.

When model output fails artifact validation, the service makes at most one bounded repair retry on the same resolved backend model: the retry prompt reuses the original prompt and appends a fresh nonce-delimited section with the truncated previous output and stable validation issue codes. Output containing secret-like text is never echoed back to the provider - the retry is skipped entirely - and the retry response goes through the same `model_used` provenance check. A successful repair is surfaced via the artifact warning `model output repaired after one retry`; every other outcome fails closed as `status=error`, `invalid_output`.

## local-openai-partner (OpenAI-compatible endpoint)

`local-openai-partner` is a disabled-by-default partner that talks to any OpenAI-compatible HTTP endpoint through the `openai-compatible` backend. It stays disabled in `partners/config.example.json`; enable it only in a local `partners/config.json` after pointing it at an endpoint you control. Because `options.openai.base_url` can be any host, enabling this partner sends prompts and file context to that endpoint - keep it off unless the endpoint is intended.

Endpoint contract under `options.openai.base_url` (for example `http://127.0.0.1:11434/v1`):

- `GET {base_url}/models` must return `{"object": "list", "data": [{"id": "...", "object": "model"}, ...]}`. The ids feed `list_partner_models()`.
- `POST {base_url}/chat/completions` accepts `{"model", "messages", "stream": false, "temperature", "max_tokens"}` and must return `choices[0].message.content` as a non-empty string. A top-level `model` string is optional; when present it is reported as `model_used`.

Authentication is optional. When `auth.api_key` - or the `QP_AUTH_LOCAL_OPENAI_PARTNER_API_KEY` environment override - is set, it is sent as `Authorization: Bearer <key>`; endpoints without auth can leave the placeholder in place or omit `auth` entirely.

For repeatable no-secret testing run the bundled fake endpoint. It binds 127.0.0.1 only, serves the two fixed ids `fake-model-a` and `fake-model-b`, returns deterministic `fake-ok: ...` answers and exposes `GET /healthz` for readiness polling:

```bash
python3 tools/fake_openai_server.py --port 4143
# --served-model NAME overrides the model id reported in responses;
# by default the requested model is echoed back.
```

Then point `options.openai.base_url` at `http://127.0.0.1:4143/v1`. The `openai-compat-smoke` job in the Protocol Smoke workflow exercises the full pipeline - config load, backend call, response parsing - against this fake endpoint in CI without secrets.

`model_used` verdict: the backend reports the response-side `model` field exactly as the endpoint returned it. Whether that value proves served-model identity is endpoint-dependent - a proxy may echo the request `model` back or omit the field entirely - so strict `code-reviewer` provenance on this backend requires verifying that the specific endpoint does not merely echo. The fail-closed `strict_model` behavior still applies: a missing or mismatched `model_used` is never treated as confirmed.

## Harness and skills

`partners/harness/<partner>.txt` is the base operating contract and output style.

`partners/skills/<partner>/<skill>/` is a pluggable specialization:

- `manifest.json` - name, description, when to use, optional model and generation overrides.
- `prompt.txt` - skill-specific instructions.

Prompt composition order: base harness, skill prompt, then the caller's question context. `skill` can be passed per tool call; otherwise `default_skill` from the config is used.

Set `skills_root` in a partner config to share a skill directory across partner IDs. The example `local-openai-partner` reuses `skills/gh-copilot-partner`.

The `code-reviewer` partner ships two packs: `code-reviewer` (default general review) and `security-reviewer` (advisory defensive security review: injection, authn/authz gaps, secret handling, unsafe deserialization, path traversal, SSRF, crypto misuse, weak randomness and supply-chain red flags). Select one per call through the `skill` argument of `review_code`.

The `gemini-product` partner ships five packs: `product-manager` (default product framing), `prioritization`, `ux-architect`, `spec-compliance-verifier` (advisory spec/diff drift check returning a strict artifact) and `release-manager` (advisory release-note and changelog drafting with semver signal classification). Select one per call through the `skill` argument of the `ask_partner*` tools.

## Error contract

Tool errors are returned as text in the MCP result:

- `invalid_config` - missing config, secret, harness or skill.
- `unauthorized` - provider rejected the configured credentials.
- `model_not_supported` - model is absent from the account catalog or disabled for the plan.
- `unsupported_api_for_model` - model requires another provider endpoint.
- `quota_exceeded` - quota, rate limit or HTTP 429.
- `partner_unreachable` - timeout, DNS or upstream availability.
- `invalid_request` - malformed tool arguments.

Secrets are not returned in tool errors. Session tokens are kept in memory and refreshed before expiry.

## Development

Requirements: Python 3.11+ and no mandatory third-party dependencies.

Run unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Run the static type checker:

```bash
uvx --from "pyright[nodejs]==1.1.414" pyright
```

CI runs this check in a separate `typecheck` job that is currently non-blocking (`continue-on-error: true`) until the known error baseline is fixed. Once the baseline is cleaned up, `continue-on-error` should be removed so the check becomes blocking.

Run a no-secret protocol smoke test against `partners/config.example.json`. These are the same commands the CI `Protocol Smoke` workflow runs; they exercise stdio startup and JSON-RPC tool discovery only and never call a provider:

```bash
PARTNER_ID=gh-copilot-partner \
PARTNER_CONFIG=partners/config.example.json \
python3 tools/mcp_smoke.py --list

PARTNER_ID=gh-copilot-partner \
PARTNER_CONFIG=partners/config.example.json \
python3 tools/mcp_smoke.py --call partner_info

PARTNER_ID=gh-copilot-partner \
PARTNER_CONFIG=partners/config.example.json \
python3 tools/mcp_smoke.py --call list_partner_skills
```

Run a live call:

```bash
PARTNER_ID=gemini-product \
PARTNER_CONFIG=partners/config.json \
python3 tools/mcp_smoke.py --call ask_partner --question 'Say OK.'
```

Live calls through provider-backed tools (`ask_*`, `list_partner_models`) require a local `partners/config.json` with real credentials and are intentionally not part of CI.

The smoke client exits nonzero when the server returns a JSON-RPC error, a tool result with `isError`, a missing response or a non-JSON stdout line.

## Notes

Direct Copilot access uses an internal token exchange and therefore carries more compatibility risk than the public Gemini API. Keep the backend isolated and prefer clear errors over silent fallbacks. Model availability is plan-dependent; always trust `list_partner_models()` over a hardcoded list.
