"""Ontology rebuild workflow — parameters and flow ID constant.

Orchestration is handled by the Airflow DAG definition in dags/ontology_rebuild.py.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass

FLOW_ID = "ontology-rebuild"


@dataclass
class OntologyRebuildParams:
    force: bool = False
