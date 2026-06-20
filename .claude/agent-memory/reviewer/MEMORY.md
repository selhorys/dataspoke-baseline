# Reviewer Memory Index

- [Bash errexit / grep output](feedback_bash_errexit_grep_output.md) — no-match grep (exit 1) hides output and aborts scripts; pipe to cat or redirect to file
- [Dataset event entity types](project_dataset_event_entity_types.md) — only validation + metagen-candidate land on entity_type="dataset"; ingestion runs come via reverse-lookup
- [Verify generator dead-code claims](feedback_verify_generator_dead_code_claims.md) — grep "still used elsewhere" claims; refactors orphan old hooks/components
- [asyncpg str→UUID column](project_asyncpg_str_uuid_column.md) — str(uuid4) binds fine to UUID(as_uuid=True) col via asyncpg pgproto; unit mocks won't catch real mismatch
