@current @supported @graph-engineering @knowledge-graph
Feature: Modular Knowledge Graph engineering inside Quality Pilot
  The Knowledge Graph half models what QA agents remember, while the Task Graph
  half controls how the stages execute. Graph state is provenance-backed local
  projection; source systems remain authoritative.

  @current @supported @graph-scope @provenance
  Scenario: Scope requires competency questions before ontology work
    Given a fresh graph project
    When graph scope is requested without a competency question
    Then the graph scope status is BLOCK
    And graph scope does not invent a question

  @current @supported @graph-ontology @schema
  Scenario: Ontology validation enforces typed relation domains and ranges
    Given a fresh graph project with a valid scope
    When the starter graph ontology is validated
    Then the graph ontology status is READY
    And the graph ontology has typed relations

  @current @supported @graph-extraction @provenance
  Scenario: Extraction rejects a fact without provenance evidence
    Given a valid graph ontology and a candidate entity without evidence
    When the graph candidate is extracted
    Then the graph extraction status is BLOCK
    And the graph remains without that entity

  @current @supported @graph-quality-gate @evaluation
  Scenario: Structural graph checks do not become quality PASS without gold labels
    Given a graph with a provenance-backed entity
    When the graph quality gate runs without adjudicated labels
    Then the graph quality gate status is HOLD
    And the reason is gold_labels_required

  @current @supported @graph-fusion @human-gate
  Scenario: Fusion is previewed before a reversible human-approved merge
    Given a graph with two exact duplicate entities
    When the graph fusion plan runs without confirmation
    Then the graph fusion status is HOLD
    And the reason is human_fusion_approval_required

  @current @supported @graph-serving @read-only
  Scenario: Serving returns a provenance-preserving read-only subgraph
    Given a graph with a linked relation
    When the graph serves one hop from the source entity
    Then the graph serving status is PASS
    And every served relation has provenance

  @current @supported @graph-task-graph @checkpoint
  Scenario: The nine-stage graph workflow is compiled as a Task Graph
    When the Knowledge Graph Task Graph is compiled
    Then entity extraction fans out before relation and event extraction
    And fusion has an explicit human gate
    And the graph workflow has a checkpoint contract

  @current @supported @integration @qa-artifacts
  Scenario: Existing Quality Pilot artifacts feed the graph read model
    Given a clean Quality Pilot project with a canonical case run
    When the QA graph adapter projects existing cases runs and evidence
    Then the graph candidate source mode is quality_pilot_canonical_artifacts
    And the graph candidate contains a TestCase, TestRun, and Evidence
