# Security policy

## Secrets

- Do not commit raw tokens, passwords, private keys, cookies, or local login files.
- Config files may reference environment variable names such as `QA_AIST_TRACKER_TOKEN`.
- Evidence renderers must redact values that look like secrets before tracker writes.

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
