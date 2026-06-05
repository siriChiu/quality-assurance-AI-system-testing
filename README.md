# QA-AIST

QA-AIST (QA - AI self tester) is a reusable, deterministic-first QA automation toolkit.

This repository is the tool source only. It must not contain customer, product, or project-specific test cases, runner scripts, issue mirrors, evidence bundles, status pages, hostnames, credentials, or active defect state.

## What belongs here

- Generic CLI source code.
- Generic schemas, templates, and setup helpers.
- Generic documentation for the close-loop QA workflow.
- Generic unit tests for QA-AIST itself.

## What does not belong here

- A target project's cases, runner registry, or status pages.
- Tracker issue mirrors or issue IDs from a target project.
- Runtime evidence, logs, screenshots, downloaded artifacts, or state snapshots.
- Login files, hostnames, tokens, passwords, or local lab configuration.
- Product-specific commands or domain-specific fixtures.

## Recommended host-project layout

When a target project uses QA-AIST, keep reusable tool code separate from project data:

```text
<target-repo>/
  tools/qa-aist/          # optional submodule or vendored checkout of this repo
  .qa-aist.yaml           # project config, safe to review before committing
  .qa-aist-project/
    cases/                # project-owned case contracts
    runners/              # project-owned runner scripts
    rules/                # project-owned rules
    state/                # ignored runtime state
    evidence/             # ignored runtime evidence
    reports/              # generated reports
```

The tool may be mounted at another path, but project-owned files should stay in the host project, not inside the QA-AIST repository.
If the tool source itself is embedded at `<target-repo>/.qa-aist/`, keep host-project assets in `<target-repo>/.qa-aist-project/` and do not write cases, runners, state, or evidence into the tool checkout.

## Quick start

```bash
python -m qa_aist.cli init-project --root /path/to/target-repo
python -m qa_aist.cli status --root /path/to/target-repo
```

After packaging is enabled:

```bash
qa-aist init-project --root /path/to/target-repo
qa-aist status --root /path/to/target-repo
```

## Design rules

1. Deterministic steps first; LLM/agents are optional summarizers or reviewers.
2. Tracker writes must pass a deterministic write gate.
3. Closed tracker issues are not active work items.
4. Retest comments must use the same canonical contract as the issue body.
5. Every confirmed bug must expand into sibling-surface, boundary, invalid-value, and side-effect-safe regression coverage.
6. Secrets live in environment variables or secret stores, never in this repo.
