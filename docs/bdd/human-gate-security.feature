Feature: Human gates and task-graph safety
  As a QA owner
  I want irreversible actions to require explicit approval
  So that an agent cannot publish, modify a tracker, or push code by implication

  Background:
    Given a compiled Quality Pilot task graph

  Rule: Human gate placement

    @current @supported @task-graph @human-gate
    Scenario: An irreversible task pauses without explicit approval
      Given publish.publish is irreversible and consumes the merged report
      And no approval token exists for publish.publish
      When the task graph executor reaches the human gate
      Then the gate returns HOLD
      And publish.publish does not run

    @current @supported @task-graph @human-gate
    Scenario: Explicit approval unlocks only the gated task
      Given the merged report passed deterministic validation
      And the user approves publish.publish explicitly
      When the task graph executor reaches the human gate
      Then the approval is recorded with the graph contract hash
      And publish.publish may run
      And the approval does not authorize any unrelated task

    @current @supported @task-graph @security
    Scenario: A task graph never promotes its own result to product truth
      Given a task node reports PASS
      When the execution checkpoint is persisted
      Then the checkpoint records node evidence and validator results
      And it does not create PASS, READY, APPROVED, or MERGE_ALLOWED authority

    @planned @task-graph @human-gate
    Scenario: A human gate is placed at the first irreversible boundary
      Given a workflow contains read-only preparation, validation, and remote publication
      When the task graph is compiled
      Then read-only nodes do not require per-node approval
      And the publication boundary has exactly one explicit human gate
