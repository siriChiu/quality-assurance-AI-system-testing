---
name: quality-pilot-subagents
description: "Use the configured local subagents MCP server for independent DeepSeek, Kimi, Qwen, and Xiaomi Mimo engineering review."
compatibility: Requires Hermes MCP server named subagents and provider login profiles.
license: MIT
metadata:
  hermes:
    tags: [mcp, subagents, review, quality-pilot, candidate-only]
    mcp_server: subagents
---

# Quality Pilot subagent council

Use this adapter when the lead asks for independent engineering opinions from the
local `subagents` MCP server. The four providers are advisory reviewers, not employees with write access and
not QA authorities. The active council is DeepSeek, Kimi, Qwen, and Xiaomi Mimo;
Z.ai is explicitly excluded until its capacity/security record gate is restored.

## Exact MCP tools

The configured server exposes:

- `subagent_status` — check provider availability/login state.
- `subagent_ask` — send a prompt to one or more providers.
- `subagent_login` — request a visible login flow only with explicit user direction.

Use the server's discovered schema rather than inventing additional tools. See
`references/mcp-tool-schema.md` for the redacted discovered contract.

## Independent-review procedure

1. Call `subagent_status` for `deepseek`, `kimi`, `qwen`, and `mimo`.
2. Send the same neutral prompt to each provider separately, unless the lead
   explicitly requests a different comparison design.
3. Label each response with its provider and preserve unavailable/timeout/error
   states. Do not silently replace one provider with another.
4. Compare agreement and disagreement. The lead remains the final decision-maker.
5. Treat every response as candidate advice. It cannot edit files, write QA
   artifacts, approve a PR, merge, publish, or bypass a write gate.

Suggested roles:

- DeepSeek: architecture and integration critic.
- Kimi: developer/operator UX and workflow critic.
- Qwen: BDD, testing, and Task Graph implementation critic.
- Xiaomi Mimo: independent product/workflow and source-authority critic.

## Development-only advisory boundary

These providers are helpers for the lead agent's development analysis only. They
are not runtime Task Graph nodes, product test generators, mapping authorities,
QA verifiers, or release functionality. Their suggestions must stay outside
contracts, runs, evidence, graph state, and write requests unless the lead agent
independently validates and manually applies an approved change.

## Chat-record evidence gate

A returned answer, local output file, `logged_in` status, or printed `chat_url`
is **not** by itself proof that a provider-side chat record is available. The
MCP adapter now exposes per-provider `chat_record_verified` results; those are
true only after the driver reloads the authenticated chat URL/ID and matches the
submitted prompt/answer. Preserve missing/false provider results. Mark a provider
`UNVERIFIED` unless that gate passes or the human explicitly confirms the visible
provider history.

Never report “all four consulted” when one provider only produced a local capture
or an unverified URL. If the record gate is not met, report the opinion as
`candidate_unverified` and do not use it as authoritative evidence. The Quality
Pilot-side `validate_mcp_subagent_results(...)` helper emits a deterministic
record receipt for verified providers and returns `record_gate_status: BLOCK`
for missing/unverified providers; this is a consultation gate, not a QA PASS.

## Security and collaboration

- Never put cookies, passwords, API keys, bearer tokens, or raw credential values
  in prompts, attachments, output files, or transcripts.
- Do not ask a provider to modify the repository; the parent agent performs
  reviewed edits.
- Do not let provider suggestions change dependency edges, QA verdicts, or write
  permissions without deterministic validation.
- Keep disagreements respectful and explicit. Do not pressure the user into
  accepting a provider's recommendation.
