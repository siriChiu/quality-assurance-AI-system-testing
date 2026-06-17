# SWQA test-design rule

AI Quality Pilot treats each confirmed bug as a reusable failure pattern, not as a
single reproduction command. A fix is not complete until deterministic tests
prove the original failure, adjacent inputs, invalid inputs, and safe no-op
smoke paths.

## Required expansion for every new bug

```yaml
bug_pattern:
  exact_reproduction:
    required: true
    evidence: failing automated test or side-effect-safe CLI repro
  sibling_surface_scan:
    required: true
    question: which commands/features share the same parser, validator, state, or transport path?
  negative_cases:
    required: true
    include: invalid values, missing values, duplicate/conflicting flags, and documented disable modes
  boundary_values:
    required: true
    include: zero, negative, minimum positive, default, maximum/huge values, empty strings, and values that look like flags
  side_effect_safe_smoke:
    required: true
    examples: --help, --version, dry-run, parser-only fixtures, or explicit no-op fakes
```

## CLI argument-order matrix

For any CLI parser or command contract change, cover these dimensions before
marking PASS:

```yaml
cli_argument_order_matrix:
  flag_scope:
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
    - path_value
    - url_value
    - duration_or_number_boundary
  assertions:
    - contextual_help_stays_contextual
    - local_flags_are_not_mistaken_for_global_flags
    - global_flags_do_not_steal_command_flag_values
    - positional_arguments_do_not_hide_later_local_flags_unless_the_contract_rejects_that_shape
```

## Boundary and invalid-value tests

Do not assume parser acceptance means semantic validity.

```yaml
validation_matrix:
  durations:
    valid: [minimum_positive, default]
    invalid_when_retry_enabled: [zero, negative]
    note: use an explicit disable flag or max-attempts=0; do not let 0s become busy retry
  retry_or_count_flags:
    valid: [0_if_documented_disable, 1, default]
    invalid: [negative]
  booleans:
    valid: [present, absent, explicit_true_false_when_supported]
    invalid: [ambiguous_or_conflicting_forms]
```

## PASS gate

A SWQA PASS for a bug fix requires:

1. the exact old failure is reproduced first;
2. the fix is verified through the real user-facing interface;
3. sibling commands/features sharing the same pattern are checked;
4. boundary and invalid-value cases are explicitly listed;
5. evidence is real and safe to share; and
6. any remaining untested risk is reported as HOLD, not hidden as PASS.
