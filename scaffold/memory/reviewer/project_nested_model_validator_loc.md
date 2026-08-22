---
name: nested-model-validator-loc
description: A Pydantic model_validator(mode="after") on a NESTED model reports loc = the parent's field path (e.g. ["attribute"]), not [] — parse the request model, not the nested model, before believing a client-vs-server error-shape claim
metadata:
  type: project
---

`ValidationAttribute._check_window_shift` (`src/api/schemas/validation.py`) is a
`model_validator(mode="after")`. Instantiating `ValidationAttribute` directly gives
`loc == ()`. But the field that carries it is `PutValidationConfRequest.attribute`, and
Pydantic prefixes the nested model's root with the parent field name:

```
[{"loc": ["attribute"], "msg": "Value error, cadence_offset * cadence_unit must not exceed
  315360000 seconds (ten years)", "type": "value_error"}]
```

Over the wire FastAPI prefixes `body`, so the 422 `loc` is `["body", "attribute"]` — a
**field** path, one level shallower than a field-level rule would give.

**Why:** the frontend stage claimed it had "empirically verified" the server loc was `[]`
(model root) and used that to justify the client attaching the same rule to
`attribute.cadence_offset`. The `[]` came from parsing the nested model in isolation. The
*conclusion* survived (client and server disagree; the divergence is unobservable because
RHF `handleSubmit` never calls `onValid` on a failed parse, the two bounds are the same
constant, and no code maps a 422 `loc` back onto a form field), but the stated evidence was
wrong by one path segment.

**How to apply:** when a generator reports a server error `loc`, re-derive it by
constructing the **request** model (`PutXRequest(**body)`) and printing `e.errors()`.
`uv run python` with `sys.path.insert(0, <repo root>)` is enough. Then close the loop on
reachability: grep for `setError` / any `loc`-to-field mapping in `src/frontend/lib/api/`
and the panel — if `serverError` is a plain string, the server's shape is genuinely
unreachable and documenting the divergence in a comment is the right call.
See [[frontend-numeric-bound-seams]] for the sibling client-side enforcement seam.
