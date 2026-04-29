"""Apache AGE graph client for DataSpoke ontology triple materialisation.

The AGE graph is a read-side replica of the relational ``ontogen_triples``
table. Materialisation failures must NOT roll back the relational write — callers
are responsible for catching ``DataSpokeError`` (code ``AGE_ERROR``) and
continuing so the relational source of truth is always updated first.
"""

from src.shared.graph.client import AgeGraph

__all__ = ["AgeGraph"]
