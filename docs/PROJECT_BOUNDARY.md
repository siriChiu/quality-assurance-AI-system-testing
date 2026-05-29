# Repository boundary

status: active

## Rule

QA-AIST is a product repository. It is not a host-project workspace.

## Commit here

```yaml
allowed:
  - generic CLI/package source
  - generic docs
  - generic schemas
  - starter templates with placeholder values only
  - unit tests for QA-AIST itself
```

## Do not commit here

```yaml
forbidden:
  - product-specific test cases
  - project runner scripts
  - issue mirrors or live issue IDs
  - status reports for a target project
  - evidence bundles or run logs
  - hostnames, IP addresses, credentials, tokens, or lab topology
  - generated state from a target project
```

## Host-project overlay

Target repositories should store project-owned assets outside the QA-AIST tool checkout. Recommended default:

```text
.qa-aist.yaml
.qa-aist/cases/
.qa-aist/runners/
.qa-aist/rules/
.qa-aist/state/      # usually ignored
.qa-aist/evidence/   # ignored
.qa-aist/reports/    # generated
```

## Review checklist before commit

- Search for target project names and issue URLs.
- Search for IP addresses, hostnames, login filenames, tokens, and passwords.
- Verify no runtime state or evidence is staged.
- Verify templates use placeholders, not real systems.
