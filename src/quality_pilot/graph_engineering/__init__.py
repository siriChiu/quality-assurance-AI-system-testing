"""Knowledge-graph engineering primitives for AI Quality Pilot.

The module is the memory/provenance half of Graph Engineering.  The Task Graph executor
orchestrates these stages, while this package validates and stores typed graph facts.  Its
QA adapter projects existing Quality Pilot contracts, runs, evidence, and pinned reviews;
the graph remains a read model rather than a replacement source of truth.
"""

from .model import (
    GRAPH_SCHEMA,
    ONTOLOGY_SCHEMA,
    GraphEntity,
    GraphEvent,
    GraphRelation,
    GraphValidationError,
    Ontology,
    Provenance,
    load_ontology,
    validate_ontology,
)
from .paths import GraphPaths, graph_paths
from .qa_adapter import (
    QAAdapterError,
    QA_CANDIDATE_SCHEMA,
    build_qa_candidate_snapshot,
    prepare_qa_candidate_snapshot,
    write_qa_candidate_snapshot,
)
from .pipeline import (
    graph_evaluate,
    graph_extract,
    graph_fuse,
    graph_ontology,
    graph_quality_gate,
    graph_representation,
    graph_scope,
    graph_serve,
    graph_status,
    graph_tutor,
)
from .review_adapter import REVIEW_PROJECTION_SCHEMA, ReviewAdapterError, load_review_artifact
from .store import GraphStore
from .workflow import run_graph_task_graph
from ..task_graph import compile_graph_engineering_task_graph

__all__ = [
    "GRAPH_SCHEMA",
    "ONTOLOGY_SCHEMA",
    "GraphEntity",
    "GraphEvent",
    "GraphRelation",
    "GraphStore",
    "GraphValidationError",
    "GraphPaths",
    "QAAdapterError",
    "ReviewAdapterError",
    "REVIEW_PROJECTION_SCHEMA",
    "load_review_artifact",
    "QA_CANDIDATE_SCHEMA",
    "build_qa_candidate_snapshot",
    "prepare_qa_candidate_snapshot",
    "write_qa_candidate_snapshot",
    "Ontology",
    "Provenance",
    "graph_evaluate",
    "graph_extract",
    "graph_fuse",
    "graph_ontology",
    "graph_paths",
    "graph_quality_gate",
    "graph_representation",
    "graph_scope",
    "graph_serve",
    "graph_status",
    "graph_tutor",
    "run_graph_task_graph",
    "compile_graph_engineering_task_graph",
    "load_ontology",
    "validate_ontology",
]
