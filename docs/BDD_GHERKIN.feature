# Quality Pilot BDD contract index.
#
# This file is intentionally small. Responsibility-specific executable contracts
# live under docs/bdd/ and are discovered by the BDD audit.

Feature: Quality Pilot Task Graph contract
  As an engineer running a long-lived AI-assisted QA workflow
  I want context, contracts, dependencies, verification, repair, and gates to be explicit
  So that a failed node has a precise address and the workflow can resume safely

  Background:
    Given the target is a host product repository
    And source authority and user constraints are represented in a canonical context packet
    And every task node has an input/output contract
    And raw secrets are rejected before they enter context, checkpoints, or task outputs

  Rule: BDD contracts are organized by responsibility

    @current @supported @task-graph @docs-contract
    Scenario: The BDD contract is split by responsibility boundary
      When the BDD contract audit runs
      Then it discovers context-contract, task-graph, execution-repair, human-gate, review-comprehensive, remote-product-browser, and knowledge-graph feature files
      And it reports executable bindings separately from planned scenarios
      And an unbound planned scenario is not green evidence
