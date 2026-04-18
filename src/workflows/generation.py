"""Generation workflow — parameters.

Orchestration is handled by the Airflow DAG definition in dags/generation.py.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass


@dataclass
class GenerationParams:
    dataset_urn: str
