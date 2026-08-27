# Security policy

## Secrets

- Do not commit raw tokens, passwords, private keys, cookies, or local login files.
- Config files should not reference tracker token environment variables. AI Quality Pilot relies on Hermes MCP handoff paths for Gitea/Redmine access.
- Evidence renderers must redact values that look like secrets before tracker writes.
- The centralized detector covers credential assignments, bearer/token prefixes, private keys, PII/restricted markers, email-like values, and opaque high-entropy payloads; unknown or failed classification is fail-closed at contract, task-context, evidence, and remote-write boundaries.
- Task Graph context packets, node outputs, checkpoints, and repair records pass through the same redaction boundary and never contain raw credentials, customer, or restricted values.
- Knowledge Graph entity/relation/event payloads, provenance evidence, SQLite rows, JSON exports, fusion ledgers, and stage reports pass through the same fail-closed detector; graph storage is not a secret vault.
- Candidate LLM extraction is never written directly: ontology/domain-range/provenance validation must pass first. Fusion keeps a reversible ledger and requires human confirmation before merge.
- PTY/TUI transcripts and review MCP result reconciliation are redacted/fail-closed; a transcript marker, MCP response, or review request is not itself PASS, approval, or merge permission. A PR review handoff is explicitly `COMMENT`/advisory only; the user owns COMMENT, REQUEST_CHANGES, and APPROVED decisions.
- Product build/run testing executes only allowlisted argv with `shell=False` in a disposable copy of the pinned review worktree. The source worktree remains untouched; README commands are never executable without explicit user-owned allowlisting.
- Product evidence records artifact/contract hashes and redacted build/run logs. A build timeout is BLOCK, a product operation timeout is FAIL, and exit-only probes are HOLD.
- Browser testing is local Playwright only in the current slice. Missing package/browser/server or missing positive semantic interaction is BLOCK/HOLD; no curl, API, mock-DOM, or automatic browser-install fallback is allowed.

## Tracker writes

All tracker writes require a deterministic gate result:

```yaml
write_gate_result:
  allowed: false
  reason: string
  target_state: open|closed|missing|unknown
  contract_match: true|false
  evidence_current: true|false
  contains_raw_secret: true|false
```

Closed tracker items are read-only for active QA runs unless a maintainer explicitly creates a new, matching regression workflow.

Knowledge Graph node/edge counts, extraction confidence alone, or a successful local graph query cannot grant QA PASS, release readiness, tracker writes, PR approval, or merge permission.
