# Reference Directory (`ref/`)

This directory contains external source code and documentation for AI assistants to reference when working on DataSpoke.

## Purpose

When designing or implementing DataSpoke features, AI assistants (like Claude Code) can reference these materials to:

- Understand DataHub's data models, API patterns, and integration points
- Browse actual implementation examples from related projects
- Ensure compatibility with the exact versions deployed in `dev_env/`
- Study GraphQL schemas, Kafka event formats, and SDK usage patterns

## Directory Structure

```
ref/
├── github/               # Open source repositories cloned from GitHub
│   ├── datahub/          # DataHub OSS (version-locked to dev_env deployment)
│   └── kestra/           # Kestra workflow engine (version-locked to dev_env deployment)
└── README.md
```

## Versions

Reference materials are version-locked to match the DataSpoke development environment:

| Component | Version | Location | Source |
|-----------|---------|----------|--------|
| DataHub OSS | v1.4.0.3 | `github/datahub/` | https://github.com/datahub-project/datahub |
| Kestra | v1.3.3 | `github/kestra/` | https://github.com/kestra-io/kestra |

> **Note**: When `dev_env/.env` is updated with new DataHub versions, re-run the corresponding setup script to fetch matching source code.

## Setup

Run the setup script to download all reference materials:

```bash
cd ref
./setup.sh
```

Or download specific components:

```bash
./setup.sh datahub    # Download only DataHub source
./setup.sh kestra     # Download only Kestra source
./setup.sh --all      # Download everything (default)
```

## Usage Guidelines for AI Assistants

### When to Reference

- **DataHub Integration**: Designing Kafka consumers, GraphQL queries, or SDK usage
- **Data Models**: Understanding entities (Dataset, DataJob, DataFlow, etc.)
- **API Patterns**: Learning DataHub's REST/GraphQL API conventions before extending them
- **Event Schemas**: Parsing MCE/MAE events or emitting metadata changes

### What to Avoid

- **Do not copy code directly** — DataSpoke is Apache 2.0 licensed; DataHub may have different licensing
- **Do not assume parity** — DataSpoke is a sidecar, not a fork; architecture differs intentionally
- **Do not bundle reference code** — this directory is for local AI context only, excluded from builds

## Maintenance

- **Update schedule**: When `dev_env/.env` versions change
- **Cleanup**: Run `./setup.sh --clean` to remove all downloaded references before re-fetching
- **Disk usage**: DataHub OSS is ~500MB; budget accordingly

## .gitignore

All downloaded content is git-ignored. Only `README.md`, `setup.sh`, and other setup scripts are version-controlled.
