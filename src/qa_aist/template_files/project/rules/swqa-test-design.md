# SWQA test-design rule

QA-AIST treats each confirmed bug as a reusable failure pattern, not as a
single reproduction command. A fix is not complete until deterministic tests
prove the original failure, adjacent inputs, invalid inputs, and safe no-op
smoke paths.

## PASS gate

A SWQA PASS for a bug fix requires exact reproduction, sibling-surface checks,
boundary and invalid-value coverage, and shareable evidence.
