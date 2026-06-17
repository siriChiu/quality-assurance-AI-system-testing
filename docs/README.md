# AI Quality Pilot documentation

AI Quality Pilot is a reusable tool. Documentation in this repository must describe the product, interfaces, and generic templates only.

Hermes integration is guided and interactive: `/quality-pilot ...` responses include `next_actions`, and the skill should present those actions as a Traditional Chinese numbered menu instead of acting like a passive command relay. When user input is required, the payload includes `hermes_needs_input`; Hermes should call `clarify`.

Project-specific materials belong to the target repository overlay:

- `.quality-pilot.yaml`
- `.quality-pilot-project/cases/`
- `.quality-pilot-project/runners/`
- `.quality-pilot-project/rules/`
- `.quality-pilot-project/state/`
- `.quality-pilot-project/evidence/`
- `.quality-pilot-project/reports/`

Read next:

- `PROJECT_BOUNDARY.md` — what can and cannot be committed here.
- `ARCHITECTURE.md` — deterministic-first workflow and component model.
- `SWQA_TEST_DESIGN.md` — reusable SWQA knowledge for bug-pattern expansion, CLI matrices, and boundary tests.
- `COMMANDS.md` — CLI command surface.
- `CONFIGURATION.md` — host-project configuration model.
- `SECURITY.md` — secret and tracker-write policy.
