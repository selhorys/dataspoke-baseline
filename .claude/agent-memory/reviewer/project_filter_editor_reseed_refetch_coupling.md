---
name: filter-editor-reseed-refetch-coupling
description: DatasetFilterEditor's raw-text retention (#146) depends on refetchOnWindowFocus false in app/providers.tsx — the conf pages' useEffect([conf]) reseed would otherwise clobber mid-edit
metadata:
  type: project
---

`DatasetFilterEditor` buffers raw textarea text and reseeds from props only when a list
dimension's **content** differs from what it last emitted. That guard is not self-contained:

- `app/(app)/ontogen/conf/page.tsx` and `app/(app)/metagen/conf/[id]/page.tsx` both run
  `useEffect(() => { if (conf) setDatasetFilter(conf.dataset_filter ?? {}) }, [conf])`.
- `conf` is a react-query result object. Any refetch that returns *different* data pushes a
  new filter down and legitimately reseeds the boxes — wiping whatever the user was typing.
- The only thing keeping that from firing mid-edit is `refetchOnWindowFocus: false` in
  `src/frontend/app/providers.tsx` (plus react-query structural sharing, which returns the
  same reference when the payload is deeply equal).

**Why:** the coupling spans three files and is invisible from the editor. Turning
`refetchOnWindowFocus` on, or adding a poll to those conf queries, silently re-breaks #146
on the two conf pages while every editor-level unit test stays green.

**How to apply:** when reviewing a change to the query-client defaults, to the conf pages'
seeding effects, or to the editor's reseed guard, check all three together. See
[[frontend-probe-silent-noop]] for how to probe this in jsdom — the editor alone needs no
Radix stubs; only its consumer forms (MetricForm / MetagenConfForm / OntogenConfForm) do.
