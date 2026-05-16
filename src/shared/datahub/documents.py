"""DataHub document-entity reading helpers shared by UC3 (ontogen) and UC4 (metagen).

Spec: spec/DATAHUB_INTEGRATION.md §Document Aspects
      spec/feature/BACKEND.md §Ontology Generation Service
"""

import logging
from typing import Any

from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

# Cap per dataset to keep evidence payloads bounded.
DOCUMENT_EVIDENCE_CAP_PER_DATASET: int = 10


async def fetch_related_documents(
    dataset_urn: str,
    datahub: DataHubClient,
) -> list[dict[str, Any]]:
    """Fetch document entities whose relatedAssets include *dataset_urn*.

    Uses GraphQL ``searchAcrossEntities`` with a ``relatedAssets`` filter.
    Results are sorted by lastModified descending and capped at
    ``DOCUMENT_EVIDENCE_CAP_PER_DATASET``.

    Each entry has: {urn, title, body, related_assets, last_modified}.

    Best-effort — returns [] on failure with a logged WARNING.
    """
    gql = """
    query searchDocumentsByRelatedAsset($input: SearchAcrossEntitiesInput!) {
        searchAcrossEntities(input: $input) {
            searchResults {
                entity {
                    urn
                    ... on Document {
                        info {
                            title
                            contents { text }
                            relatedAssets { asset { urn } }
                            lastModified { time }
                        }
                    }
                }
            }
        }
    }
    """
    variables: dict[str, Any] = {
        "input": {
            "types": ["DOCUMENT"],
            "query": "*",
            "start": 0,
            "count": DOCUMENT_EVIDENCE_CAP_PER_DATASET * 5,
            "orFilters": [{"and": [{"field": "relatedAssets", "values": [dataset_urn]}]}],
        }
    }

    try:
        result = await datahub._with_retry(
            datahub._graph.execute_graphql, gql, variables=variables
        )
        search_results = (result or {}).get("searchAcrossEntities", {}).get("searchResults", [])
        docs: list[dict[str, Any]] = []
        for item in search_results:
            entity = item.get("entity") or {}
            info = entity.get("info") or {}
            contents = info.get("contents") or {}
            related_raw = info.get("relatedAssets") or []
            related_assets = [
                r["asset"]["urn"]
                for r in related_raw
                if r.get("asset") and r["asset"].get("urn")
            ]
            last_modified_ms: int = (info.get("lastModified") or {}).get("time") or 0
            docs.append(
                {
                    "urn": entity.get("urn", ""),
                    "title": info.get("title", ""),
                    "body": contents.get("text", ""),
                    "related_assets": related_assets,
                    "last_modified": last_modified_ms,
                }
            )
        docs.sort(key=lambda d: -(d.get("last_modified") or 0))
        return docs[:DOCUMENT_EVIDENCE_CAP_PER_DATASET]
    except Exception:
        logger.warning(
            "fetch_related_documents_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )
        return []
