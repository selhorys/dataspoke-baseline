"""Unit tests for ontology workflow params."""

from src.workflows.ontology import OntologyRebuildParams


def test_params_defaults():
    params = OntologyRebuildParams()
    assert params.force is False


def test_params_force():
    params = OntologyRebuildParams(force=True)
    assert params.force is True
