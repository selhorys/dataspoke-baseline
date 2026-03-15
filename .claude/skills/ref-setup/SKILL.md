---
name: ref-setup
description: Download AI reference materials (external source code for AI assistant reference). Offers individual or all references.
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash(bash ref/*), Bash(ls *), Bash(test *), Bash(tail *), Bash(sleep *), Read
---

## Step 1 — Ask which reference to install

Use `AskUserQuestion` with the following options:

| Option | Label | Description |
|--------|-------|-------------|
| 1 | **all** | Download all reference materials (default) |
| 2 | **datahub** | Download only DataHub source code |
| 3 | **clean** | Remove all downloaded content |

Map the selected option to a `ref/setup.sh` argument:
- **all** → `bash ref/setup.sh` (no argument)
- **datahub** → `bash ref/setup.sh datahub`
- **clean** → `bash ref/setup.sh --clean`

## Step 2 — Run setup

Execute the setup script **in the background**: `bash ref/setup.sh <selected-option>`
- Note the background task ID and output file path.

## Step 3 — Monitor progress

While the script runs, **check the background task output every ~20 seconds**:
- Read the output file (e.g., `tail -30 <output-file>`) to report progress messages (cloning, cleanup steps, etc.).
- After each check, summarize what has happened since the last check.
- If an error is printed or the script exits with a non-zero code, report it immediately with the full output and stop.

Continue until the background script exits with exit code 0.

## Step 4 — Report to user

When the script exits successfully:
1. Tell the user that **all reference materials are ready**.
2. Show the location: `ref/github/datahub/`
3. Remind the user of the key directories available for AI reference:
   - `metadata-models/` — Entity schemas (PDL/Avro)
   - `metadata-service/` — GMS backend (Java/Spring)
   - `datahub-web-react/` — Frontend (TypeScript/React)
   - `metadata-ingestion/` — Python SDK and ingestion framework
   - `datahub-graphql-core/` — GraphQL API schemas
