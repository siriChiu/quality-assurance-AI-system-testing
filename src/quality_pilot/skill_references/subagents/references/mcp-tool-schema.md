# Redacted `subagents` MCP discovery

Server: `subagents`
Transport: local stdio (`mcp_server.py`)

Discovered tools:

- `subagent_status`
  - input: `{ "providers": ["deepseek"|"kimi"|"qwen"|"mimo", ...] }`
  - purpose: inspect saved provider/browser status.
- `subagent_ask`
  - required input: `providers` and `prompt`
  - optional input: `prompt_file`, `include`, `attach`, `timeout_sec`, `mode`,
    `deep_think`, `search`, `no_humanize`
  - purpose: forward a prompt to the native provider launcher.
- `subagent_login`
  - input: `{ "provider": "deepseek"|"kimi"|"qwen"|"mimo" }`
  - purpose: open a visible login flow.

The server returns launcher console output plus `provider_results`,
`chat_record_verified_by_provider`, `chat_record_missing_providers`, and an
aggregate `chat_record_verified` gate. A provider is verified only after its
driver reloads the authenticated chat URL/ID and matches the submitted prompt
and answer. Consumers must still preserve false/missing states and must not
equate a temporary answer file or a printed URL with provider-side history.

The provider profiles contain cookies and local storage. They are never copied
into this reference, repository, prompts, evidence, or issue payloads.
