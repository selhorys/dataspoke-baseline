---
name: project-recipe-regex-trust-boundary
description: Ingestion recipe allow/deny patterns are writer-supplied regexes run in the hourly sweep on the API's single event loop; malformed patterns are guarded via stdlib re.compile, catastrophic backtracking is issue #114 and two obvious mitigations are already rejected
metadata:
  type: project
---

`recipe.source.config.{schema_pattern,table_pattern,topic_patterns,dataset_pattern}` are
writer-supplied (`require_writer`) regex lists stored verbatim in the `ingestion_source.recipe`
JSONB and executed by `build_matcher_checked` (`src/shared/models/ingestion.py`) against every
DataHub dataset name on every hourly `IngestionService.sync()` sweep. `recipe` is typed
`dict[str, Any]` at intake with no cap on pattern count, pattern length or body size (only
`src/api/routers/spoke/ontogen.py` does its own 413 check; ingress caps bodies at 50 MB).

**Malformed / wrongly-typed patterns — handled, no SDK coupling.** `_precompile` runs stdlib
`re.compile` over the raw pattern strings inside the guarded block before constructing
`AllowDenyPattern`, so `re.error` lands in the handler instead of firing later from inside the
returned predicate. Verified equivalent to the runtime path: `AllowDenyPattern._compiled_allow`
compiles with `re.IGNORECASE`, and IGNORECASE never changes whether a pattern *compiles*.
`build_matcher_checked` returns `(predicate, reason)`; reason `None` means the pattern set really
was evaluated.

**Catastrophic backtracking — issue #114, and do not re-propose these two fixes.** A pathological
pattern raises nothing, so no guard fires; it blocks the API's single event loop (uvicorn, 1
worker) and ~60s trips the `/health` liveness probe, killing the pod, and the hourly DAG
re-triggers it. Rejected on technical grounds by the human, verified empirically:
`asyncio.to_thread` + `wait_for` does **not** bound it (CPython holds the GIL for the whole of one
`re.match`, a C-level call that never yields), and a pattern-length cap does not either
(`^(a+)+$` is 8 characters). A real fix needs a different regex engine or an out-of-process
matcher.

**Two adjacent traps the guard does *not* cover:**
- **Unparseable recipe ≠ degraded.** `parse_recipe` failing returns reason `None`, so the sweep
  treats it as "evaluated, matched nothing" and **prunes every stored `matched` row** for that
  source with no warning and no counter. Reachable without any writer action: the DataHub client
  does `(s.get("config") or {}).get("recipe") or ""`, and step 1 overwrites the stored recipe with
  `{}` on any unparseable/absent recipe.
- **`degraded_reason` is the one writer-influenced string logged with `%s`** (every other field
  uses `%r`). Raw newlines are reachable (`[\n-\x00]`, `(?\nX)` → `bad character range \n-\x00`),
  pydantic `ValidationError` text is inherently 4 lines, and length is unbounded
  (`\N{` + 200 000 chars → 200 KB reason, re-logged hourly forever since a degraded source never
  self-heals). No credential leak today only because DataHub's `ConfigModel` sets
  `hide_input_in_errors: True` — an SDK `model_config` under a `>=1.6,<2.0` range pin, not
  something this code asserts.

**How to apply:** on any diff touching `build_matcher*`, `sync()`, or recipe intake, ask: (a) is
compilation forced inside a guarded block, (b) is per-source evaluation isolated, (c) is the
prune/report split honest about which failures were *not* evaluated, (d) is any exception-derived
string logged unescaped or uncapped. Recipes are stored masked (`_mask_recipe_secrets`, key-name
based) and hold `${name__key}` refs, so pattern handling never touches plaintext credentials — see
[[project-ingestion-secret-ref-model]].

The mapping table (`ingestion_source_dataset`) feeds `reverse_lookup`,
`/data/{urn}/attr|event/ingestion`, the ingestion-freshness measurer and the unmanaged bucket —
**no ACL reads it**, and a writer can already assert arbitrary mappings by design
(`allow: [".*"]`), so mapping-integrity findings here are integrity/attribution, not privilege.
