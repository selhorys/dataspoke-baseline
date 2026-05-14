"""Apache AGE graph client — materialises ontogen_triples as graph edges.

The relational ``ontogen_triples`` table is the **source of truth** for review
status.  This client keeps an AGE graph (``dataspoke_ontogen``) in sync as a
read-side replica used by traversal queries (governance overview ontology-graph
view).

Design decisions
----------------
* **Session-factory injection**: the constructor takes an
  ``async_sessionmaker[AsyncSession]`` (same factory used by
  ``db/session.py``) so the client can open its own sessions and participate
  in the same connection pool.
* **AGE Cypher parameter binding**: AGE's ``cypher()`` function requires the
  third argument be a Postgres parameter or a JSON literal cast to agtype.
  We bind the params JSON and the cypher string via SQLAlchemy ``text()``
  bindparams.  The graph name is validated with ``_assert_slug()`` in
  ``__init__`` before being string-interpolated into the SQL (AGE's first
  argument does not accept a bind parameter in all versions).
* **SET LOCAL search_path**: scoped to the current transaction so it resets
  automatically when the connection is returned to the pool.
* **Best-effort**: all methods log a WARNING on any AGE error and re-raise as
  ``DataSpokeError("AGE_ERROR")``.  The caller decides whether to continue.
"""

import json
import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.exceptions import DataSpokeError

logger = logging.getLogger(__name__)

# Slug pattern: lowercase letters, digits, underscores only.
# Double-underscore is already excluded at the DB CHECK level; this regex is
# defence-in-depth at the Python layer.
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")

_GRAPH_NAME = "dataspoke_ontogen"


def _assert_slug(value: str, label: str) -> None:
    """Raise ValueError when *value* is not a valid slug identifier.

    IDs are constrained to slugs by the ontogen_nodes/edges DB CHECK
    constraints; this is an additional defence-in-depth guard at the Python
    layer before they are interpolated into AGE Cypher strings.
    """
    if not _SLUG_RE.match(value):
        raise ValueError(
            f"AgeGraph: {label} {value!r} is not a valid slug "
            f"(allowed: a-z 0-9 _). Injection guard triggered."
        )


class AgeGraph:
    """Materialise and traverse ontology triples in an Apache AGE graph.

    Parameters
    ----------
    session_factory:
        Async session factory (``async_sessionmaker[AsyncSession]``).
    graph_name:
        AGE graph name.  Defaults to ``dataspoke_ontogen``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        graph_name: str = _GRAPH_NAME,
    ) -> None:
        # Validate the graph name at construction time so string interpolation
        # below is provably safe (AGE's first cypher() arg does not accept a
        # bound parameter in all supported versions).
        _assert_slug(graph_name, "graph_name")
        self._session_factory = session_factory
        self._graph_name = graph_name

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _setup_age(self, session: AsyncSession) -> None:
        """Load AGE and set LOCAL search_path for the current transaction."""
        await session.execute(text("LOAD 'age'"))
        await session.execute(text("SET LOCAL search_path = ag_catalog, '$user', public"))

    # ── Public API ────────────────────────────────────────────────────────────

    async def materialize_triple(
        self,
        subject_id: str,
        edge_id: str,
        object_id: str,
        edge_label: str,
    ) -> None:
        """MERGE (subject)-[edge]->(object) in the AGE graph.

        Parameters
        ----------
        subject_id, edge_id, object_id:
            Slug IDs matching relational ontogen_nodes / ontogen_edges PKs.
        edge_label:
            Human-readable edge label stored on the AGE relationship.

        Raises
        ------
        ValueError
            If any ID is not a valid slug (defence-in-depth; DB level also
            enforces this).
        DataSpokeError
            ``error_code="AGE_ERROR"`` on any AGE execution failure.  The
            caller should catch this and continue — the relational write is
            NOT rolled back by this error.
        """
        _assert_slug(subject_id, "subject_id")
        _assert_slug(edge_id, "edge_id")
        _assert_slug(object_id, "object_id")

        params_json = json.dumps(
            {
                "subj_id": subject_id,
                "obj_id": object_id,
                "edge_id": edge_id,
                "edge_label": edge_label,
            }
        )

        cypher_query = (
            "MERGE (s:Node {id: $subj_id}) "
            "MERGE (o:Node {id: $obj_id}) "
            "MERGE (s)-[r:Edge {id: $edge_id, label: $edge_label}]->(o)"
        )

        # The graph name is slug-validated in __init__ so interpolation is safe.
        # The cypher string and params JSON are passed as bind parameters.
        sql = text(
            f"SELECT * FROM ag_catalog.cypher("
            f"'{self._graph_name}', "
            f":cypher, "
            f"CAST(:params AS agtype)"
            f") AS (a agtype)"
        )

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._setup_age(session)
                    await session.execute(sql, {"cypher": cypher_query, "params": params_json})
        except Exception as exc:
            logger.warning(
                "age_materialize_triple_failed",
                extra={
                    "subject_id": subject_id,
                    "edge_id": edge_id,
                    "object_id": object_id,
                },
                exc_info=True,
            )
            raise DataSpokeError("AGE_ERROR: graph operation failed") from exc

    async def delete_triple(
        self,
        subject_id: str,
        edge_id: str,
        object_id: str,
    ) -> None:
        """MATCH and DELETE the relationship between subject and object nodes.

        Parameters
        ----------
        subject_id, edge_id, object_id:
            Slug IDs identifying the triple to remove from the graph.

        Raises
        ------
        DataSpokeError
            ``error_code="AGE_ERROR"`` on any AGE execution failure.
        """
        _assert_slug(subject_id, "subject_id")
        _assert_slug(edge_id, "edge_id")
        _assert_slug(object_id, "object_id")

        params_json = json.dumps(
            {
                "subj_id": subject_id,
                "obj_id": object_id,
                "edge_id": edge_id,
            }
        )

        cypher_query = (
            "MATCH (s:Node {id: $subj_id})-[r:Edge {id: $edge_id}]->(o:Node {id: $obj_id}) "
            "DELETE r"
        )

        sql = text(
            f"SELECT * FROM ag_catalog.cypher("
            f"'{self._graph_name}', "
            f":cypher, "
            f"CAST(:params AS agtype)"
            f") AS (a agtype)"
        )

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._setup_age(session)
                    await session.execute(sql, {"cypher": cypher_query, "params": params_json})
        except Exception as exc:
            logger.warning(
                "age_delete_triple_failed",
                extra={
                    "subject_id": subject_id,
                    "edge_id": edge_id,
                    "object_id": object_id,
                },
                exc_info=True,
            )
            raise DataSpokeError("AGE_ERROR: graph operation failed") from exc

    async def traverse(
        self,
        start_node_id: str,
        max_hops: int = 2,
    ) -> list[tuple[str, str, str]]:
        """Variable-length traversal from *start_node_id* up to *max_hops* away.

        Returns every reachable (subject_id, edge_id, object_id) tuple,
        deduped.  Uses a variable-length Cypher match
        ``[*1..max_hops]`` so all edges at depth 1 through *max_hops* are
        returned.

        Parameters
        ----------
        start_node_id:
            Slug ID of the starting node.
        max_hops:
            Maximum traversal depth (inclusive).  Must be an int between 1
            and 10 (inclusive); 10 aligns with spec §Overview Service usage.

        Returns
        -------
        list[tuple[str, str, str]]
            Deduplicated list of ``(subject_id, edge_id, object_id)`` tuples
            for every edge reachable from *start_node_id*.

        Raises
        ------
        ValueError
            If ``max_hops`` is not an int or is outside 1..10.
        DataSpokeError
            ``error_code="AGE_ERROR"`` on any AGE execution failure.
        """
        _assert_slug(start_node_id, "start_node_id")
        # Type guard first (bool is a subclass of int in Python, reject it)
        if not isinstance(max_hops, int) or isinstance(max_hops, bool):
            raise ValueError("max_hops must be an int")
        if not 1 <= max_hops <= 10:
            raise ValueError("max_hops must be between 1 and 10")

        params_json = json.dumps({"start_id": start_node_id})

        # max_hops is a provably safe int 1..10 at this point; interpolating it
        # directly into the Cypher range literal is safe.
        cypher_query = (
            f"MATCH (start:Node {{id: $start_id}})-[r*1..{max_hops}]-(neighbor:Node) "
            f"UNWIND r AS rel "
            f"MATCH (s:Node)-[rel]->(o:Node) "
            f"RETURN s.id, rel.id, o.id"
        )

        sql = text(
            f"SELECT * FROM ag_catalog.cypher("
            f"'{self._graph_name}', "
            f":cypher, "
            f"CAST(:params AS agtype)"
            f") AS (subject_id agtype, edge_id agtype, object_id agtype)"
        )

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._setup_age(session)
                    result = await session.execute(
                        sql, {"cypher": cypher_query, "params": params_json}
                    )
                    rows = result.fetchall()
        except Exception as exc:
            logger.warning(
                "age_traverse_failed",
                extra={"start_node_id": start_node_id, "max_hops": max_hops},
                exc_info=True,
            )
            raise DataSpokeError("AGE_ERROR: graph operation failed") from exc

        # AGE returns agtype values as JSON-quoted strings like '"book"'; strip quotes.
        seen: set[tuple[str, str, str]] = set()
        triples: list[tuple[str, str, str]] = []
        for row in rows:
            # agtype scalar strings are returned as '"value"' — strip surrounding quotes.
            def _strip(v: object) -> str:
                s = str(v)
                if s.startswith('"') and s.endswith('"'):
                    return s[1:-1]
                return s

            triple = (_strip(row[0]), _strip(row[1]), _strip(row[2]))
            if triple not in seen:
                seen.add(triple)
                triples.append(triple)

        return triples
