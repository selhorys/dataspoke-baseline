---
name: asyncpg-str-uuid-column
description: asyncpg accepts a str bound to a UUID(as_uuid=True) ORM column; coercion is asyncpg pgproto, not SQLAlchemy bind_processor
metadata:
  type: project
---

Binding a `str(uuid.uuid4())` to a `UUID(as_uuid=True)` SQLAlchemy ORM column works
on this stack (postgresql+asyncpg). Verified: the asyncpg UUID `bind_processor`
returns the str unchanged, but asyncpg's `pgproto.UUID(value)` codec accepts a str at
encode time, and reads come back as a Python `uuid.UUID` (Pydantic `UUID | None` accepts).

**Why:** ontogen `run_id` is `str(uuid.uuid4())` (used as Langfuse session id) but the
new `ontogen_{nodes,edges,triples}.run_id` column is `UUID(as_uuid=True)`. Most other
ORM inserts in the repo pass `uuid.uuid4()` *objects*, so str→UUID-column had no
precedent — don't assume it's a bug on sight.

**How to apply:** when a reviewer/generator claims "SQLAlchemy coerces the string on
insert," correct the mechanism (it's asyncpg pgproto, SQLAlchemy passes the str through)
but the outcome holds — no need to force `uuid.UUID(run_id)` at the call site. The path
is only exercised end-to-end in api-wired/spot (real Postgres); unit tests use mock DBs
and won't catch a real asyncpg type mismatch.
