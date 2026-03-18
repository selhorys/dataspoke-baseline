"""Unit tests for ontology workflow params and flow ID."""

from src.workflows.ontology import FLOW_ID, OntologyRebuildParams


def test_flow_id():
    assert FLOW_ID == "ontology-rebuild"


def test_params_defaults():
    params = OntologyRebuildParams()
    assert params.force is False


def test_params_force():
    params = OntologyRebuildParams(force=True)
    assert params.force is True
