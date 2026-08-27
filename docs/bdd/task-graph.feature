Feature: Task Graph topology
  As a workflow compiler
  I want edges to represent real data dependencies
  So that independent work can fan out and every irreversible path is controlled

  Background:
    Given a deterministic Task Graph runtime

  Rule: Compilation and dependency topology

    @current @supported @task-graph
    Scenario: The close-loop compiles into explicit task nodes
      Given the selected cases are CASE-001 and CASE-002
      When the Quality Pilot task graph is compiled
      Then it contains context.build, contract.compile, execute:CASE-001, execute:CASE-002, merge.results, gate.publish, and publish.publish
      And every edge names a consumed output
      And the graph has a stable contract hash

    @current @supported @task-graph
    Scenario: Independent case workers share a parallel layer
      Given the selected cases are CASE-001 and CASE-002
      When the Quality Pilot task graph is compiled
      Then execute:CASE-001 and execute:CASE-002 are in the same topological layer
      And neither worker depends on the other worker

    @current @supported @task-graph
    Scenario: A fake edge is rejected
      Given a node depends on another node but does not consume any of its outputs
      When the task graph is validated
      Then validation returns fake_task_edge
      And the graph is not executable

    @current @supported @task-graph
    Scenario: A dependency cycle is rejected
      Given task A depends on task B and task B depends on task A
      When the task graph is validated
      Then validation returns task_graph_cycle
      And no node is scheduled

    @current @supported @task-graph
    Scenario: Multiple nodes cannot write the same artifact
      Given two task nodes both own the same output artifact
      When the task graph is validated
      Then validation returns multiple_task_writers
      And the graph is not executable

  Rule: Verifier and merge topology

    @current @supported @task-graph
    Scenario: Verification uses a separate owner and context scope
      Given a verifier node checks an execution node
      When the task graph is validated
      Then the verifier depends on the execution node
      And the verifier owner differs from the execution owner
      And the verifier context scope differs from the execution context scope

    @planned @task-graph
    Scenario: The stop rule limits agent fan-out and repair rounds
      Given a graph declares a maximum of three rounds and five workers
      When the scheduler attempts a fourth repair round
      Then the scheduler stops with task_graph_stop_rule_exceeded
      And it does not spawn another worker
