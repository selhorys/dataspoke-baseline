# DataSpoke Baseline

AI Data Catalog Starter: Productized Scaffold to Generate Custom Solutions

![DataSpoke Concept](../assets/dataspoke_concept.jpg)

---

## 1. Background

### The Need for Custom Solutions

Data catalog solutions (e.g. DataHub, Dataplex, OpenMetadata) offer broad feature sets, yet real-world adoption rarely exploits their full potential. The root cause: by serving everyone, they are optimized for no one.

- Different user groups have fundamentally different needs. Data engineers want detailed technical specs and pipeline costs. Data analysts want domain-rich metadata for text-to-SQL. Data stewards want availability metrics and quality-check histories. Security teams want PII visibility at a glance. No single UI can serve all these purposes well.

- Beyond the UI, users need domain-specific capabilities — custom ML-based data quality modules, ingestion of non-standard data sources, and other extensions that generic catalogs do not support.

### New Requirements in the AI Era

As LLMs and AI agents enter day-to-day workflows, data catalogs must evolve beyond metadata repositories to fulfill two new functions:

- **Online Verifier**: Real-time validation of pipeline outputs within AI coding loops. By connecting the catalog via RAG or MCP (Model Context Protocol), it enables a TDD-like workflow — set up validation first, then develop pipelines against it.

- **Self-Organization & Self-Purification**: AI-driven design of data taxonomies and ontologies, with autonomous consistency checking and correction. As business data grows in complexity, enterprise-wide schemas and table/column definitions must stay current. The data catalog is the natural component to perform this work.

## 2. Project Definition

This project addresses the custom catalog problem with a framework for rapid creation of tailored data catalogs using coding agents.

The name **DataSpoke** treats the existing DataHub as the Hub and defines each specialized extension as a Spoke — like spokes on a wheel.

This repository delivers two artifacts:

- **Baseline Product** — A pre-built implementation of essential features for an AI-era catalog, targeting Data Engineers (DE), Data Analysts (DA), and Data Governance personnel (DG).
- **AI Scaffold** — Conventions, development specs, environment configuration, and Claude Code utilities that enable rapid construction of custom catalogs.

Users extend the baseline and leverage the AI Scaffold to run an Agentic Coding Loop.

## 3. Baseline Product

### Core Principles

To prevent redundant builds and cross-domain inconsistency:

- **DataHub-backed Backend**: DataHub stores metadata and serves as the Single Source of Truth (SSOT).
- **API Convention Compliance**: A unified API specification maintains consistency across all domains.

### System Architecture

DataSpoke consists of four components:

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

- **DataSpoke UI**: Portal-style interface with user-group entry points.
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
- **DataSpoke Backend/Pipeline**: Core logic — ingestion, quality validation, documentation, ontology generation.
- **DataHub**: Metadata SSOT.

### Features by User Group

#### Data Engineering (DE) Group

- **Deep Technical Spec Ingestion**: Collects platform-specific technical metadata — storage compression formats, Kafka topic replication levels, and similar details.
- **Online Data Validator**: Time-series monitoring and validation of data. Provides an API for dry-run validation (without writing to the store) and point-in-time validation against historical data.
- **Automated Documentation Generation**:
  - Generates documentation from source code references (e.g. GitHub links).
  - Highlights differences between similar tables.
  - Proposes enterprise-wide taxonomy and ontology standards; once approved, suggests downstream modifications.

#### Data Analysis (DA) Group

- **Natural Language Search**: Explore data tables using natural language queries.
- **Text-to-SQL Optimized Metadata**: Curated metadata focused on data content rather than technical specs, enabling AI to generate accurate SQL.
- **Online Data Validator**: Same functionality as the DE group.

#### Data Governance (DG) Group

- **Enterprise Metrics Time-Series Monitoring**: Dashboards tracking dataset counts per platform, total volume, data availability ratios, and more.
- **Multi-Perspective Data Overview**:
  - Taxonomy/ontology graph visualization with dataset coloring/sizing by statistics (2D/3D).
  - Medallion Architecture-based dataset overview.

## 4. AI Scaffold

Commands and skills for step-by-step development workflows.

- **Development Environment Setup**
  - GitHub clone and reference data setup
  - Local Kubernetes cluster-based dev environment provisioning
- **Development Planning**
  - Feature spec authoring: guided Q&A to define features per user group under `spec/feature/spoke/`, or common features under `spec/feature/`.
  - Implementation planning: tracked via GitHub Issues and PRs with AI coding approach suggestions (skill/subagent composition) to reinforce the scaffold.
- **PR Automation (PR-auto)**
  - Cron-driven monitoring of GitHub issues labeled `prauto:ready`; invokes Claude Code CLI to analyze, implement, and submit PRs autonomously. See `spec/AI_PRAUTO.md` for the full specification.
