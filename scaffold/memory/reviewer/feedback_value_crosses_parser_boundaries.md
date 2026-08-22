---
name: value-crosses-parser-boundaries
description: A value install.sh interpolates into generated YAML is re-parsed by Helm and again by the consuming app — sweep spellings at every boundary, not just the documented one
metadata:
  type: feedback
---

When `install.sh` writes an operator-supplied value into a generated values/env
fragment, that string crosses **three** parsers before it means anything:
PyYAML (in install.sh's own `python3 -c` resolvers) → Helm's YAML/`tpl` →
the consuming app's own coercion. Verify each boundary separately; a check
written against one of them silently disagrees with the others.

**Why:** two demonstrated misses in the #138 (prod Airflow auth) review.

1. *Boolean spellings.* `_resolve_effective_all_admins` string-compares the
   resolved value to exactly `"True"`. PyYAML resolves unquoted `true`/`yes`/`on`
   to Python `True` (prints `True`, matches), but **quoted** `"true"`, `"TRUE"`,
   `"1"`, `"t"` stay strings and miss — while Helm renders them verbatim into
   `airflow.cfg` and Airflow's `conf.getboolean` accepts `t|true|1` after
   `.lower().strip()`. Net: a real anonymous-admin overlay took the "strict"
   branch and the security warning was never printed. Mirror the consumer's own
   coercion instead of `== "True"`.

2. *Trailing newline folds to a space.* A secret value interpolated as
   `value: "${x}:ADMIN"` inside a double-quoted YAML scalar turns an embedded
   `\n` into a **space** — a `kubectl create secret --from-file` trailing newline
   silently produced `"user :ADMIN"` while the init container wrote the raw
   `"user\n"` key, so the two never matched. A newline mid-value also injects a
   whole extra env entry into every pod; `helm template`/`helm lint` exit 0.

**How to apply:** whenever a review touches install.sh emitting YAML from a
Secret/env value, (a) run the extracted resolver over a sweep of quoted and
unquoted spellings, (b) `helm template` each one and read the rendered line,
(c) check the consumer's parser source for its accepted set, and (d) demand a
charset guard on the interpolated value — install.sh already does this for
namespaces, StorageClass names and `secrets.existingSecret`, so it is the
house convention, not an extra ask. See [[project-airflow-prod-auth-wiring]].
