# Command surface

Implemented V1 commands:

```bash
qa-aist init-project --root <target-repo> --workspace .qa-aist-project
qa-aist setup --root <target-repo> --workspace .qa-aist-project
qa-aist status --root <target-repo> --workspace .qa-aist-project
qa-aist doctor --root <target-repo>
qa-aist config show --root <target-repo>
qa-aist config validate --config <target-repo>/.qa-aist.yaml
qa-aist qa-test list --root <target-repo>
qa-aist qa-test validate --root <target-repo>
qa-aist qa-test dry-run --root <target-repo>
qa-aist qa-test run --root <target-repo>
qa-aist qa-test run-one --root <target-repo> <case-id>
qa-aist close-loop status --root <target-repo>
qa-aist close-loop run-once --root <target-repo>
qa-aist report status --root <target-repo>
qa-aist report json --root <target-repo>
qa-aist tracker plan-write --root <target-repo>
```

V1 command groups:

```yaml
commands:
  setup:
    - init-project
    - setup
    - status
    - doctor
    - config validate
    - config show
  qa-test:
    - list
    - validate
    - dry-run
    - run
    - run-one
  close-loop:
    - run-once
    - status
  tracker:
    - plan-write
  report:
    - status
    - json
```

V1 never writes trackers. `tracker plan-write` evaluates the deterministic
write gate and returns the planned action list, which is empty until a future
adapter is enabled.

All commands must accept explicit paths so the tool can run from a submodule, package install, or separate checkout without writing project data into the QA-AIST repository.

`init-project` refuses to use a workspace that is itself a QA-AIST source checkout. This prevents embedded-tool layouts such as `<target-repo>/.qa-aist/` from receiving host-project cases or runtime state.
