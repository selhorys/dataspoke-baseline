"""Multi-source metadata extractors for ingestion pipelines.

Each extractor connects to an external source, extracts metadata, and returns
a list of ExtractedMetadata value objects that the IngestionService transforms
into DataHub aspects.
"""

import asyncio
import base64
import csv
import io
from abc import ABC, abstractmethod
from typing import Any

import httpx
import sqlparse
from sqlparse.sql import Identifier, IdentifierList
from sqlparse.tokens import DML, Keyword

from src.shared.exceptions import DataSpokeError


class ExtractedMetadata:
    """Immutable value object representing a single piece of extracted metadata."""

    __slots__ = ("metadata_type", "content", "source_ref")

    def __init__(self, metadata_type: str, content: dict[str, Any], source_ref: str) -> None:
        self.metadata_type = metadata_type
        self.content = content
        self.source_ref = source_ref


class BaseExtractor(ABC):
    """Abstract base class for all metadata extractors."""

    @abstractmethod
    async def extract(self, source_config: dict[str, Any]) -> list[ExtractedMetadata]:
        """Extract metadata from the configured source."""


class ConfluenceExtractor(BaseExtractor):
    """Extracts dataset descriptions from Confluence wiki pages."""

    REQUIRED_KEYS = ("base_url", "space_key", "username", "api_token")

    async def extract(self, source_config: dict[str, Any]) -> list[ExtractedMetadata]:
        missing = [k for k in self.REQUIRED_KEYS if k not in source_config]
        if missing:
            raise DataSpokeError(f"ConfluenceExtractor missing config keys: {missing}")

        base_url = source_config["base_url"].rstrip("/")
        url = f"{base_url}/wiki/rest/api/content"
        params = {
            "spaceKey": source_config["space_key"],
            "expand": "body.storage",
            "limit": 50,
        }
        auth = (source_config["username"], source_config["api_token"])

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params, auth=auth)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise DataSpokeError(f"Confluence API error: {exc}") from exc

        results: list[ExtractedMetadata] = []
        for page in data.get("results", []):
            body = page.get("body", {}).get("storage", {}).get("value", "")
            if body:
                results.append(
                    ExtractedMetadata(
                        metadata_type="description",
                        content={"title": page.get("title", ""), "body": body},
                        source_ref=f"{base_url}/wiki{page.get('_links', {}).get('webui', '')}",
                    )
                )
        return results


class GitHubExtractor(BaseExtractor):
    """Extracts code references from a GitHub repository."""

    REQUIRED_KEYS = ("owner", "repo", "token")

    async def extract(self, source_config: dict[str, Any]) -> list[ExtractedMetadata]:
        missing = [k for k in self.REQUIRED_KEYS if k not in source_config]
        if missing:
            raise DataSpokeError(f"GitHubExtractor missing config keys: {missing}")

        owner = source_config["owner"]
        repo = source_config["repo"]
        token = source_config["token"]
        path = source_config.get("path", "")

        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise DataSpokeError(f"GitHub API error: {exc}") from exc

        items = data if isinstance(data, list) else [data]
        results: list[ExtractedMetadata] = []
        for item in items:
            results.append(
                ExtractedMetadata(
                    metadata_type="code_ref",
                    content={
                        "name": item.get("name", ""),
                        "path": item.get("path", ""),
                        "type": item.get("type", ""),
                        "sha": item.get("sha", ""),
                    },
                    source_ref=item.get("html_url", f"https://github.com/{owner}/{repo}"),
                )
            )
        return results


class ExcelExtractor(BaseExtractor):
    """Extracts column mappings from Excel or CSV files."""

    async def extract(self, source_config: dict[str, Any]) -> list[ExtractedMetadata]:
        if "file_content" in source_config:
            raw = base64.b64decode(source_config["file_content"])
            file_name = source_config.get("file_name", "upload")
        elif "file_path" in source_config:
            file_path = source_config["file_path"]
            raw = await asyncio.to_thread(self._read_file, file_path)
            file_name = file_path
        else:
            raise DataSpokeError("ExcelExtractor requires 'file_path' or 'file_content'")

        if file_name.endswith(".csv") or source_config.get("format") == "csv":
            rows = self._parse_csv(raw)
        else:
            rows = await asyncio.to_thread(self._parse_excel, raw)

        results: list[ExtractedMetadata] = []
        for row in rows:
            results.append(
                ExtractedMetadata(
                    metadata_type="column_mapping",
                    content=row,
                    source_ref=file_name,
                )
            )
        return results

    @staticmethod
    def _read_file(path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    @staticmethod
    def _parse_csv(raw: bytes) -> list[dict[str, Any]]:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]

    @staticmethod
    def _parse_excel(raw: bytes) -> list[dict[str, Any]]:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        if ws is None:
            return []
        rows_iter = ws.iter_rows(values_only=True)
        try:
            headers = [
                str(h) if h is not None else f"col_{i}" for i, h in enumerate(next(rows_iter))
            ]
        except StopIteration:
            return []
        result = []
        for row in rows_iter:
            result.append({headers[i]: cell for i, cell in enumerate(row) if i < len(headers)})
        wb.close()
        return result


class SqlLogExtractor(BaseExtractor):
    """Extracts lineage edges by parsing SQL queries for table references."""

    REQUIRED_KEYS = ("queries",)

    async def extract(self, source_config: dict[str, Any]) -> list[ExtractedMetadata]:
        if "queries" not in source_config:
            raise DataSpokeError("SqlLogExtractor requires 'queries' key")

        queries = source_config["queries"]
        if not isinstance(queries, list):
            raise DataSpokeError("SqlLogExtractor 'queries' must be a list of SQL strings")

        results: list[ExtractedMetadata] = []
        for sql in queries:
            tables = await asyncio.to_thread(self._extract_tables, sql)
            if tables:
                results.append(
                    ExtractedMetadata(
                        metadata_type="lineage_edge",
                        content={"tables": tables, "sql": sql},
                        source_ref="sql_log",
                    )
                )
        return results

    @staticmethod
    def _extract_tables(sql: str) -> list[str]:
        """Parse SQL and extract table names from FROM/JOIN clauses."""
        tables: list[str] = []
        parsed = sqlparse.parse(sql)
        for statement in parsed:
            from_seen = False
            for token in statement.tokens:
                if token.ttype is DML:
                    from_seen = False
                    continue
                if token.ttype is Keyword and token.normalized in (
                    "FROM",
                    "JOIN",
                    "INNER JOIN",
                    "LEFT JOIN",
                    "RIGHT JOIN",
                    "FULL JOIN",
                    "CROSS JOIN",
                    "LEFT OUTER JOIN",
                    "RIGHT OUTER JOIN",
                    "FULL OUTER JOIN",
                ):
                    from_seen = True
                    continue
                if from_seen:
                    if token.is_whitespace:
                        continue
                    if isinstance(token, IdentifierList):
                        for identifier in token.get_identifiers():
                            name = identifier.get_real_name()
                            if name:
                                parent = identifier.get_parent_name()
                                tables.append(f"{parent}.{name}" if parent else name)
                        from_seen = False
                    elif isinstance(token, Identifier):
                        name = token.get_real_name()
                        if name:
                            parent = token.get_parent_name()
                            tables.append(f"{parent}.{name}" if parent else name)
                        from_seen = False
                    elif token.ttype is not Keyword:
                        from_seen = False
        return tables


EXTRACTOR_REGISTRY: dict[str, type[BaseExtractor]] = {
    "confluence": ConfluenceExtractor,
    "github": GitHubExtractor,
    "excel": ExcelExtractor,
    "sql_log": SqlLogExtractor,
}
