# Capability matrix

This page is the product-truth index for the current checkout. Read it before
interpreting a design principle or roadmap item as an enforced capability.

Status meanings:

- **Supported** — implemented in the current checkout and intended for normal use.
- **Partial** — a useful slice exists, but important gates or workflow contracts are incomplete.
- **Planned** — design direction only; do not treat it as runtime behavior.

## Current capabilities

| Area | Status | Current contract | Important limit |
|---|---|---|---|
| Hermes dynamic skill and deterministic dispatcher | Supported | `/quality-pilot ...` dispatches into the local engine and returns structured next actions | This is a dynamic skill, not a native Hermes router or an autonomous background service |
| Local setup, runtime discovery, doctor, and state audit | Supported | Repo analysis runs before missing-input questions; `audit state` checks cross-file truth | External targets, credentials, fixtures, and side-effect boundaries still require user-owned facts |
| Explicit local/remote environment profile and execution preflight | Supported, first slice | `environment status/configure` records non-secret readiness; prepared cases BLOCK on missing environment, fixture, credential, target, or executable | Legacy configs without the new fields are compatibility-ready until migrated; deep lab adapters remain project-owned |
| Init case generation | Supported, first stratified slice | Initial selection is stratified across available operation/dimension strata instead of filling the budget from one repeated surface; rejection probes distinguish validation evidence from infrastructure failure | It is not complete white-box, black-box, mutation, API, UI, security, or performance coverage |
| Growing case generation | Supported, first operation-matrix slice | Uses repo/issue/change/run signals and bounded operation variants; duplicate commands do not consume the new-case budget; external assertions and claim IDs are normalized before write | Candidate review, deep code-path grounding, and semantic coverage scoring remain partial |
| Structured command assertions | Supported, contract v2 first slice | Commands can evaluate exit status, stdout/stderr contains/regex/equals assertions, and duration bounds; results preserve assertion IDs, `oracle_results`, strength, and partial state | Rich domain oracles such as database state, distributed traces, UI state, business invariants, and mutation score remain planned |
| Evidence and contract-hash consistency | Supported | Runs persist evidence and current-contract identity; audit can reject stale or mismatched evidence | A command-level PASS is not by itself proof that an issue or release is ready |
| Four-axis truth model | Supported, first slice | Workflow progress, official test outcome, gate decision, and component health are separate; partial observations use auxiliary `probe_outcome`, and partial-only runs HOLD | Older overlays can require regeneration or repair before these fields are available |
| Redmine/Gitea issue sync and canonical mapping | Partial | Full Redmine snapshots, local mirrors, mapping, and gated Gitea handoffs exist | Freshness/reconciliation and module-level retry/idempotency are not complete in every path |
| Issue report, failure-derived issue handoff, fix handoff, PR linkage, and Wiki handoff | Partial | `create-from-failure --local` writes a complete local report; `--remote` also preserves that local report while emitting a gated, redacted, standalone-readable SWQA issue body; linked evidence uses gated remote-write payloads | Full post-fix retest orchestration and remote result reconciliation remain incomplete |
| Independent A0-A8 agent modules | Partial | Public commands provide separable workflow entry points | A common resumable module-result contract and session state are still planned |
| `close-loop run-once` | Partial | Runs the current deterministic pipeline for a selected scope | It is not yet the complete one-command autonomous A0-A8 loop |
| `close-loop heartbeat` | Supported, single tick | One invocation senses growth, executes newly created or explicitly selected safe work, persists heartbeat state, and can return `idle`; a blocked issue sensor stops the tick and raises an alert | It does not install or run its own timer; Hermes or an external scheduler must invoke every tick |
| Deterministic issue/release PASS/HOLD gate | Partial | Contract/evidence truth checks exist and the four truth axes prevent several false-ready states | Full risk-based sibling, boundary, residual-risk, and deep-coverage enforcement is not complete |
| Deep white-box/black-box, mutation, fuzz, load/soak, security, UI, and distributed-system strategies | Planned | The test-design model reserves these as strategy families | Do not claim these are automatically generated or enforced today |

## Interpreting PASS

In the current first slice, distinguish four questions:

1. `workflow_status` — where is the lifecycle now?
2. `test_outcome` — what did the executed assertion set observe?
3. `gate_status` — may the next side effect or publication proceed?
4. `health_status` — was component/integration health independently evaluated?

`probe_outcome` separately preserves partial-probe observations. If every result
is partial, official `test_outcome` is `HOLD`, not `PASS`.
The close-loop runner currently marks `health_status: NOT_EVALUATED` because its
health-check stage is owned by `doctor`; it does not derive health from QA PASS.

For example, a command may have `test_outcome: PASS` while the workflow is
`BLOCKED` because coverage is shallow, a fixture is missing, evidence
is stale, or a remote-write gate has not passed. Do not collapse these axes into
one green status.

## Product direction versus current behavior

The following documents describe design direction as well as current work:

- `AGENT_CLOSE_LOOP_IMPROVEMENT_PLAN.md`
- `QUALITY_PILOT_UX_HARDENING_TASKS.md`

Items marked proposed, planned, pending, or unchecked there are not runtime
contracts. Public behavior is defined by this matrix, `COMMANDS.md`,
`CONFIGURATION.md`, and the dispatcher output for the installed version.
