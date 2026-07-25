"""Section-aware parsing helpers for the declarative catalogues under ``spec/``.

Shared by the spec-conformance tests in this package. Each helper reads a source that
*enumerates* its entries statically — a markdown table, a docstring block, a class
attribute, or an AST node — never a text grep for error-code strings. That distinction
matters in one direction only, and it is worth stating precisely:

* An **error-code string** is often not written out at all. ``EntityNotFoundError``
  builds its code as ``f"{entity_type.upper()}_NOT_FOUND"``, so grepping ``src/`` for
  ``"USER_NOT_FOUND"`` finds nothing even though the code is returned to clients. A
  grep over code strings therefore produces false "never raised" results and is not
  used here.
* The ``entity_type`` **argument**, by contrast, is a plain string literal at every
  call site. It is extracted by AST (:func:`entity_not_found_call_site_types`), which
  is exact rather than textual, and the resulting code is obtained by *instantiating*
  the exception rather than by re-deriving the naming rule in the test.

Spec files are located relative to the repo root derived from ``__file__`` so the
helpers work regardless of the working directory pytest is invoked from.
"""

from __future__ import annotations

import ast
import pkgutil
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# tests/unit/spec_conformance/_api_md.py → parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
API_MD = REPO_ROOT / "spec" / "API.md"
BACKEND_MD = REPO_ROOT / "spec" / "feature" / "BACKEND.md"
EXCEPTIONS_PY = REPO_ROOT / "src" / "shared" / "exceptions.py"

# A route identity: (HTTP method, normalised path).
RouteRef = tuple[str, str]

#: Heading of the ``spec/API.md`` section that owns the route tables. Only tables
#: nested under it are read as route rows, so unrelated method-shaped tables
#: elsewhere in the document (e.g. §Access Control's method × role grid) cannot leak in.
ROUTE_CATALOGUE_HEADING = "Route Catalogue"

#: ``spec/API.md §"Redefined DataHub Functions *(TBD)*"`` carries a route table whose
#: prose states: "These routes are **not yet defined**; scope and design will be
#: specified when the feature is planned." Sections flagged TBD document future work,
#: so their rows are excluded from the catalogue. Keying off the heading (rather than
#: hardcoding the two paths) means a later addition to such a section is handled too.
TBD_HEADING_RE = re.compile(r"\bTBD\b")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")

# ``| `GET` | `/spoke/...` | …`` — the shared shape of every route table in API.md.
# The Admin/Internal-Admin tables carry different trailing columns (Body/Response/Auth
# instead of Purpose/Feature/UC); only the first two cells are read.
_ROUTE_ROW_RE = re.compile(
    r"^\|\s*`?(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)`?\s*\|\s*`([^`]+)`\s*\|"
)

# ``| `ERROR_CODE` | 422 | description |`` in §Application Error Codes.
_ERROR_ROW_RE = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*(\d{3})\s*\|")

# ``| `ExceptionName` | 404 | `CODE_A`, `CODE_B` |`` in BACKEND.md §Exception-to-HTTP Mapping.
_MAPPING_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(\d{3})\s*\|\s*(.+?)\s*\|\s*$")
_BACKTICKED_CODE_RE = re.compile(r"`([A-Z][A-Z0-9_]*)`")

# Class-docstring enumeration blocks in src/shared/exceptions.py.
_BLOCK_HEADER_RE = re.compile(r"^\s*Valid (error_code|entity_type) values\b.*:\s*$")
_CODE_ENTRY_RE = re.compile(r"^\s+([A-Z][A-Z0-9_]{2,})\s+[—-]\s")
_ENTITY_ENTRY_RE = re.compile(r'^\s+"([a-z][a-z0-9_]*)"\s*→\s*([A-Z][A-Z0-9_]*)\s*$')

_PATH_PARAM_RE = re.compile(r"\{[^}]*\}")
_API_PREFIX = "/api/v1"


@dataclass(frozen=True)
class Section:
    """One markdown section: its heading, nesting level, breadcrumb, and body lines."""

    heading: str
    level: int
    breadcrumb: tuple[str, ...]
    lines: tuple[str, ...]


def parse_sections(text: str) -> list[Section]:
    """Split markdown into sections, tracking the full heading breadcrumb.

    Lines inside fenced code blocks are never treated as headings, so a ``#`` comment
    in an embedded YAML/shell snippet cannot open a spurious section.
    """
    sections: list[Section] = []
    stack: list[str] = []
    heading, level, breadcrumb = "", 0, ()
    buf: list[str] = []
    in_fence = False

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match is None:
            buf.append(line)
            continue
        sections.append(Section(heading, level, breadcrumb, tuple(buf)))
        buf = []
        level = len(match.group(1))
        heading = match.group(2)
        del stack[level - 1 :]
        stack.append(heading)
        breadcrumb = tuple(stack)

    sections.append(Section(heading, level, breadcrumb, tuple(buf)))
    return sections


def api_md_sections() -> list[Section]:
    """Parsed sections of ``spec/API.md``."""
    return parse_sections(API_MD.read_text(encoding="utf-8"))


def normalise_path(path: str) -> str:
    """Reduce a route path to the form both sides can be compared on.

    Path parameters collapse to ``{}`` (the impl and the spec name them differently —
    ``{user_id}`` vs ``{id}``, ``{composite_id:path}`` vs ``{composite_id}``), the
    ``/api/v1`` mount prefix is stripped (``spec/API.md §Route Catalogue``: "All routes
    are prefixed with ``/api/v1``", with §System mounted at the root as the documented
    exception), and a trailing slash is dropped.
    """
    path = _PATH_PARAM_RE.sub("{}", path)
    if path.startswith(_API_PREFIX):
        path = path[len(_API_PREFIX) :]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/"


def _route_rows_from(sections: Iterable[Section]) -> frozenset[RouteRef]:
    rows: set[RouteRef] = set()
    for section in sections:
        for line in section.lines:
            match = _ROUTE_ROW_RE.match(line)
            if match is not None:
                rows.add((match.group(1), normalise_path(match.group(2))))
    return frozenset(rows)


def spec_route_rows() -> frozenset[RouteRef]:
    """Every route-table row under §Route Catalogue, excluding TBD sections."""
    return _route_rows_from(
        section
        for section in api_md_sections()
        if ROUTE_CATALOGUE_HEADING in section.breadcrumb
        and not any(TBD_HEADING_RE.search(h) for h in section.breadcrumb)
    )


def spec_tbd_route_rows() -> frozenset[RouteRef]:
    """Route-table rows that live under a TBD-flagged section (documented future work)."""
    return _route_rows_from(
        section
        for section in api_md_sections()
        if ROUTE_CATALOGUE_HEADING in section.breadcrumb
        and any(TBD_HEADING_RE.search(h) for h in section.breadcrumb)
    )


def registered_routes(app: object) -> frozenset[RouteRef]:
    """Every route registered on the FastAPI app, walked recursively.

    Included routers are wrapped lazily: a wrapper exposes ``original_router`` plus an
    ``include_context`` carrying the mount prefix, and only the leaves carry ``path`` /
    ``methods``. Walking ``app.routes`` alone would see the wrappers, not the endpoints.

    ``HEAD`` and ``OPTIONS`` are dropped — they are framework-supplied for ``GET``
    routes and are not catalogued in ``spec/API.md``.
    """
    return frozenset(method_path for method_path, _ in _walk_routes(getattr(app, "routes", []), ""))


def registered_route_response_models(app: object) -> tuple[tuple[RouteRef, object], ...]:
    """``((method, path), response_model)`` for every registered route, walked recursively.

    Same walk as :func:`registered_routes`, retaining each route's declared
    ``response_model`` so a check can reason about which schema serves which route.
    Returned as a tuple of pairs rather than a dict because two routes that differ only
    in a path-parameter *name* normalise to the same :data:`RouteRef`, and a dict would
    silently collapse them.
    """
    return _walk_routes(getattr(app, "routes", []), "")


def _walk_routes(routes: Iterable[object], prefix: str) -> tuple[tuple[RouteRef, object], ...]:
    found: list[tuple[RouteRef, object]] = []
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            sub_prefix = getattr(context, "prefix", "") or ""
            found.extend(_walk_routes(getattr(original, "routes", []), prefix + sub_prefix))
            continue
        path = getattr(route, "path", None)
        # ``is not None`` and never a truthiness check: collection-root endpoints are
        # declared as ``@sub_router.get("")`` (e.g. src/api/routers/spoke/common/data/
        # core.py), so their own ``path`` is the empty string and ``if not path`` would
        # silently drop them from the comparison.
        if path is None:
            continue
        for method in getattr(route, "methods", None) or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            ref = (method, normalise_path(prefix + path))
            found.append((ref, getattr(route, "response_model", None)))
    return tuple(found)


def api_md_error_codes() -> dict[str, int]:
    """``{error_code: http_status}`` from ``spec/API.md §Application Error Codes``."""
    codes: dict[str, int] = {}
    for section in api_md_sections():
        if section.heading != "Application Error Codes":
            continue
        for line in section.lines:
            match = _ERROR_ROW_RE.match(line)
            if match is not None:
                codes[match.group(1)] = int(match.group(2))
    return codes


def backend_md_exception_mapping() -> list[tuple[str, int, tuple[str, ...]]]:
    """``spec/feature/BACKEND.md §Exception-to-HTTP Mapping`` as (exception, status, codes)."""
    mapping: list[tuple[str, int, tuple[str, ...]]] = []
    for section in parse_sections(BACKEND_MD.read_text(encoding="utf-8")):
        if section.heading != "Exception-to-HTTP Mapping":
            continue
        for line in section.lines:
            match = _MAPPING_ROW_RE.match(line)
            if match is None:
                continue
            codes = tuple(_BACKTICKED_CODE_RE.findall(match.group(3)))
            if codes:
                mapping.append((match.group(1), int(match.group(2)), codes))
    return mapping


def _docstring_blocks(doc: str) -> list[tuple[str, list[str]]]:
    """Return ``(block_kind, block_lines)`` for each ``Valid … values:`` block."""
    lines = doc.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(lines):
        header = _BLOCK_HEADER_RE.match(lines[index])
        if header is None:
            index += 1
            continue
        body: list[str] = []
        index += 1
        while index < len(lines):
            line = lines[index]
            # A block runs until a blank line or a line that returns to column 0.
            if not line.strip() or not line[:1].isspace():
                break
            body.append(line)
            index += 1
        blocks.append((header.group(1), body))
    return blocks


def exception_docstring_error_codes() -> dict[str, frozenset[str]]:
    """``{ExceptionClass: {error_code, …}}`` from ``src/shared/exceptions.py`` docstrings.

    Reads only the declarative ``Valid error_code values:`` / ``Valid entity_type
    values:`` blocks on each class docstring — the classes' statically enumerable
    self-declaration of which codes they may carry.
    """
    result: dict[str, frozenset[str]] = {}
    tree = ast.parse(EXCEPTIONS_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        doc = ast.get_docstring(node)
        if doc is None:
            continue
        codes: set[str] = set()
        for kind, body in _docstring_blocks(doc):
            for line in body:
                if kind == "error_code":
                    match = _CODE_ENTRY_RE.match(line)
                    if match is not None:
                        codes.add(match.group(1))
                else:
                    entity = _ENTITY_ENTRY_RE.match(line)
                    if entity is not None:
                        codes.add(entity.group(2))
        if codes:
            result[node.name] = frozenset(codes)
    return result


def entity_not_found_map() -> dict[str, str]:
    """``{entity_type: error_code}`` from ``EntityNotFoundError``'s docstring block.

    This is the only declarative list of valid ``entity_type`` values; the pairs are
    read verbatim rather than recomputed with the impl's own ``upper() + "_NOT_FOUND"``
    rule, so a broken derivation cannot validate itself.
    """
    tree = ast.parse(EXCEPTIONS_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "EntityNotFoundError":
            continue
        doc = ast.get_docstring(node) or ""
        pairs: dict[str, str] = {}
        for kind, body in _docstring_blocks(doc):
            if kind != "entity_type":
                continue
            for line in body:
                match = _ENTITY_ENTRY_RE.match(line)
                if match is not None:
                    pairs[match.group(1)] = match.group(2)
        return pairs
    return {}


def exception_class_attr_error_codes() -> dict[str, str]:
    """``{ExceptionClass: default_error_code}`` from class-body ``error_code`` assignments.

    A class-level ``error_code: str = "…"`` is the code every instance carries unless a
    constructor argument overrides it, so it is as declarative as a docstring block and
    is enumerated the same way.
    """
    codes: dict[str, str] = {}
    tree = ast.parse(EXCEPTIONS_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            target, value = None, None
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                target, value = stmt.target.id, stmt.value
            elif (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                target, value = stmt.targets[0].id, stmt.value
            if (
                target == "error_code"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                codes[node.name] = value.value
    return codes


#: The Python packages under ``src/``. Named explicitly rather than globbing all of
#: ``src/`` because ``src/frontend`` is a pnpm project whose ``node_modules`` vendors
#: stray ``.py`` files; parsing third-party code would make this suite fail for reasons
#: unrelated to DataSpoke. A rename that empties one of these roots is caught by
#: :func:`src_python_roots` being asserted non-empty in the tests.
SRC_PYTHON_PACKAGES: tuple[str, ...] = ("api", "backend", "shared", "workflows")


def src_python_roots() -> dict[str, Path]:
    """``{package: path}`` for each Python package under ``src/``."""
    return {name: REPO_ROOT / "src" / name for name in SRC_PYTHON_PACKAGES}


def _iter_src_python_files() -> Iterable[Path]:
    files: list[Path] = []
    for root in src_python_roots().values():
        files.extend(root.rglob("*.py"))
    return sorted(files)


def api_schema_module_names() -> tuple[str, ...]:
    """Importable dotted names of every module in the ``src.api.schemas`` package.

    Walked with :func:`pkgutil.walk_packages`, matching
    ``tests/unit/api/test_response_format.py``'s discovery: the walk is **recursive**, so
    converting a schema module into a subpackage keeps its models in view instead of
    silently dropping them from every assertion built on this list. Private
    (``_``-prefixed) helper modules are included — nothing stops a response model from
    living in one.

    Walking the imported package (rather than globbing ``src/**/*.py``) also avoids
    ``src/frontend``'s vendored ``node_modules``, the hazard documented on
    :data:`SRC_PYTHON_PACKAGES`. ``test_response_envelope.py`` pins the walked package
    path against :func:`src_python_roots` so the two discovery mechanisms cannot drift.
    """
    import src.api.schemas as schemas_pkg

    return tuple(
        name
        for _, name, _ in pkgutil.walk_packages(
            schemas_pkg.__path__, prefix=schemas_pkg.__name__ + "."
        )
    )


def declared_paginated_subclass_defs(base_name: str) -> dict[str, tuple[str, ...]]:
    """``{module path: (class name, …)}`` for classes that **directly** declare *base_name*.

    An AST scan across **all** Python packages under ``src/`` — not just
    ``src/api/schemas`` — so that a response model defined outside the schemas package
    (in a router, say) is still visible. It is the counterpart to importing the schema
    modules: the import side enumerates what a conformance check will actually inspect,
    and this side proves nothing escaped that enumeration by living elsewhere.

    The match is **syntactic and one level deep**: a class whose base is an intermediate
    that itself extends *base_name* is not returned. Callers that rely on this as a
    completeness backstop must pair it with a count equality against the imported set
    (``test_response_envelope.py`` does), which is what breaks the moment such an
    indirection appears.
    """
    found: dict[str, list[str]] = {}
    for path in _iter_src_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
                if name == base_name:
                    found.setdefault(str(path.relative_to(REPO_ROOT)), []).append(node.name)
    return {module: tuple(names) for module, names in sorted(found.items())}


def entity_not_found_call_site_types() -> dict[str, tuple[str, ...]]:
    """``{entity_type: (call site, …)}`` for every ``EntityNotFoundError("…", …)`` in ``src/``.

    Extracted by AST from the first positional argument, which is a string literal at
    every call site. Docstring prose mentioning the constructor is not an ``ast.Call``
    and therefore cannot pollute the result the way a text grep would.
    """
    sites: dict[str, list[str]] = {}
    for path in _iter_src_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            entity = _entity_not_found_literal_arg(node)
            if entity is None:
                continue
            where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
            sites.setdefault(entity, []).append(where)
    return {entity: tuple(where) for entity, where in sorted(sites.items())}


def entity_not_found_dynamic_call_sites() -> tuple[str, ...]:
    """Call sites whose ``entity_type`` argument is **not** a string literal.

    The AST extraction above can only see literals. Any dynamic argument would be
    invisible to it, so those sites are surfaced separately rather than skipped —
    otherwise the conformance check would quietly stop covering them.
    """
    dynamic: list[str] = []
    for path in _iter_src_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_entity_not_found(node.func):
                continue
            if not node.args:
                dynamic.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} (no positional arg)")
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                dynamic.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    return tuple(dynamic)


def _is_entity_not_found(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "EntityNotFoundError"
    if isinstance(func, ast.Attribute):
        return func.attr == "EntityNotFoundError"
    return False


def _entity_not_found_literal_arg(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or not _is_entity_not_found(node.func) or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


#: An exception class name: any identifier ending in ``Error`` or ``Exception``.
_EXCEPTION_NAME_RE = re.compile(r"\b\w*(?:Error|Exception)\b")

#: An error-code token: an ALL-CAPS identifier of two or more underscore-separated
#: segments (``DATASET_NOT_FOUND``, ``METAGEN_CONF_EXISTS``). The leading ``\b`` is
#: load-bearing — ``src/shared/exceptions.py``'s module docstring states the derivation
#: rule by quoting the bare suffix ``"_NOT_FOUND"``, which is a fragment of a code rather
#: than a code, and a leading-underscore match would misread it as one.
_ERROR_CODE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

#: An HTTP status such a mapping would assign to a class.
_HTTP_STATUS_RE = re.compile(r"\b[1-5]\d{2}\b")


def module_docstring_mapping_blocks(doc: str | None = None) -> tuple[str, ...]:
    """Blocks of a module docstring that pair an exception class with a code or status.

    ``src/shared/exceptions.py`` keeps its exception→code/status catalogue on the
    per-class docstrings, with ``spec/feature/BACKEND.md §Exception-to-HTTP Mapping`` as
    the spec-side statement of the same mapping. A third copy in the **module** docstring
    is what this detects, so it can be asserted absent rather than parsed and tolerated.

    A blank-line-separated block is reported when it names an exception class **and**
    carries an error-code token or an HTTP status — the co-occurrence that makes a block
    a mapping, whatever layout (``→``, a pipe table, aligned columns) it is written in.
    Prose that names a class without pairing it to a code, or lists codes without naming
    a class, is not a mapping and is not reported.

    *doc* defaults to ``src/shared/exceptions.py``'s module docstring; passing a string
    lets a test prove the detector fires on a synthetic block.
    """
    if doc is None:
        tree = ast.parse(EXCEPTIONS_PY.read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree) or ""
    offenders: list[str] = []
    for block in re.split(r"\n\s*\n", doc):
        if not block.strip():
            continue
        if _EXCEPTION_NAME_RE.search(block) and (
            _ERROR_CODE_TOKEN_RE.search(block) or _HTTP_STATUS_RE.search(block)
        ):
            offenders.append(block.strip())
    return tuple(offenders)


def assert_drift_allowlist(
    actual: Iterable[str],
    allowlist: frozenset[str],
    *,
    what: str,
    allowlist_name: str,
) -> None:
    """Assert an observed drift set equals its allowlist — in **both** directions.

    An allowlist that only suppresses failures rots into a rubber stamp, so it is
    checked symmetrically:

    1. drift that is not allowlisted fails — new drift is caught;
    2. an allowlist entry that is no longer drifting **also** fails, with an
       instruction to delete it — the list is forced to shrink and cannot go on
       masking a regression that reintroduces the same mismatch later.

    An allowlist entry records either a *scheduled* mismatch (delete it when the phase
    that fixes it lands) or a *documented* exception (a spec/ citation justifies it
    permanently). Branch 2 is meaningful for both: for the latter it asserts the
    documented thing still exists, so removing it fails here rather than going unnoticed.

    The symmetry is a design choice of this package, not a rule quoted from a spec.
    It follows the intent of spec/TESTING.md §Assertion Discipline — "Author assertions
    so that a passing result is only reachable when the spec'd behavior actually
    occurred" — applied to the allowlist itself: a one-directional allowlist makes a
    passing result reachable for drift that has already been fixed.

    Both branches are exercised directly by test_drift_allowlist.py; without those
    tests every call site here runs empty-vs-empty and neither branch is ever taken.
    """
    observed = set(actual)
    unexpected = sorted(observed - allowlist)
    assert not unexpected, (
        f"Undeclared {what}: {unexpected}. Fix the mismatch, or add each entry to "
        f"{allowlist_name} with a comment justifying it — a spec/ citation for a "
        f"documented exception, or the issue-#86 phase that resolves a scheduled mismatch."
    )
    stale = sorted(allowlist - observed)
    assert not stale, (
        f"Stale {allowlist_name} entries: {stale}. These no longer drift — delete them "
        f"from {allowlist_name}. Leaving a resolved entry in place would silently mask "
        f"a later regression that reintroduces the same {what}."
    )
