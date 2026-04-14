"""Generation workflow — parameters and flow ID constant.

Orchestration is handled by the Airflow DAG definition in dags/generation.py.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass

FLOW_ID = "generation"


@dataclass
class GenerationParams:
    dataset_urn: str
