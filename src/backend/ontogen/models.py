"""Pydantic output models for the Ontogen LLM inference pipeline.

Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
"""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, constr

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
