"""Ontology rebuild workflow — parameters.

Orchestration is handled by the Airflow DAG definition in dags/ontology_rebuild.py.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass


@dataclass
class OntologyRebuildParams:
    force: bool = False
