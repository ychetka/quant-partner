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
- `org-partner` - organizational design advisory for AI-native companies: org structures, span of control for mixed human+agent teams, committees/RACI/quorums and role/hand-off contracts (`advisory` toolset, Gemini API).
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
  media_ingest.py                 offline transcript ingest for media-partner
  visual_adapter.py               optional visual annotations for transcript.json
  transcript_chunker.py           segment-aligned chunking for oversized transcripts
  transcript_rag.py               BM25 retrieval index over transcript corpora
  decision_ledger.py              offline append-only decisions.jsonl ledger
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

Ask tools (canonical names, advertised by every non-`review` toolset - `gh-copilot-partner`, `gemini-product`, `pm-partner`, `market-partner`, `strategy-partner`, `comms-partner`, `org-partner` and any other partner configured with a non-review toolset):

- `ask_partner(question, context?, files?, skill?, role?, model?)`
- `ask_partner_v2(question, context?, files?, skill?, role?, model?)`
- `ask_partner_dialog(messages, context?, skill?, role?, model?)`

`advisory`-toolset partners additionally accept the optional grounding fields `decision_to_make`, `options_considered`, `constraints` and `context_pack` on all three ask tools - composed as labeled prompt sections, with a clarifying-questions fail-soft on ungrounded `ask_*` calls.

The `code` toolset (`gh-copilot-partner`, `local-openai-partner`) and the `product` toolset (`gemini-product`) also advertise their historical names as legacy aliases of the canonical ask tools, with the same signatures:

- `ask_code_partner`, `ask_code_partner_v2`, `ask_code_partner_dialog`
- `ask_product_partner`, `ask_product_partner_v2`, `ask_product_partner_dialog`

The `*_v2` ask tools accept the same arguments as the base ask tools but return a structured JSON object instead of bare text: `{"partner_id", "skill", "model_requested", "model_used", "model_match", "text"}`. `model_requested` is the candidate model that produced the answer, `model_used` is the provider-reported served model (or `null` when the backend reports none), and `model_match` is `true`/`false`/`null` - `null` when `model_used` is absent.

`code-reviewer` tools (`toolset: "review"`):

- `list_reviewer_models()`
- `review_code(head_sha, base_sha, diff, model, strict_model?, skill?, context?, files?, review_id?)`

Dialog tools are stateless: pass the whole message history in each call. The optional `context` argument carries a caller-supplied context/context-pack brief and is sent as a leading user turn with a `## Context` section, inserted before the message history.

## Advisory business partners

`pm-partner`, `market-partner`, `strategy-partner`, `comms-partner`, `org-partner`, `media-partner` and `text-quality-partner` are seven enabled-by-default Gemini-backed advisory instances sharing the generic `advisory` toolset. Each has its own harness contract and `skills_root`, so per-role skill packs can evolve without collisions. They answer through `ask_partner*` with prose, framework-style output rather than a strict artifact.

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

## Media transcript ingest

`tools/media_ingest.py` is an offline companion to `media-partner`. It is not part of the MCP server: it turns a media reference into a schema-validated `transcript.json` artifact on stdout or at `--output` (mode 0600), and callers pass that artifact to `media-partner` through the ordinary `context` or `files` arguments of the ask tools. The tool is stdlib-only; all external work is delegated to binaries through argv lists (never a shell), explicit timeouts and named errors.

Prerequisites:

- `yt-dlp` on `PATH` for `from-youtube` and `from-web-page` (override with `--ytdlp-binary` or `YT_DLP_BINARY`).
- whisper.cpp `whisper-cli` on `PATH` plus a model file for every ASR path (`--whisper-binary`/`WHISPER_CPP_BINARY`, `--whisper-model`/`WHISPER_CPP_MODEL`). ASR runs locally only - no hosted transcription service is ever called.
- `ffmpeg`, used by `yt-dlp -x --audio-format wav` to produce the WAV input whisper-cli expects. The captions path does not need it.

Usage:

```bash
# Local media file, local whisper.cpp ASR
python3 tools/media_ingest.py from-file meeting.mp4 \
  --whisper-model /models/ggml-base.bin -o transcript.json

# Caller-supplied transcript instead of ASR (plain text, SRT, VTT or loose JSON)
python3 tools/media_ingest.py from-file meeting.mp4 \
  --transcript-file meeting.srt -o transcript.json

# YouTube: platform captions (default, platform_captions trust)
python3 tools/media_ingest.py from-youtube 'https://www.youtube.com/watch?v=...' \
  -o transcript.json

# YouTube: download audio and transcribe locally (self_transcribed trust)
python3 tools/media_ingest.py from-youtube 'https://www.youtube.com/watch?v=...' \
  --asr --whisper-model /models/ggml-base.bin -o transcript.json

# Generic web page: yt-dlp extracts the media, local ASR transcribes it
python3 tools/media_ingest.py from-web-page 'https://videos.example.com/talks/keynote' \
  --whisper-model /models/ggml-base.bin -o transcript.json

# Same ASR path with speaker turn tagging (needs a tdrz-capable model)
python3 tools/media_ingest.py from-file meeting.mp4 \
  --whisper-model /models/ggml-small.en-tdrz.bin --diarize -o transcript.json

# Re-validate an artifact and print its content_hash
python3 tools/media_ingest.py validate transcript.json
```

Every transcript declares `source.transcription_method` and `source.transcription_trust`: `local_asr` and `caller_supplied` map to `self_transcribed`, while `yt_dlp_captions` maps to `platform_captions` - platform-authored or auto-generated captions of unknown quality, explicitly labeled so downstream consumers can weigh them accordingly. `--ref` overrides `source.ref` on every ingest subcommand; `from-file` defaults to the media basename so local paths never reach the artifact, and the URL subcommands default to the URL itself.

Speaker turn tagging: `--diarize` on any ASR path (`from-file` without `--transcript-file`, `from-youtube --asr`, `from-web-page`) passes `--tinydiarize` to whisper-cli, which requires a tdrz-capable model (for example `ggml-small.en-tdrz.bin`) and a whisper.cpp build that supports the flag. When turn tagging works, `segments[].speaker` carries turn-relative labels `S0`, `S1`, ... marking speaker *changes* within that one file. These are turn tags, not speaker identity: they are not stable across files, across runs, or as real-world identities, so media-partner must not read them as "who is speaking". When the binary or model cannot produce turn tags the flag soft-skips: ingest still succeeds with `speaker: null` and a stderr note. `--diarize-required` turns the same gap into a hard `diarization_unavailable` error. Both flags are rejected with `invalid_usage` outside ASR modes (platform captions, `--transcript-file`).

Error contract (stderr `ERROR <code>: <message>`, exit 1):

| Code | Meaning |
| --- | --- |
| `invalid_usage` | Bad flags, flags inapplicable to the selected mode (for example `--sub-langs` with `--asr`, or binary overrides with `--transcript-file`), missing files, missing `--whisper-model`, mutually exclusive `--asr`/`--transcript-file`. |
| `yt_dlp_unavailable` | `yt-dlp` binary missing on `PATH` or not executable. |
| `asr_unavailable` | whisper.cpp binary missing on `PATH` or not executable. |
| `no_captions_available` | yt-dlp ran but produced no usable caption file; pass `--transcript-file` or retry with `--asr`. |
| `no_media_found` | yt-dlp ran but the page yielded no downloadable media. |
| `diarization_unavailable` | `--diarize-required` was set but the whisper.cpp run produced no speaker-turn tags; turn tagging needs a tdrz-capable model and a build that supports `--tinydiarize`. |
| `external_tool_error` | External binary timed out or exited non-zero (stderr tail in the hint). |
| `transcript_format_invalid` | Caller-supplied transcript text matched no supported format, or the input file is not valid UTF-8. |
| `transcript_schema_invalid` / `unsupported_schema_version` | Artifact failed the schema contract on `validate` or a cache write. |
| `output_write_failed` | `--output` path not writable. |

Cache policy: transcripts are stored in a bounded cache (`--cache-dir`, default `.cache/media-ingest`) keyed only by `content_hash` - a sha256 over canonicalized segments, never by URL or file path. Entries are 0600 files with a 6-hour TTL (`--cache-ttl-seconds`) and a 256-file bound (`--cache-max-files`); `--no-cache` bypasses it. A hit re-emits the stored transcript with the current input's `source.ref` and a `cache_hit: true` flag. The cache stores transcripts only - never partner questions or answers - and corrupt or tampered entries count as misses.

### Visual annotations (optional adapter)

`tools/visual_adapter.py` is an opt-in companion that enriches an existing `transcript.json` with visual observations extracted by a Gemini model through the Files API. It updates the transcript in place: new entries land in `annotations[]` as `{kind, t0, t1, text, source: "gemini-files-api", model, generated_at}` - `kind` is `visual` (scenes, slides, on-screen text, UI state) or `object` (tracked objects and people appearing/disappearing). Segments and `content_hash` are untouched, the write-back is atomic (temp file + `os.replace`, mode 0600) and the merged document is re-validated before it replaces the original - on any failure the file stays byte-identical.

Prerequisites:

- `GEMINI_API_KEY` in the environment. The key is sent only as the `x-goog-api-key` header - never in URLs - and every error path redacts it if a message ever echoes it back.
- A local copy of the media: `--media PATH` always works; without it, `source.ref` is resolved when `source.kind == "local_file"` (URLs are never re-downloaded, so a `youtube`/`web_page` transcript without `--media` fails as `media_not_found`).

Usage:

```bash
GEMINI_API_KEY=... python3 tools/visual_adapter.py \
  --transcript transcript.json --media meeting.mp4

# Re-run even when annotations for this transcript+model are cached
python3 tools/visual_adapter.py --transcript transcript.json --force
```

The uploaded file is ephemeral: the adapter deletes it (best-effort) after the analysis and never persists file URIs. Extracted observations are cached under `~/.cache/quant-partner/visual/` (override with `--cache-dir`), keyed by `sha256({content_hash, model, prompt_version})`; a hit skips the API call entirely and needs no API key, `--force` bypasses it. `--chunk-seconds N` (default 30) bounds the observation window asked for in the prompt - stdlib-only means the media itself is never sliced. `--model` defaults to `gemini-2.5-flash`.

Cost note: the adapter is opt-in and runs one paid Gemini Files-API upload plus one `generateContent` call per uncached transcript; the cache keeps repeat runs free. Model output is parsed defensively - malformed entries are dropped with a stderr warning, and output with no usable JSON array at all fails as `model_output_invalid` without touching the transcript.

Named errors (stderr `ERROR <code>: <message>`, exit 1): `auth_error` (missing or rejected `GEMINI_API_KEY`), `media_not_found`, `upload_failed`, `file_api_error` (status poll or generate call), `quota_exceeded` (HTTP 429 or quota-class failures), `model_output_invalid`, plus the shared `invalid_usage`, `output_write_failed` and schema errors from `media_ingest`.

### Transcript chunking and retrieval

`tools/transcript_chunker.py` and `tools/transcript_rag.py` are two more offline, stdlib-only companions to `media-partner`, for when a `transcript.json` is too large for one `context`/`files` argument or when questions span many transcripts at once.

**Why:** partners have context limits, and ad-hoc splitting destroys the `[mm:ss]` citations the partner's answers rely on. The chunker emits segment-aligned chunks - it never splits a segment mid-text - that keep `t0`/`t1`, the covered `segment_ids` and the `speaker_set`, so every chunk stays citation-backed. The retrieval tool ranks those chunks with deterministic BM25, so a caller can attach only the few relevant chunks to a question instead of a whole corpus.

Usage:

```bash
# Split a transcript into <=8000-char chunks; writes meeting.chunks.json (0600)
python3 tools/transcript_chunker.py meeting.json --max-chars 8000

# Print the chunks document instead of writing a sidecar
python3 tools/transcript_chunker.py meeting.json --stdout

# Build a persistent BM25 index over a directory of transcripts
python3 tools/transcript_rag.py build transcripts/ --index rag_index.json

# Re-chunk everything instead of reusing unchanged documents
python3 tools/transcript_rag.py build transcripts/ --index rag_index.json --rebuild

# Ranked JSON hits: path, content_hash, chunk_id, t0/t1, speaker_set, score, snippet
python3 tools/transcript_rag.py query --index rag_index.json \
  "what was decided about pricing" --top-k 5

# Ad-hoc query over files without writing an index
python3 tools/transcript_rag.py query "pricing decision" --ephemeral a.json b.json
```

Chunking details: `chunk_transcript` accumulates whole segments until the next one would exceed `max_chars`; a single oversized segment becomes its own chunk and is never split. When segments carry non-null `speaker` values the flusher prefers cutting at the last speaker change inside the current group - best-effort, the `max_chars` bound always wins. The optional `*.chunks.json` sidecar stores `{schema_version: 1, source_hash, generator_version, generated_at, chunks}` and is written atomically with mode 0600.

Index details: the index stores `doc_sources` (`{path, content_hash}`), per-document chunks plus `token_count` keyed by `content_hash`, `postings` (`term -> [content_hash, chunk_id, tf]`), `doc_lengths` and `avg_doc_length`. The build is incremental: documents whose `content_hash` is already indexed keep their stored chunks unless `--rebuild` is passed, documents no longer discovered are dropped, and every candidate file is schema-validated - invalid or foreign JSON files are skipped with a stderr warning and never crash the build. When a `<name>.chunks.json` sidecar exists and its `source_hash` matches the transcript's `content_hash` it is reused; otherwise the transcript is chunked on the fly in memory - sidecars are read, never written, by this tool. Tokenization is lowercase alphanumeric runs of 2+ characters with no stemming, so ranking is fully deterministic (BM25 `k1=1.5`, `b=0.75`).

Named errors (stderr `ERROR <code>: <message>`, exit 1): `chunking_error` (invalid or empty source transcript), `rag_index_error` (missing, stale or corrupt index - corruption is reported with the 1-based JSON line or the offending key path), `rag_corpus_empty` (no valid transcripts found), plus the shared `invalid_usage`, `output_write_failed` and `unsupported_schema_version` from `media_ingest`.

## Decision ledger

`tools/decision_ledger.py` is an offline, stdlib-only companion to the advisory partners. It keeps an append-only `decisions.jsonl` ledger recording which partner recommendations the orchestrator adopted, modified or rejected - the raw material a `comms-partner` `retro-review` call later works over. It is not part of the MCP server and never calls a provider.

Entry contract (`schema_version` 1, strict - unknown keys and type mismatches are rejected):

- `decision_id`, `project`, `partner`, `recommendation_summary` - non-empty strings.
- `context_size` (non-negative integer) and/or `context_hash` (`sha256:<64 lowercase hex>`) - each optional on its own, but at least one of the two is required; they fingerprint the context a recommendation was given without storing the context itself.
- `orchestrator_action` - exactly one of `adopted`, `modified`, `rejected`.
- `created_at` - an ISO-8601 UTC timestamp ending in `Z`.

Usage:

```bash
# Append one entry from a JSON file
python3 tools/decision_ledger.py append --file entry.json

# Append inline; values parse as JSON when possible, else stay raw strings
python3 tools/decision_ledger.py append \
  --field decision_id=d-014 --field project=quant-partner \
  --field partner=strategy-partner --field context_size=4210 \
  --field recommendation_summary='prefer local ASR over hosted transcription' \
  --field orchestrator_action=adopted --field created_at=2026-11-08T10:30:00Z \
  --field schema_version=1

# Validate the whole ledger, then dump entries or a compact summary
python3 tools/decision_ledger.py read
python3 tools/decision_ledger.py list

# Validate the ledger file or a single candidate entry
python3 tools/decision_ledger.py validate
python3 tools/decision_ledger.py validate --file entry.json
```

`--ledger PATH` overrides the default `./decisions.jsonl` on every subcommand. The file is created with mode 0600 on first append. The ledger is append-only by construction: `append` writes one newline-terminated JSON line through a single `O_APPEND` write sequence (concurrent appenders never interleave a line), validates the whole existing file first and refuses to write onto a corrupt ledger, and there is no update or delete path anywhere - `read`, `list` and `validate` are read-only.

Error contract (stderr `ERROR <code>: <message>`, exit 1): `invalid_usage`, `entry_schema_invalid` / `unsupported_schema_version`, `ledger_corrupt` (names the 1-based line number, including a truncated or unterminated tail line), `ledger_read_failed` and `ledger_write_failed`.

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

## Process lifecycle and resilience

Each partner is a separate `partners/mcp_partner.py` process selected by `PARTNER_ID`. The stdio loop is serialized: requests are handled one at a time in stdin order, so a slow provider call head-of-line-blocks only its own partner process - never other partners. There is no internal concurrency to tune; parallelism comes from running one process per partner.

- Per-call error boundary: every `tools/call` is isolated. A `PartnerError` becomes an `isError` tool result with a sanitized message; an unexpected backend exception becomes a generic `internal_error` result that never echoes exception text. The transport also appends one structured JSON event line to stderr (`event: "internal_error"`, `tool`, `error_class`), so the whole stderr stream stays JSONL. Either way the process keeps serving later calls normally.
- Structured request log: each `tools/call` appends one JSON line to stderr with `event: "partner_call"`, `partner_id`, `tool`, `skill`, `model_requested`, `model_used`, `latency_ms` and `outcome` (`ok`, a `PartnerError` code or `internal_error`). `review_code` always returns a JSON artifact instead of raising, so its outcome is mapped from the artifact `status` (`ok`, `error` or `rejected`; `unknown` when the payload has no recognizable status). Question, context, file and diff contents are never logged, and the serialized line is sanitized against the configured secrets before writing.
- Config hot-reload: before each call the server stats the `PARTNER_CONFIG` file resolved at startup and reloads it when the `(mtime_ns, size)` pair changed, so timestamp-preserving writes that alter the file size are still detected. A successful reload swaps in the new config and drops the cached backend so new auth and options take effect immediately. Any failed reload (invalid JSON, non-UTF-8 bytes, an unreadable or deleted file, an unexpected error) keeps the last valid config and emits a `config_reload` warning line to stderr once per change; `partner_info` reports the failure as `config_reload_error` - a stable short reason code: `unreadable` (missing or unreadable file), `decode_error` (non-UTF-8 content), a `PartnerError` code such as `invalid_config` (validation failure) or `internal_error` (unexpected exception). A config that reappears or is fixed is picked up on the next call.
- Timeouts and retries: each backend passes `timeout_seconds` (default 240) to its HTTP calls; a timeout surfaces as `partner_unreachable`. The server performs no automatic retry - advisory calls are idempotent and safe to retry by the caller.
- Recovery: killing the process remains a supported recovery path. The MCP client respawns it on the next call, the config is re-read at startup and the backend is rebuilt lazily.
- Placeholder detection: an auth value is ignored as a secret when it is empty, contains a placeholder marker (`REPLACE`, `TOKEN_HERE`, `KEY_HERE`, `YOUR_`, `EXAMPLE`) or is a bare env-var-style name such as `GITHUB_TOKEN` (uppercase letters, digits, `_`/`-` only, length >= 6, containing at least one letter; digits-only values are not placeholders). Surrounding whitespace is stripped before these checks, so a padded `" GITHUB_TOKEN "` still counts as a placeholder. `auth_configured` in `partner_info` therefore distinguishes "real credential present" from "placeholder left in the file".

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

CI runs this check in a separate `typecheck` job that acts as a hard-zero gate: the job runs pyright with `--outputjson`, reads `summary.errorCount` and fails when the count exceeds the baseline stored in the `PYRIGHT_ERROR_BASELINE` environment variable in `.github/workflows/lint.yml`, which is `0`. The codebase currently reports zero pyright errors, so any new error fails the job. Keep it at zero by running pyright locally before pushing and fixing reported errors (prefer real None-guards or raises over `cast()` or `# type: ignore`); raising the baseline requires an explicit note in the PR.

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
