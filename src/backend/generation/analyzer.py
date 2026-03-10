"""Source code analyzer for similar-table diffing and code reference analysis."""

from typing import Any

from src.shared.llm.client import LLMClient


class SourceCodeAnalyzer:
    """Analyzes source code references and computes similar-table diffs."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def analyze(
        self,
        code_refs: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Extract code context and use LLM to produce field-level insights.

        Args:
            code_refs: GitHub repo config (owner, repo, token, path).
            schema_fields: List of field dicts with fieldPath, nativeDataType, description.

        Returns:
            Dict mapping field paths to code-informed descriptions.
        """
        from src.backend.ingestion.extractors import GitHubExtractor

        extractor = GitHubExtractor()
        items = await extractor.extract(code_refs)

        if not items:
            return {}

        code_context_parts: list[str] = []
        for item in items:
            name = item.content.get("name", "")
            path = item.content.get("path", "")
            code_context_parts.append(f"- {path} ({name})")

        field_names = [f.get("fieldPath", "") for f in schema_fields]
        code_context = "\n".join(code_context_parts)

        prompt = (
            f"Given these source code files:\n{code_context}\n\n"
            f"And these schema fields: {field_names}\n\n"
            "For each field, describe what it likely represents in business terms "
            "based on the code context. Return a JSON object mapping field names "
            "to descriptions."
        )
        system = "You are a data documentation assistant. Respond with valid JSON only."

        result = await self._llm.complete_json(prompt, system=system)
        return result

    async def diff_similar_tables(
        self,
        current_schema: list[dict[str, Any]],
        similar_schemas: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Compare current dataset fields against each similar dataset.

        Args:
            current_schema: List of field dicts for the current dataset.
            similar_schemas: List of dicts, each with 'urn' and 'fields' keys.

        Returns:
            List of diff summaries per similar dataset.
        """
        current_fields = {f.get("fieldPath", ""): f for f in current_schema}
        current_field_names = set(current_fields.keys())

        diffs: list[dict[str, Any]] = []
        for similar in similar_schemas:
            similar_urn = similar.get("urn", "")
            similar_fields_list = similar.get("fields", [])
            similar_fields = {f.get("fieldPath", ""): f for f in similar_fields_list}
            similar_field_names = set(similar_fields.keys())

            overlapping = current_field_names & similar_field_names
            current_only = sorted(current_field_names - similar_field_names)
            similar_only = sorted(similar_field_names - current_field_names)

            type_mismatches: list[dict[str, Any]] = []
            overlapping_details: list[dict[str, Any]] = []
            for field_name in sorted(overlapping):
                cur_type = current_fields[field_name].get("nativeDataType", "")
                sim_type = similar_fields[field_name].get("nativeDataType", "")
                detail: dict[str, Any] = {"fieldPath": field_name}
                if cur_type != sim_type:
                    type_mismatches.append(
                        {
                            "fieldPath": field_name,
                            "current_type": cur_type,
                            "similar_type": sim_type,
                        }
                    )
                overlapping_details.append(detail)

            diffs.append(
                {
                    "similar_urn": similar_urn,
                    "overlapping_fields": overlapping_details,
                    "current_only": current_only,
                    "similar_only": similar_only,
                    "type_mismatches": type_mismatches,
                }
            )

        return diffs
