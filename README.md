<!--
Copyright (c) 2026 Alexandr Varenic / ychetka.
SPDX-License-Identifier: MIT
-->

# quant-partner

`quant-partner` is a small MCP bridge that lets an AI assistant consult specialized LLM partners through a consistent tool interface.

One Python entrypoint runs multiple partner instances. Each instance has its own backend, base harness and pluggable skills:

- `gh-copilot-partner` - code, architecture, stack, review and debugging consultations through direct GitHub Copilot API access.
- `gemini-product` - product management, UX architecture and prioritization through the Gemini API.
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

Dialog tools are stateless: pass the whole message history in each call.

## Harness and skills

`partners/harness/<partner>.txt` is the base operating contract and output style.

`partners/skills/<partner>/<skill>/` is a pluggable specialization:

- `manifest.json` - name, description, when to use, optional model and generation overrides.
- `prompt.txt` - skill-specific instructions.

Prompt composition order: base harness, skill prompt, then the caller's question context. `skill` can be passed per tool call; otherwise `default_skill` from the config is used.

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

Run a local protocol smoke test:

```bash
PARTNER_ID=gh-copilot-partner \
PARTNER_CONFIG=partners/config.json \
python3 tools/mcp_smoke.py --list
```

Run a live call:

```bash
PARTNER_ID=gemini-product \
PARTNER_CONFIG=partners/config.json \
python3 tools/mcp_smoke.py --call ask_product_partner --question 'Say OK.'
```

## Notes

Direct Copilot access uses an internal token exchange and therefore carries more compatibility risk than the public Gemini API. Keep the backend isolated and prefer clear errors over silent fallbacks. Model availability is plan-dependent; always trust `list_partner_models()` over a hardcoded list.
