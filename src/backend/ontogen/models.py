"""Pydantic output models for the Ontogen LLM inference pipeline.

Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
"""

from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, constr, model_validator

from src.backend.ontogen.slug import to_snake

# LLM-controlled string used as a node/edge/triple id reference
_BoundedId = Annotated[str, StringConstraints(max_length=200)]

# LLM-controlled URN string in dataset_urns
_BoundedUrn = Annotated[str, StringConstraints(max_length=1024)]


class OntogenLLMNode(BaseModel):
    id: str | None = Field(default=None, max_length=200)  # optional hint; service always re-slugs from name
    name: constr(strip_whitespace=True, min_length=1, max_length=200)  # type: ignore[valid-type]
    description: constr(max_length=4000) = ""  # type: ignore[valid-type]
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    dataset_urns: list[_BoundedUrn] = Field(default_factory=list, max_length=100)


class OntogenLLMEdge(BaseModel):
    id: str | None = None
    label: constr(strip_whitespace=True, min_length=1, max_length=200)  # type: ignore[valid-type]
    semantics: constr(max_length=4000) = ""  # type: ignore[valid-type]
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)


class OntogenLLMTriple(BaseModel):
    subject_node_id: _BoundedId = Field(..., max_length=200)
    edge_id: _BoundedId = Field(..., max_length=200)
    object_node_id: _BoundedId = Field(..., max_length=200)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)


class OntogenLLMOutput(BaseModel):
    nodes: list[OntogenLLMNode] = Field(default_factory=list, max_length=500)
    edges: list[OntogenLLMEdge] = Field(default_factory=list, max_length=500)
    triples: list[OntogenLLMTriple] = Field(default_factory=list, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def _normalize_ids(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        node_id_remap: dict[str, str] = {}
        for node in values.get("nodes", []) or []:
            if not isinstance(node, dict):
                continue
            orig = node.get("id")
            if orig and isinstance(orig, str):
                norm = to_snake(orig)
                node_id_remap[orig] = norm
                node["id"] = norm

        edge_id_remap: dict[str, str] = {}
        for edge in values.get("edges", []) or []:
            if not isinstance(edge, dict):
                continue
            orig = edge.get("id")
            if orig and isinstance(orig, str):
                norm = to_snake(orig)
                edge_id_remap[orig] = norm
                edge["id"] = norm

        for triple in values.get("triples", []) or []:
            if not isinstance(triple, dict):
                continue
            subj = triple.get("subject_node_id")
            if subj in node_id_remap:
                triple["subject_node_id"] = node_id_remap[subj]
            eid = triple.get("edge_id")
            if eid in edge_id_remap:
                triple["edge_id"] = edge_id_remap[eid]
            obj = triple.get("object_node_id")
            if obj in node_id_remap:
                triple["object_node_id"] = node_id_remap[obj]

        return values
