# QA-AIST documentation

QA-AIST is a reusable tool. Documentation in this repository must describe the product, interfaces, and generic templates only.

Hermes integration is guided and interactive: `/qa-aist ...` responses include `next_actions`, and the skill should present those actions as a Traditional Chinese numbered menu instead of acting like a passive command relay.

Project-specific materials belong to the target repository overlay:

- `.qa-aist.yaml`
- `.qa-aist-project/cases/`
- `.qa-aist-project/runners/`
- `.qa-aist-project/rules/`
- `.qa-aist-project/state/`
- `.qa-aist-project/evidence/`
- `.qa-aist-project/reports/`

Read next:

- `PROJECT_BOUNDARY.md` — what can and cannot be committed here.
- `ARCHITECTURE.md` — deterministic-first workflow and component model.
- `SWQA_TEST_DESIGN.md` — reusable SWQA knowledge for bug-pattern expansion, CLI matrices, and boundary tests.
- `COMMANDS.md` — CLI command surface.
- `CONFIGURATION.md` — host-project configuration model.
- `SECURITY.md` — secret and tracker-write policy.
