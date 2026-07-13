# AI Quality Pilot documentation

AI Quality Pilot is a reusable Hermes dynamic skill backed by a deterministic
engine. It is not yet a fully autonomous collection of resumable agent modules.
Start with the [capability matrix](CAPABILITY_MATRIX.md) so Supported, Partial,
and Planned behavior are not confused.

Hermes integration is guided and interactive: typing `/quality-pilot` in the
input box now opens an input-time completion dropdown before Enter (the first
row preserves the bare overview, followed by nested workflows such as
`cases generate`, `publish wiki status`, and `close-loop heartbeat`). Leaf
options such as `--init` and `--growing` are suggested as well. Bare `/quality-pilot` opens the
Traditional Chinese overview and menu, while `/quality-pilot ...` responses
include `next_actions`; the skill should present those actions as a Traditional
Chinese numbered menu instead of acting like a passive command relay. When user
input is required, the payload includes `hermes_needs_input`;
Hermes should call `clarify` only for facts the repo, config, tracker snapshots,
and existing state cannot establish.

For any workflow that involves requirements, issue analysis, test design,
case generation/review/run, close-loop growth, publishing, tracker work, or
subagent work, Hermes now enforces the installed `grill-me` companion as a
blocking system-level preflight. The agent executes the interview itself (the
user does not need to type `/grill-me`), waits for answers, and carries them
into the subsequent Quality Pilot command. Only explicitly listed deterministic
read-only commands bypass this gate; a missing companion stops with
`grill_me_required`.

## Choose your journey

- **Local repo QA:** follow the local-only happy path in
  [Command Surface](COMMANDS.md#local-repo-qa). Missing Gitea/Redmine readiness
  does not prevent local analysis, case work, or reports; it blocks only the
  corresponding remote integration.
- **Redmine/Gitea issue QA:** start with the issue-driven path in
  [Command Surface](COMMANDS.md#issue-driven-qa). Hermes performs MCP snapshot
  handoffs; the user should not manually translate Redmine, Gitea, and case IDs.
- **Developer fix/retest:** use the fix path in
  [Command Surface](COMMANDS.md#developer-fix-and-retest). PR creation remains
  gated on verification evidence.
- **Scheduled growth:** read [Heartbeat and external scheduling](HEARTBEAT_CRON.md).
  Heartbeat is one tick; an external scheduler must invoke future ticks.

## Product truth and test depth

- [Capability matrix](CAPABILITY_MATRIX.md) — what is Supported, Partial, and Planned.
- [Architecture](ARCHITECTURE.md) — deterministic pipeline, component boundaries, and truth axes.
- [SWQA test design](SWQA_TEST_DESIGN.md) — current structured assertions and stratified init selection, plus explicitly planned deeper strategies.
- [Command surface](COMMANDS.md) — task-oriented journeys and CLI reference.
- [Configuration](CONFIGURATION.md) — host-project config, integrations, and local-versus-remote behavior.

## Install, operate, and contribute

- [Hermes Agent Install](HERMES_AGENT_INSTALL.md) — install and verify the dynamic skill.
- [Heartbeat and external scheduling](HEARTBEAT_CRON.md) — safe Hermes/cron operation.
- [Security](SECURITY.md) — secrets and tracker-write policy.
- [Repository boundary](PROJECT_BOUNDARY.md) — what belongs in this tool repo versus a product overlay.

The two implementation plans mix current evidence with proposed work. They are
roadmaps, not user-facing runtime contracts:

- [Agent Close Loop Improvement Plan](AGENT_CLOSE_LOOP_IMPROVEMENT_PLAN.md)
- [Quality Pilot UX Hardening Tasks](QUALITY_PILOT_UX_HARDENING_TASKS.md)

Project-specific runtime materials belong to the target repository overlay:

- `.quality-pilot.yaml`
- `.quality-pilot-project/cases/`
- `.quality-pilot-project/runners/`
- `.quality-pilot-project/rules/`
- `.quality-pilot-project/state/`
- `.quality-pilot-project/evidence/`
- `.quality-pilot-project/reports/`
