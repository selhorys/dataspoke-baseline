# Reviewer Memory Index

- [Bash errexit / grep output](feedback_bash_errexit_grep_output.md) — no-match grep (exit 1) hides output and aborts scripts; pipe to cat or redirect to file
- [Dataset event entity types](project_dataset_event_entity_types.md) — only validation + metagen-candidate land on entity_type="dataset"; ingestion runs come via reverse-lookup
- [Verify generator dead-code claims](feedback_verify_generator_dead_code_claims.md) — grep "still used elsewhere" claims; refactors orphan old hooks/components
- [asyncpg str→UUID column](project_asyncpg_str_uuid_column.md) — str(uuid4) binds fine to UUID(as_uuid=True) col via asyncpg pgproto; unit mocks won't catch real mismatch
- [Metagen conf Save button morph](project_metagen_conf_save_button_morph.md) — metagen conf moved Save into header slot; re-introduces submit-on-Edit hazard, needs keys + browser E2E
- [EXISTS subquery auto-correlate](feedback_exists_subquery_autocorrelate.md) — EXISTS over a table already in outer FROM raises InvalidRequestError at build; compile real SQL, mocks miss it
- [Helm stale local subchart tgz](project_helm_stale_local_subchart_tgz.md) — umbrella renders stale charts/*.tgz not subcharts/ source; verify via standalone render
- [Runtime env-file rename blast radius](project_runtime_envfile_rename_blast_radius.md) — renaming helm-charts/.env→.env.dev breaks ~17 hardcoded loaders/skills + a gitignore secret-leak gotcha
- ["No references remain" brace grep](feedback_no_references_remain_brace_grep.md) — `/hub` grep stays clean while `{auth,spoke,hub}` brace-list survives; grep token-level minus homographs
- [Recharts var() in SVG attr](project_recharts_css_var_svg_attr.md) — var() DOES resolve in SVG presentation attrs (Chromium-verified); Recharts hsl(var(--token)) stroke is fine, not a regression
- [FastAPI injected Response on error path](project_fastapi_injected_response_error_path.md) — endpoint raise ⇒ sub-response headers dropped; no Set-Cookie on error paths; MRO picks specific handler
- [Verify branch-reachability rationales](feedback_verify_branch_reachability_rationales.md) — auth is route-level Depends, so unauthenticated routes never 401; prove "unreachable" claims, watch float-vs-floor races
- [health-check.sh unattended lock prompt](project_healthcheck_unattended_lock_prompt.md) — no --keep-lock ⇒ interactive read aborts under set -e; exit 1 misread as "cluster red"
- [token_urlsafe scrub charset](project_token_urlsafe_scrub_charset.md) — dsk_/token_urlsafe are base64url; scrub regex needs [A-Za-z0-9_-]+ or ~74% of PATs leak a tail
- [mypy override audit](feedback_mypy_override_audit.md) — --warn-unused-configs misses over-granted codes; re-run mypy with overrides stripped to get the true suppressed set
- [Display-URL guard x3](project_display_url_guard_three_copies.md) — one safety regex hand-copied into pydantic + Python sanitizer + lib/safe-url.ts; diff-verify recipe
