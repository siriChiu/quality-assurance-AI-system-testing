from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import ProjectConfig


@dataclass(frozen=True)
class GraphPaths:
    root: Path
    workspace: Path
    state: Path
    database: Path
    json_export: Path
    scope: Path
    representation: Path
    ontology: Path
    extraction: Path
    stages: Path
    fusion_plan: Path
    evaluation: Path

    def as_dict(self) -> dict[str, Path]:
        return {
            "root": self.root,
            "workspace": self.workspace,
            "state": self.state,
            "database": self.database,
            "json_export": self.json_export,
            "scope": self.scope,
            "representation": self.representation,
            "ontology": self.ontology,
            "extraction": self.extraction,
            "stages": self.stages,
            "fusion_plan": self.fusion_plan,
            "evaluation": self.evaluation,
        }


def graph_paths(config: ProjectConfig) -> GraphPaths:
    workspace = config.paths.workspace / "graph"
    state = config.paths.state / "graph"
    return GraphPaths(
        root=config.root,
        workspace=workspace,
        state=state,
        database=state / "knowledge.sqlite3",
        json_export=state / "knowledge-graph.json",
        scope=state / "scope.json",
        representation=state / "representation.json",
        ontology=config.paths.rules / "graph-ontology.yaml",
        extraction=state / "extraction",
        stages=state / "stages",
        fusion_plan=state / "fusion-plan.json",
        evaluation=state / "evaluation.json",
    )
