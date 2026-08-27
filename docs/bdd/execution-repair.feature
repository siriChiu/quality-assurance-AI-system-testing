Feature: Task execution, checkpoint, and targeted repair
  As a workflow operator
  I want deterministic validation and durable checkpoints
  So that a local failure can be repaired without restarting successful work

  Background:
    Given a compiled Quality Pilot task graph
    And a scoped canonical context packet

  Rule: Deterministic execution

    @current @supported @task-graph @execution
    Scenario: A failed node stops its downstream tasks
      Given the execution node returns an output that fails its contract validator
      When the task graph executor runs
      Then that node is marked FAIL
      And its downstream nodes are marked SKIPPED
      And the executor does not claim the workflow passed

    @current @supported @task-graph @execution
    Scenario: Independent workers execute in a bounded parallel layer
      Given a compiled Quality Pilot task graph
      When the task graph executor runs with two workers
      Then both independent case workers overlap
      And the executor records their outputs before the merge node

    @current @supported @task-graph @execution
    Scenario: A missing prerequisite produces BLOCK
      Given a node requires a context fact that is not in its scope
      When the task graph executor runs
      Then that node is marked BLOCK
      And no product-side task is executed

    @current @supported @task-graph @integration @checkpoint
    Scenario: Default close-loop mode persists before the human gate
      Given a clean Quality Pilot project with the example contract
      When the default close-loop mode runs without publish confirmation
      Then the Task Graph execution returns HOLD at the human gate
      And its durable checkpoint contains the graph contract hash

    @current @supported @task-graph @integration @checkpoint
    Scenario: Explicit close-loop Task Graph mode persists before the human gate
      Given a clean Quality Pilot project with the example contract
      When the explicit close-loop Task Graph mode runs without publish confirmation
      Then the Task Graph execution returns HOLD at the human gate
      And its durable checkpoint contains the graph contract hash

    @current @supported @task-graph @integration
    Scenario: Legacy close-loop mode is an explicit fallback
      Given a clean Quality Pilot project with the example contract
      When the legacy close-loop mode runs
      Then the close-loop execution mode is legacy
      And the legacy close-loop status is PASS

    @current @supported @task-graph @checkpoint
    Scenario: A checkpoint resumes without rerunning passed nodes
      Given context.build and contract.compile are already PASS in a checkpoint
      When execution resumes from that checkpoint
      Then passed nodes are not rerun
      And the next unresolved node receives the same graph contract hash

    @current @supported @task-graph @repair
    Scenario: Targeted repair invalidates only the failed node and descendants
      Given execute:CASE-001 failed while execute:CASE-002 passed
      When repair is requested for execute:CASE-001
      Then execute:CASE-001 and its descendants return to PENDING
      And execute:CASE-002 remains PASS
      And the repair round is recorded in the checkpoint

    @planned @task-graph @repair
    Scenario: A repaired node resumes from its last verified predecessor
      Given a repair node produces a valid replacement artifact
      When deterministic validation passes
      Then only the repaired branch resumes
      And unrelated branches remain unchanged
