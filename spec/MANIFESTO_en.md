# DataSpoke Baseline

A Baseline Product for an Omnipotent Data Catalog

![DataSpoke Concept](../assets/dataspoke_concept.jpg)

---

## 1. Background

### Capabilities a Data Catalog Should Provide in the AI Era

- **Self-Organization**: The data catalog autonomously constructs its ontology from the available
  data.
- **Self-Purification**: The data catalog inspects and cleans its own state based on the ontology.
  For example, it detects and reports errors in data documentation, or proposes data documentation
  generatively.
- **Online Quality Ledger**: The data catalog exposes APIs for data-quality tasks in the
  data pipeline to report or cache results.

### Custom Data Catalogs in the Era of Vibe Coding

Existing data catalog solutions offer vast feature sets, yet real-world adoption is often low.
The root cause: by trying to satisfy every user, they grow complex enough to be optimized for no
one in particular.

- Different user groups have fundamentally different needs:
  - Data Engineers: technical specs, pipeline costs
  - Data Analysts: domain-centric metadata for Text-to-SQL
  - Data Stewards: availability metrics, quality-check history
  - Security Teams: PII (personally identifiable information) usage
- Need for domain-specific capabilities: ML-based custom quality modules, ingestion of
  non-standard data sources that do not fit existing structures, and other extensions that generic
  catalogs cannot support.

In the era of Vibe Coding, building a custom data catalog that carries the capabilities above and
includes only what a specific company needs is not a hard task. That said, having a solid baseline
product to start that vibe coding from is not a bad thing either.

## 2. Project Definition

This project aims to develop the following two core artifacts:

- **Baseline Product** — A foundational data catalog implementation with self-organization,
  self-purification, and an online quality ledger.
- **Productized Scaffold** — A framework for custom development, comprising specs, a development
  environment, coding-agent utilities, and more.

Users extend this baseline to fit their own purposes, and leverage the provided Scaffold throughout
development.

The name **DataSpoke** draws on the idea of treating the existing DataHub as the Hub and each
organization's specialized extension as a spoke on a wheel.

### 2.1 Baseline Product

#### Features

- **Ingestion Control**: Convenience functions for configuring, controlling, and managing data
  ingestion in one place.
- **Validation**: Configurable storage of final and intermediate results of validation, used
  by data-quality tasks in the data pipeline.
- **Ontology Generation**: Autonomously constructs an ontology from DataHub-resident
  metadata (descriptions, schemas, glossary terms, document entities), maintained in a
  graph DB and a vector DB inside DataSpoke.
- **Metadata Generation**: Based on the ontology, inspects the state of data documentation and
  proposes metadata via generative AI, including APIs and a review process.
- **Governance**: APIs for configuring and monitoring governance metrics such as documentation
  coverage and data freshness.

#### System Architecture

DataSpoke consists of four components.

```
┌───────────────────────────────────────────────┐
│                 DataSpoke UI                  │
└───────────────────────┬───────────────────────┘
                        │
┌───────────────────────▼───────────────────────┐
│                DataSpoke API                  │
└───────────┬───────────────────────┬───────────┘
            │                       │
┌───────────▼───────────┐ ┌─────────▼───────────┐
│       DataHub         │ │      DataSpoke      │
│    (metadata SSOT)    │ │  Backend / Pipeline │
└───────────────────────┘ └─────────────────────┘
              High Level Architecture
```

- **DataSpoke UI**: A portal-style interface with per-user-group entry points.
  ```
  ┌─────────────────────────────────────────────┐
  │  Data Hub & Spokes                   Login  │
  │─────────────────────────────────────────────│
  │                                             │
  │              (DE)                           │
  │                 \                           │
  │                  \                          │
  │                   (Hub)----(DG)             │
  │                  /                          │
  │                 /                           │
  │              (DA)                           │
  │                                             │
  └─────────────────────────────────────────────┘
                  UI Main Page
  ```
- **DataSpoke API**: Three-tier URI structure.
  ```
  /api/v1/spoke/common/…       # Common features shared across user groups
  /api/v1/spoke/[de|da|dg]/…   # User-group-specific features
  /api/v1/hub/…                # DataHub pass-through (optional ingress for clients)
  ```
- **DataSpoke Backend/Pipeline**: Core logic — ingestion, validation, ontology generation,
  metadata generation, and governance (the five §2.1 features).
- **DataHub**: Metadata SSOT.

### 2.2 Productized Scaffold

#### AI Scaffold

A collection of Claude Code configurations under `.claude/` that lets the agent grasp the
project's structure, conventions, and spec hierarchy from the very first session. Includes
domain-specific skills, generator/evaluator subagents, and cron-based PR automation (PRauto). See
`spec/AI_SCAFFOLD.md` for the full specification.

#### Development Scaffold

A fully scripted Kubernetes-based development environment. The `dev_env/` directory holds
install/reinstall/uninstall scripts for core components — DataHub, PostgreSQL, Redis, Airflow, the
DataSpoke API, and more — and builds on the same Helm chart used in production
(`helm-charts/dataspoke/`) with a development overlay (`values-dev.yaml`) applied on top. See
`spec/feature/DEV_ENV.md` for the full specification.
