<!--
Copyright (c) 2026 Alexandr Varenic / ychetka.
SPDX-License-Identifier: MIT
-->

# quant-partner

`quant-partner` is a small MCP bridge that lets an AI assistant consult specialized LLM partners through a consistent tool interface.

One Python entrypoint runs multiple partner instances. Each instance has its own backend, base harness and pluggable skills:

- `gh-copilot-partner` - code, architecture, stack, review and debugging consultations through direct GitHub Copilot API access.
- `gemini-product` - product management, UX architecture and prioritization through the Gemini API.
- `code-reviewer` - advisory second-opinion review of unified diffs with strict model provenance.
- `openai-compatible` - optional generic backend for any OpenAI-compatible endpoint.

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
tools/
  mcp_smoke.py                    local JSON-RPC smoke client
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

`gh-copilot-partner` tools:

- `ask_code_partner(question, context?, files?, skill?, role?, model?)`
- `ask_code_partner_dialog(messages, skill?, role?, model?)`

`gemini-product` tools:

- `ask_product_partner(question, context?, files?, skill?, role?, model?)`
- `ask_product_partner_dialog(messages, skill?, role?, model?)`

`code-reviewer` tools (`toolset: "review"`):

- `list_reviewer_models()`
- `review_code(head_sha, base_sha, diff, model, strict_model?, context?, files?, review_id?)`

Dialog tools are stateless: pass the whole message history in each call.

## Advisory code review

The `code-reviewer` partner exposes a strict review toolset instead of the conversational `ask_*` tools.

- `list_reviewer_models()` returns the allowlisted model aliases, their pinned backend model IDs and the configured input limits. It is metadata only and performs no provider access.
- `review_code` runs one advisory second-opinion review and always returns a schema-valid JSON artifact (`status` is `ok`, `rejected` or `error`).

Required `review_code` arguments: `head_sha` and `base_sha` (40-hex SHAs echoed into the artifact), `diff` (a unified diff) and `model`. The `model` argument must be an alias from `options.review.allowed_models` - free-form provider IDs are rejected before any backend access.

With `strict_model` (default `true`) the call fails closed unless live model discovery confirms the pinned backend model and the provider-reported `model_used` matches it; the artifact `provenance` records `model_alias`, `model_requested`, `model_used`, `model_match` and `discovery_confirmed`. Setting `strict_model: false` downgrades a mismatch to a warning with `model_match: false`.

Optional `files` entries are bounded extra context: a repo-relative path string acts as a label only, while `{name, content}` objects attach inline text. All file contents are caller-supplied; the server never reads the filesystem.

The artifact is advisory only (`advisory: true`): it never authorizes a merge and never publishes comments or reviews anywhere. Input validation, path checks and outbound secret scanning all run before any provider access, so oversized, unsafe or secret-like input is rejected without a backend call. Only `status=ok` artifacts are cached, keyed by an `idempotency_key` derived from the review inputs.

## Harness and skills

`partners/harness/<partner>.txt` is the base operating contract and output style.

`partners/skills/<partner>/<skill>/` is a pluggable specialization:

- `manifest.json` - name, description, when to use, optional model and generation overrides.
- `prompt.txt` - skill-specific instructions.

Prompt composition order: base harness, skill prompt, then the caller's question context. `skill` can be passed per tool call; otherwise `default_skill` from the config is used.

Set `skills_root` in a partner config to share a skill directory across partner IDs. The example `local-openai-partner` reuses `skills/gh-copilot-partner`.

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
python3 tools/mcp_smoke.py --call ask_product_partner --question 'Say OK.'
```

Live calls through provider-backed tools (`ask_*`, `list_partner_models`) require a local `partners/config.json` with real credentials and are intentionally not part of CI.

The smoke client exits nonzero when the server returns a JSON-RPC error, a tool result with `isError`, a missing response or a non-JSON stdout line.

## Notes

Direct Copilot access uses an internal token exchange and therefore carries more compatibility risk than the public Gemini API. Keep the backend isolated and prefer clear errors over silent fallbacks. Model availability is plan-dependent; always trust `list_partner_models()` over a hardcoded list.
