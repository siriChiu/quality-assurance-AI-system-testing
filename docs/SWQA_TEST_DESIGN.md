# SWQA test-design knowledge

status: reusable
scope: generic AI Quality Pilot test design

This document contains both current executable contracts and the deeper test
strategy AI Quality Pilot is growing toward. The current checkout supports a
first slice of structured command assertions, four-axis truth, and stratified
init selection. Complete white-box/black-box, mutation, fuzz, security,
UI/browser, API-schema, load/soak, and distributed-system coverage is **Planned**,
not implied by generated dimension labels. See
[`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md).

The target is to model how an experienced SWQA engineer expands one confirmed
bug into a reusable test pattern. The goal is not only to prove the exact fix;
it is to expose sibling failures that share the same parser, validator, state
transition, transport path, or safety boundary.

## Core principle

```yaml
swqa_principle:
  exact_bug_reproduction: required
  pattern_expansion: required
  sibling_surface_scan: required
  boundary_and_invalid_values: required
  side_effect_safe_evidence: required
  pass_without_explicit_risk_list: forbidden
```

This is the target issue-level principle. The current engine enforces structured
command assertions and several evidence-consistency gates, but complete
risk-based issue/release PASS/HOLD enforcement is Partial. Therefore a
command-level `test_outcome: PASS` must not be summarized as complete issue or
release readiness.

## Supported first slice: structured command assertions

A contract can attach machine-evaluable assertions to each command. Public
contracts should prefer `assertions`; `oracles` is accepted as a compatibility
alias. `expected_exit_code` remains supported and is converted into an effective
exit assertion when no explicit exit assertion is present.

```yaml
commands:
  - id: rejected_invalid_option
    run: "${QUALITY_PILOT_BINARY} inspect --definitely-invalid"
    expected_exit_code: 2
    assertions:
      - id: exit_rejected
        type: exit_code
        operator: equals
        expected: 2
      - id: invalid_message
        type: stderr
        operator: contains
        expected: "unknown option"
      - id: usage_context
        type: stderr
        operator: regex
        expected: "(?i)usage|invalid"
      - id: bounded_rejection
        type: duration_ms
        operator: less_than_or_equal
        expected: 2000
```

Supported assertion types and operators:

| Type | Operators | Expected value |
|---|---|---|
| `exit_code` | `equals` | integer |
| `stdout` | `contains`, `regex`, `equals` | string |
| `stderr` | `contains`, `regex`, `equals` | string |
| `duration_ms` | `less_than`, `less_than_or_equal` | non-negative number |

Assertion `id` is optional but recommended for traceability. Invalid assertion
schemas and invalid regular expressions fail contract loading. Each executed
command records `duration_ms`, `oracle_results`, `oracle_strength`, and
`oracle_partial`; oracle results include the assertion id, expected value,
redacted actual evidence, source, status, and mismatch reason. The case result
also summarizes oracle strength/partial state. An assertion can fail even when
the process exit code is zero.

This is only the first oracle slice. Database state, HTTP/schema validation,
files, distributed traces, UI state, business invariants, resource curves, and
custom project runners remain future oracle families.

## Supported first slice: four-axis truth

Current run summaries keep these fields separate:

```yaml
workflow_status: PLANNED | COMPLETED | BLOCKED | FAILED
test_outcome: NOT_RUN | PASS | FAIL | BLOCK | HOLD | ABORT
gate_status: NOT_EVALUATED | ALLOWED | BLOCKED
health_status: NOT_EVALUATED | HEALTHY | DEGRADED | UNHEALTHY
probe_outcome: NOT_RUN | PASS | FAIL | BLOCK | HOLD  # auxiliary partial-probe result
```

- `workflow_status` answers where the lifecycle is.
- `test_outcome` answers what the assertion set observed.
- `gate_status` answers whether a gated next action may proceed.
- `health_status` answers whether component/integration health was independently
  evaluated. The current close-loop runner uses `NOT_EVALUATED`; it does not
  infer health from QA PASS or a write gate.
- `probe_outcome` preserves partial observations without promoting them to the
  official test outcome.

These axes intentionally allow states such as `test_outcome: PASS` with
`gate_status: BLOCKED`, or `test_outcome: NOT_RUN` with
`workflow_status: PLANNED`. The legacy top-level `status` may still exist for
compatibility; reports and agents should use the four explicit fields when
available. If every selected case is an `oracle_partial`/partial probe, the
official `test_outcome` is `HOLD`; its observed result is recorded separately in
`probe_outcome`.

## Bug-to-pattern workflow

Target policy: for every new confirmed bug, create a reusable knowledge card
before closing QA. Bug-pattern-card persistence and its complete issue-level
gate are still Planned:

```yaml
bug_pattern_card:
  trigger_class: parser | validator | state | transport | permission | data_model | concurrency | reporting
  exact_repro:
    command_or_steps: required
    expected_before_fix: must fail or expose wrong behavior
    side_effect_control: help/version/dry-run/fake fixture/no-op target when possible
  shared_surfaces:
    required_question: which sibling commands/features share this mechanism?
    minimum_action: list checked siblings or mark HOLD with reason
  equivalence_classes:
    valid: required
    invalid: required
    ambiguous: required when user input grammar allows ambiguity
  boundary_values:
    include: [zero, negative, minimum_positive, default, maximum_or_huge, empty, duplicate]
  regression_tests:
    unit_or_contract: required
    user_facing_smoke: required
  issue_evidence:
    real_evidence_only: true
    unconfirmed_claims: forbidden
```

## Init and growing case generation

`/quality-pilot cases generate` requires an explicit mode so users do not confuse first-time test design with incremental growth.

`/quality-pilot cases generate --init` is the first-time repo SWQA map. It scans
README, code inventory, package metadata, existing runners, existing cases, and
project rules. Its first supported stratified selector puts one candidate from
each available init operation stratum at the front of the pool before adding the
remaining seed-by-stratum matrix:

```text
functional primary -> positive smoke -> negative invalid input
-> boundary invalid value -> stress/timeout baseline -> sibling consistency
```

This means a small `--count` is less likely to be filled by one functional/help
surface. It does not prove each generated command deeply realizes the named
dimension. Generated contract v2 records `quality_pilot.swqa_operation`,
`quality_pilot.oracle_strength`, `quality_pilot.dimension_realization`, and
top-level `coverage_claims`. Operation wrappers emit a semantic marker when they
can support a structured oracle; an exit-only surface probe remains
`oracle_partial` rather than deep coverage. Each dimension is evaluated
separately: only a dimension tied to an actual non-exit assertion may be
`realized`; adjacent labels remain `partial` or `planned`.

Generated negative and boundary wrappers do not accept arbitrary non-zero
returns. They require a bounded rejection exit and invalid/usage/option/value
diagnostic evidence, preserve the diagnostic in stdout evidence, and reject
panic/traceback, permission, missing-dependency, command-not-found, and timeout
signatures. This prevents an infrastructure failure from impersonating correct
input validation.

`/quality-pilot cases generate --growing` is the follow-up mode. It observes repo
metadata, code inventory, current Gitea issue mirrors, linked PR references,
recent git commit history, latest run state, reports, existing cases/runners,
and project rules, then creates product-runtime command contracts from fresh
signals. The default growth target is up to 20 cases, with a larger candidate
pool for dedupe and operation selection. Duplicate commands do not consume the
requested new-case budget. Generated count is not a coverage score.

Before a growth candidate becomes YAML, it passes through the current first-slice
SWQA operation matrix. The point is operation diversity; it is not a claim of
complete semantic or code-path coverage.

Externally supplied candidates are normalized before Contract v2 is written.
Their assertions, exit-code consistency, regexes, IDs, and coverage references
must match runner-effective assertion IDs; malformed or untraceable claims are
rejected before a case file is created.

```yaml
swqa_growth_operations:
  surface_probe: read-only product-runtime command that proves the surface is reachable
  invalid_option_rejection: product-runtime command with an injected invalid option and an oracle that expects rejection
  boundary_invalid_value: product-runtime command with a safe invalid boundary value and an oracle that expects rejection
  sibling_help_sweep: grouped sibling surfaces that share a parser or feature family
  repeatability_probe: repeated side-effect-safe command to catch state leakage or flaky output paths
  concurrency_probe: bounded parallel side-effect-safe command to catch shared-state or lock handling issues
  timeout_baseline: bounded timeout wrapper around a safe command to catch hangs
  monkey_help_sweep: bounded sweep of documented help/version surfaces
  monkey_repeatability: repeated monkey sweep inside the same safe envelope
  monkey_concurrency: concurrent monkey sweep inside the same safe envelope
```

Monkey-style growth is allowed only inside a bounded safe envelope. The first supported monkey sensor is `monkey_cli_help_sweep`, which groups documented CLI help/version surfaces and runs them through the configured product runtime. Destructive random actions, repo-only metadata probes, and unbounded synthetic invalid commands are not monkey tests.

```yaml
growth_generation:
  loop:
    - Observe
    - Normalize
    - Triage
    - Evolve
    - Prune
  six_hats:
    white: facts from repo/issues/PR/latest-run
    red: user-facing risk or pain
    black: regression and side-effect risk
    yellow: value of capturing this as a repeatable contract
    green: sibling surfaces and alternative coverage
    blue: decision such as add_new_tc or update_existing_tc
  default_dimensions:
    - exact_reproduction
    - positive
    - negative
    - boundary
    - invalid_input
    - sibling_surface
    - side_effect_safe
    - stress_timeout_risk
  candidate_budget_rule:
    duplicate_existing_case_or_command: record_as_existing_coverage
    requested_count: limit_new_cases_written_only
    continue_after_duplicate: true
  repo_signals:
    - code_inventory
    - README
    - pyproject_or_package_metadata
    - existing_runners
    - existing_case_contracts
    - issue_snapshot
    - pull_request_references
    - latest_run
    - reports
  if_lab_command_or_fixture_is_unclear:
    generated_case: product_runtime_command_or_needs_input
    review_required_before_run: true_when_safety_or_oracle_is_unproven
    lab_runner_status: advisory_until_configured
  ask_user_for:
    - target_or_feature_surface
    - runner_or_command
    - fixture_or_input_file
    - credential_env_names_only
    - success_criteria
    - side_effect_boundary
```

Do not invent a destructive or credentialed command when only the testing idea is known. Repo-only metadata checks are readiness checks, not generated testcase contracts. If the runtime profile is missing, generation must stop with `needs_input` and write no placeholder case YAML. After runtime confirmation, AI Quality Pilot may generate runnable side-effect-safe commands only through the configured or inferred product entrypoint, such as CLI help/parser/version, dry-run/no-op, or bounded baseline checks. Synthetic invalid subcommands, static repo checks, `python3 -c` metadata checks, `compileall`, `go test`, and `go run` are not testcase commands unless the user explicitly configured them as the product runner. Lab runners can replace or extend the product-runtime command after the user provides fixture, credential env names, and side-effect boundaries.

Hermes may use a separate growth session or agent for broader analysis, but that session may only produce candidate JSON. AI Quality Pilot remains the sole writer of case YAML and must validate schema, dedupe fingerprints, raw-secret leakage, internal prompt leakage, dangerous `.qa` runtime paths, and command fields before writing any contract.

## Target strategy: CLI parser and flag-order matrix

When a bug involves CLI flags, arguments, parser normalization, contextual help,
or command contracts, the target test strategy uses the matrix below. Some
individual operation variants exist today, but complete automatic matrix
expansion and issue-level enforcement are Planned.

```yaml
cli_argument_order_matrix:
  scope:
    - app_or_global_flag
    - command_local_flag
    - same_name_global_and_local_flag
  position:
    - before_command
    - after_command_before_options
    - after_command_options
    - after_positional_argument
    - inline_equals_form
    - short_alias_form
    - after_double_dash_separator_must_not_be_rewritten
  value_shape:
    - normal_value
    - empty_value
    - value_beginning_with_dash
    - url_or_path_value
    - duration_or_number_boundary
  required_assertions:
    - global_flags_are_accepted_where_the_contract_allows
    - local_flags_after_positionals_are_either_supported_or_explicitly_rejected
    - command_flag_values_are_not_stolen_by_global_normalization
    - contextual_help_remains_contextual
    - same_name_global_and_local_flags_do_not_hide_required_parent_values
```

SWQA note: a global-flag fix does not prove local flags are safe. A command-local flag after a positional argument is a separate class and needs its own tests.

## Target strategy: boundary and invalid-value validation

Parser acceptance is not semantic validation. Any option that controls retry, wait time, concurrency, destructive action, or polling must include invalid-value tests.

```yaml
validation_matrix:
  durations:
    valid:
      - minimum_positive
      - default
    invalid_when_feature_enabled:
      - zero
      - negative
    disable_rule: use an explicit disable switch or documented max=0; do not let 0s become implicit busy retry
  counts_or_attempts:
    valid:
      - zero_when_documented_as_disable
      - one
      - default
    invalid:
      - negative
  concurrency:
    valid:
      - one
      - default
      - documented_upper_bound
    invalid:
      - zero_unless_documented
      - negative
  destructive_actions:
    required:
      - no_confirm_rejected_when_multi_target_or_high_risk
      - confirm_accepted_only_when explicitly supplied
      - dry_run_or_no_op_path_verified_when_available
```

## Side-effect-safe CLI repro policy

A QA runner should prefer parser-only or no-op reproductions before touching real systems.

```yaml
safe_repro_order:
  - unit_or_contract_test_for_parser_or_validator
  - --help_or_--version_when_it_exercises_the_parser_path
  - dry_run_with_fixture
  - fake_target_or_mock_service
  - real_target_only_after_scope_and_risk_are explicit
```

If a command can mutate external state, evidence must explain how the run avoided or controlled side effects.

## Target policy: PASS/HOLD decision rule

The rule below defines intended issue-level readiness. Structured command
assertions and evidence consistency are Supported; complete deterministic
enforcement of every listed coverage/risk item is Partial.

```yaml
pass_gate:
  pass:
    requires:
      - exact_old_failure_reproduced
      - deterministic_regression_test_added
      - user_facing_interface_smoke_checked
      - sibling_surfaces_checked
      - boundary_invalid_values_checked
      - evidence_paths_or_outputs_recorded
  hold:
    when:
      - sibling_surfaces_not_checked
      - invalid_values_not_checked
      - evidence_is_manual_or_unrepeatable
      - real_system_risk_requires_user_confirmation
  fail:
    when:
      - confirmed_wrong_behavior_remains
      - fix_only_addresses_symptom
      - parser_accepts_dangerous_invalid_values
```

This document is generic AI Quality Pilot knowledge. Project-specific commands, hosts, credentials, tracker IDs, issue mirrors, and evidence must stay in the host-project overlay, not in the AI Quality Pilot tool repository.
