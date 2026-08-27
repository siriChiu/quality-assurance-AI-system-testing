Feature: Context and Contract boundaries
  As a task-graph compiler
  I want every node to receive scoped context and satisfy an explicit contract
  So that agents cannot treat unverified data as truth or silently omit outputs

  Background:
    Given a deterministic Task Graph runtime

  Rule: Context packets

    @current @supported @task-graph @context
    Scenario: A node receives only its declared context keys
      Given a context packet contains requirements, source_authority, policy, and private_unrequested_fact
      And a task node declares requirements and policy as its context keys
      When the node context is projected
      Then the projected context contains requirements and policy
      And the projected context does not contain private_unrequested_fact

    @current @supported @task-graph @security
    Scenario: Raw secret-like context is rejected fail-closed
      Given a context packet contains a raw password value
      When the context packet is created
      Then the task graph returns context_redaction_failed_closed
      And no task execution is started

  Rule: Node contracts

    @current @supported @task-graph @contract
    Scenario: A node contract rejects missing required output
      Given a task node contract requires verified_result
      When the node returns an output without verified_result
      Then deterministic validation returns a contract failure
      And downstream nodes are not allowed to run

    @current @supported @task-graph @contract
    Scenario: Equivalent task contracts produce a stable contract hash
      Given two task graphs have equivalent node contracts and dependencies
      When their contract hashes are calculated
      Then the hashes are equal
      And the hash is persisted with the execution checkpoint

    @planned @task-graph @contract
    Scenario: A generated contract is reviewed before becoming runnable
      Given an agent proposes a task contract from incomplete requirements
      When the contract review runs
      Then missing context, output, validator, owner, and side-effect fields are reported
      And the contract cannot be scheduled until a human accepts it

    @planned @contract @review @ux
    Scenario: Discovery candidates are promoted through one explicit confirmation
      Given README, --help, repository analysis, and existing tests produce candidate commands and Browser steps
      When Quality Pilot builds an effective product execution contract
      Then candidate source references, confidence, execution target, oracle, and side-effect boundary are shown
      And no candidate is treated as official QA truth before confirmation
      And one confirmation writes the normalized contract and its hash
      And later execution reads only the normalized contract

    @planned @contract @review @remote @provenance
    Scenario: Local and remote execution fields cannot be silently merged
      Given a contract has a local review worktree and a remote product target
      When the contract is normalized
      Then local_pytest, remote_product_runner, and playwright_target are separate fields
      And each case receives exactly one execution target
      And the contract hash includes the target and source identity policy
      And a local absolute path is never used as a remote command path
