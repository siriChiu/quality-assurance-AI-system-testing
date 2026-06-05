# Command surface

Implemented starter commands:

```bash
qa-aist init-project --root <target-repo> --workspace .qa-aist-project
qa-aist status --root <target-repo> --workspace .qa-aist-project
qa-aist config validate --config <target-repo>/.qa-aist.yaml
```

Planned command groups:

```yaml
commands:
  setup:
    - init-project
    - config validate
  qa-test:
    - list
    - run
    - normalize
  close-loop:
    - run-once
    - inspect-status
  tracker:
    - pull
    - plan-write
    - apply-write
  report:
    - status
    - evidence-index
```

All commands must accept explicit paths so the tool can run from a submodule, package install, or separate checkout without writing project data into the QA-AIST repository.

`init-project` refuses to use a workspace that is itself a QA-AIST source checkout. This prevents embedded-tool layouts such as `<target-repo>/.qa-aist/` from receiving host-project cases or runtime state.
