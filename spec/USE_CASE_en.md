# DataSpoke: Detailed Use Case Scenarios

> **Note on Document Purpose**
> This document presents conceptual scenarios for ideation and vision alignment. These use cases illustrate the intended capabilities and value propositions of DataSpoke, but are not implementation specifications or technical requirements. Actual implementation details, technical architecture, and feature prioritization will be defined in separate technical specification documents.

This document provides detailed, real-world scenarios demonstrating how DataSpoke enhances DataHub capabilities across its three user groups: **Data Engineering (DE)**, **Data Analysis (DA)**, and **Data Governance (DG)**.

All scenarios share a single imaginary company context — **Imazon**, an online bookstore — so that the use cases coexist and reinforce each other.

---

## Imaginary Company Profile: Imazon

Imazon is a 15-year-old online bookstore. Its data landscape reflects years of organic growth:

- **Legacy Oracle data warehouse** — 500+ tables covering book catalog, customers, orders, reviews, publishers, inventory, and shipping
- **Departments** — Engineering, Data Science, Marketing, Finance, Legal, Operations, Publisher Relations, Customer Support
- **Key data domains** — `catalog.*` (books, authors, genres), `customers.*`, `orders.*`, `reviews.*`, `recommendations.*`, `publishers.*`, `inventory.*`, `shipping.*`
- **DataHub adoption** — recently deployed; standard Oracle connector imported schema metadata but missed business context, stored-procedure lineage, and tribal knowledge locked in Confluence and spreadsheets

---

## Feature Mapping

| Use Case | User Group | Feature |
|----------|-----------|---------|
| [Use Case 1: Deep Ingestion — Legacy Book Catalog Enrichment](#use-case-1-deep-ingestion--legacy-book-catalog-enrichment) | DE | Deep Technical Spec Ingestion |
| [Use Case 2: Data Validation — Assertion-Based Data Quality Monitoring](#use-case-2-data-validation--assertion-based-data-quality-monitoring) | DE / DA | DataHub Assertion Management |
| [Use Case 3: Predictive SLA — Timeseries Validation for Early Warning](#use-case-3-predictive-sla--timeseries-validation-for-early-warning) | DE | DataHub Assertion Management |
| [Use Case 4: Doc Generation — Post-Acquisition Ontology Reconciliation](#use-case-4-doc-generation--post-acquisition-ontology-reconciliation) | DE | Automated Documentation Generation |
| [Use Case 5: NL Search — GDPR Compliance Audit](#use-case-5-nl-search--gdpr-compliance-audit) | DA | Natural Language Search |
| [Use Case 6: Metrics Dashboard — Enterprise Metadata Health](#use-case-6-metrics-dashboard--enterprise-metadata-health) | DG | Enterprise Metrics Time-Series Monitoring |
| [Use Case 7: Text-to-SQL Metadata — AI-Assisted Genre Analysis](#use-case-7-text-to-sql-metadata--ai-assisted-genre-analysis) | DA | Text-to-SQL Optimized Metadata |
| [Use Case 8: Multi-Perspective Overview — Enterprise Data Visualization](#use-case-8-multi-perspective-overview--enterprise-data-visualization) | DG | Multi-Perspective Data Overview |

---

## Data Engineering (DE) Group

### Use Case 1: Deep Ingestion — Legacy Book Catalog Enrichment

**Feature**: Deep Technical Spec Ingestion

#### Scenario: Enriching the Legacy Oracle Book Catalog

**Background:**
Imazon's Oracle data warehouse holds 500+ tables built over 15 years. Standard DataHub connectors captured schema metadata — table names, column types, primary keys — but missed the rich business context stored outside the database: Confluence pages describing editorial taxonomy, Excel feeds from publishers mapping ISBNs to imprints, an internal API for genre classification, and lineage hidden inside PL/SQL stored procedures that compute bestseller rankings and royalty calculations.

#### Without DataSpoke

Standard Oracle connector output: 500 tables with column types and keys — nothing else. No business descriptions (stored in Confluence), no publisher metadata (Excel), no genre taxonomy (API), no stored-proc lineage. Data consumers browse DataHub and see bare technical schemas with no way to determine what `catalog.title_master` actually tracks or how `reports.monthly_royalties` is computed.

#### With DataSpoke

**Register a multi-source enrichment config:**

```python
# PUT /api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf
dataspoke.ingestion.register_config({
  "name": "oracle_book_catalog_enriched",
  "source_type": "ORACLE",
  "schedule": "0 2 * * *",  # Daily at 2 AM

  "enrichment_sources": [
    {
      "type": "confluence",
      "space": "BOOK_DATA_DICTIONARY",
      "page_prefix": "Table: ",
      "fields_mapping": {
        "description": "confluence.content.body",
        "business_owner": "confluence.labels.owner",
        "pii_classification": "confluence.labels.pii"
      }
    },
    {
      "type": "excel",
      "path": "s3://imazon-docs/publisher-feeds/isbn-imprint-mapping.xlsx",
      "sheet": "ISBN_Classifications",
      "key_column": "table_name",
      "fields_mapping": {
        "publisher_domain": "Imprint",
        "content_rating": "Rating",
        "genre_taxonomy": "Genre_Path"
      }
    },
    {
      "type": "custom_api",
      "endpoint": "https://taxonomy-api.imazon.internal/genres",
      "auth": "bearer_token",
      "fields_mapping": {
        "genre_hierarchy": "$.genre.path",
        "editorial_tags": "$.genre.editorial_tags"
      }
    }
  ],

  "custom_extractors": [
    {
      "name": "plsql_lineage_parser",
      "type": "python_function",
      "module": "dataspoke.custom.oracle_lineage",
      "function": "extract_stored_proc_lineage",
      "params": { "parse_insert_select": true, "parse_merge_statements": true }
    },
    {
      "name": "quality_rule_extractor",
      "type": "python_function",
      "module": "dataspoke.custom.oracle_quality",
      "function": "extract_check_constraints_as_rules"
    }
  ]
})
```

**Custom PL/SQL lineage extractor** (excerpt):

```python
# dataspoke/custom/oracle_lineage.py
class OraclePLSQLLineageExtractor(CustomExtractor):
    def extract_stored_proc_lineage(self, procedure_name, procedure_body, params):
        lineage_edges = []
        for stmt in sqlparse.parse(procedure_body):
            if self._is_insert_select(stmt):
                for source in self._extract_source_tables(stmt):
                    lineage_edges.append(LineageEdge(
                        source_urn=f"urn:li:dataset:(urn:li:dataPlatform:oracle,{source},PROD)",
                        target_urn=f"urn:li:dataset:(urn:li:dataPlatform:oracle,{self._extract_target_table(stmt)},PROD)",
                        transformation_type="stored_procedure",
                        transformation_logic=procedure_name,
                        confidence_score=0.95
                    ))
        return lineage_edges
```

**Enriched metadata example — `catalog.title_master`:**

```yaml
Dataset: catalog.title_master
Platform: Oracle / DWPROD

# Base Schema (Standard Connector)
Columns: 62 | Primary Key: isbn, edition_id

# Enriched — Business Context (Confluence)
Description: |
  Master catalog of all book titles. One row per ISBN+edition.
  Source of truth for pricing, availability, and editorial classification.
  Updated nightly from publisher feeds and editorial review queue.

# Enriched — Ownership (Confluence + HR API)
Owner: maria.garcia@imazon.com | Team: Catalog Engineering

# Enriched — Publisher Metadata (Excel)
Publisher Domain: All imprints | Genre Taxonomy: 4-level hierarchy

# Enriched — Lineage (PL/SQL Parser)
Upstream: publishers.feed_raw, editorial.review_queue, pricing.base_rates
Generated By: PROC_NIGHTLY_CATALOG_REFRESH (stored procedure)
Downstream: recommendations.book_features, reports.catalog_summary

# Enriched — Quality Rules (CHECK Constraints)
1. list_price > 0
2. publication_date <= SYSDATE
3. isbn IS NOT NULL AND LENGTH(isbn) IN (10, 13)
```

#### DataHub Integration Points

All enriched metadata is persisted to DataHub via `DatahubRestEmitter`, which internally calls the OpenAPI endpoint `POST /openapi/v3/entity/dataset`. Each category maps to a DataHub aspect emitted as an MCP:

| Ingestion Phase | DataHub Aspect | REST API Path | What It Stores |
|----------------|---------------|---------------|----------------|
| Base Schema | `schemaMetadata` | `POST /openapi/v3/entity/dataset` | Column names, types, keys |
| Business Descriptions | `datasetProperties` | `POST /openapi/v3/entity/dataset` | `description` from Confluence |
| PII / Editorial Tags | `globalTags` | `POST /openapi/v3/entity/dataset` | `urn:li:tag:PII`, `urn:li:tag:Editorial_Reviewed` |
| Publisher Classification | `datasetProperties.customProperties` | `POST /openapi/v3/entity/dataset` | `publisher_domain`, `genre_taxonomy`, `content_rating` |
| Ownership | `ownership` | `POST /openapi/v3/entity/dataset` | Owner URN + `BUSINESS_OWNER` type |
| PL/SQL Lineage | `upstreamLineage` | `POST /openapi/v3/entity/dataset` | Source → target dataset URN edges |
| Quality Rules | `assertionInfo` + `assertionRunEvent` | `POST /openapi/v3/entity/assertion` | CHECK constraints as assertions |

```python
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    UpstreamClass,
    UpstreamLineageClass,
)

emitter = DatahubRestEmitter(gms_server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN)
dataset_urn = make_dataset_urn(platform="oracle", name="catalog.title_master", env="PROD")

# Description + custom properties — business context from Confluence
# REST: POST /openapi/v3/entity/dataset  (aspect: datasetProperties)
emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=dataset_urn,
    aspect=DatasetPropertiesClass(
        description="Master catalog of all book titles...",
        customProperties={"genre_taxonomy": "4-level", "publisher_domain": "All imprints"},
    ),
))

# Lineage — upstream tables extracted from PL/SQL stored procedures
# REST: POST /openapi/v3/entity/dataset  (aspect: upstreamLineage)
emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=dataset_urn,
    aspect=UpstreamLineageClass(
        upstreams=[UpstreamClass(
            dataset=make_dataset_urn(platform="oracle", name="publishers.feed_raw", env="PROD"),
            type=DatasetLineageTypeClass.TRANSFORMED,
        )],
    ),
))
```

> **Key point**: DataHub provides no enrichment logic — it only persists what DataSpoke sends.

#### DataSpoke Custom Implementation

| Component | Responsibility | Why DataHub Can't Do This |
|-----------|---------------|--------------------------|
| **Ingestion Config Registry** | Store enrichment configs (connections, field mappings, extractors) | DataHub recipes handle standard connectors only |
| **Enrichment Source Connectors** | Fetch from Confluence, Excel/S3, taxonomy API | DataHub connectors are database/platform-focused |
| **Custom Extractor Framework** | Plugin system for PL/SQL lineage parsing, CHECK constraint extraction | Parsing stored procedure bodies is outside DataHub's scope |
| **Field Mapping Engine** | Map Confluence labels → tags, Excel columns → custom properties | DataHub accepts structured aspects but doesn't transform unstructured inputs |
| **Orchestration (Kestra)** | Schedule, per-phase retry, notifications | DataHub runs recipes atomically; multi-source orchestration requires Kestra |
| **Vector Index Sync** | Generate embeddings → Qdrant on successful ingestion | DataHub has Elasticsearch keyword search, not vector similarity |

#### Outcome

| Aspect | Standard Connector | DataSpoke Deep Ingestion |
|--------|-------------------|--------------------------|
| Schema Coverage | 500 tables | 500 tables |
| Business Descriptions | 0% | 89% (445/500) |
| Ownership | 0% | 74% (370/500) |
| Genre / Publisher Tags | 0% | 100% |
| Stored Proc Lineage | Not supported | 210 edges extracted |
| Quality Rules | Manual entry only | 380 auto-extracted |
| Update Frequency | Manual re-run | Automated daily |

---

### Use Case 2: Data Validation — Assertion-Based Data Quality Monitoring

**Feature**: DataHub Assertion Management (shared with DA group)

#### Design Philosophy

DataSpoke does **not** build its own data quality scoring engine. It provides a **convenience and customization layer on top of DataHub's native assertion framework**:

- Register all 6 types of DataHub assertion rules per dataset: freshness, volume, field, schema, SQL, custom
- Unified configuration and result retrieval through DataSpoke API
- JSON format compatible with DataHub's Open Assertions Spec, with DataSpoke extensions
- Partition-aware execution with cron and manual scheduling
- DataSpoke-original validation logic via the `custom` type (e.g., SQL-based timeseries rule)
- All assertion results stored in DataHub as `assertionRunEvent` timeseries aspects

#### Validation Configuration Model

**Configuration API**: `PUT /api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf`

A dataset's validation config is a JSON object with two parts:

1. **Schedule** — singleton per dataset. Only `cron` and `manual` are supported, and both can be active simultaneously. While cron runs checks on a schedule, users can also trigger a manual validation run at any time.

2. **Rules** — JSON list where each item is a dictionary compatible with DataHub's Open Assertions Spec, with the following DataSpoke extensions:
   - `rule_id` — auto-generated unique identifier for each rule
   - `partition` and `order` variables (like SQL window functions) — determine the default (latest) target partition when cron-triggered. All 6 rule types support these.

**Example — `reviews.user_ratings` validation config:**

```json
{
  "schedule": {
    "cron": "0 */6 * * *",
    "manual": true
  },
  "rules": [
    {
      "rule_id": "r-fresh-001",
      "type": "freshness",
      "lookback_interval": "6 hours",
      "last_modified_field": "updated_at",
      "partition": {"field": "load_date", "order": "desc"}
    },
    {
      "rule_id": "r-vol-001",
      "type": "volume",
      "metric": "row_count",
      "condition": {"type": "between", "min": 10000, "max": 5000000},
      "partition": {"field": "load_date", "order": "desc"}
    },
    {
      "rule_id": "r-field-001",
      "type": "field",
      "field": "rating_score",
      "metric": "null_count",
      "condition": {"type": "less_than_or_equal_to", "value": 100},
      "partition": {"field": "load_date", "order": "desc"}
    },
    {
      "rule_id": "r-schema-001",
      "type": "schema",
      "fields": [
        {"field": "user_id", "type": "VARCHAR"},
        {"field": "isbn", "type": "VARCHAR"},
        {"field": "rating_score", "type": "NUMBER"}
      ],
      "compatibility": "superset"
    },
    {
      "rule_id": "r-sql-001",
      "type": "sql",
      "statement": "SELECT COUNT(*) FROM reviews.user_ratings WHERE rating_score < 1 OR rating_score > 5",
      "condition": {"type": "equal_to", "value": 0},
      "partition": {"field": "load_date", "order": "desc"}
    },
    {
      "rule_id": "r-custom-ts-001",
      "type": "custom",
      "subtype": "sql_timeseries",
      "description": "Daily volume and null-rate trend for anomaly detection",
      "sql": "SELECT load_date, COUNT(*) AS row_count, SUM(CASE WHEN rating_score IS NULL THEN 1 ELSE 0 END)::float / COUNT(*) AS null_rate FROM reviews.user_ratings GROUP BY load_date",
      "partition": ["load_date"],
      "order": ["load_date"],
      "values": ["row_count", "null_rate"],
      "ml_validation": {
        "targets": ["null_rate"],
        "model": "range",
        "lookback_partitions": 30
      }
    }
  ]
}
```

#### Custom Type: SQL-Based Timeseries Rule

The `custom` type with `subtype: "sql_timeseries"` enables DataSpoke-original validation:

- **Applicability**: SQL-runnable datasets (e.g., PostgreSQL, Trino, Snowflake)
- **Data manipulation SQL**: defines the view over which validation operates
- **Partition, order, value variables**: like SQL window functions — define the granularity and metrics to track
- **ML-based validation** (optional): for selected value columns, configure model type, validation range, lookback window, etc.

#### Execution Flow

When a cron or manual invocation is triggered for a dataset's validation rules:

- **Manual invocation** (`POST /api/v1/spoke/common/data/{dataset_urn}/attr/validation/method/run`): if a partition is given in the request body, that partition is targeted; otherwise the latest partition is targeted.
- **Cron invocation**: always targets the latest partition (determined by partition/order variables).

For the target partition, metrics defined by each rule are calculated and validated. For example, the SQL-based timeseries rule `r-custom-ts-001` above when cron-triggered:

**Step 1** — Latest partition's values are calculated:
```json
{"partitions": {"load_date": "2025-03-10"}, "values": {"row_count": 48230, "null_rate": 0.003}}
```

**Step 2** — Based on historical records, selected values from the current partition are validated (per `ml_validation` config):
```json
{"partitions": {"load_date": "2025-03-10"}, "values": {"row_count": 48230, "null_rate": 0.003}, "validation": {"null_rate": true}}
```

**Step 3** — Current partition's result is stored to DataHub as `assertionRunEvent`, including the validation result (pass/fail per target, actual values, partition context).

**Result History API**: `GET /api/v1/spoke/common/data/{dataset_urn}/attr/validation/result` — retrieves validation result history for the dataset from DataHub's `assertionRunEvent` timeseries.

#### Scenario: AI Agent Builds a Book Recommendation Pipeline

**Background:**
A data scientist asks an AI Agent: "Build a daily book recommendation pipeline using `reviews.user_ratings` and `orders.purchase_history`." Before building, the agent checks existing validation results to select healthy data sources.

##### Without DataSpoke

The AI Agent searches DataHub for "reviews" and "orders" tables, picks candidates by naming convention, generates code without checking data quality, and deploys a pipeline using `reviews.user_ratings_legacy` — which has a 30% null rate on `rating_score` since a migration bug last week. DataHub has no registered assertions for this table, so no quality signal exists.

##### With DataSpoke

**Step 1: Check existing validation results**

The DE team has already configured validation rules for key tables. The AI Agent queries recent results:

```
GET /api/v1/spoke/common/data/urn:li:dataset:...reviews.user_ratings/attr/validation/result

Response — last 5 cron runs all passed:
  r-field-001 (null_count on rating_score): ✓ 0 nulls per partition
  r-custom-ts-001 (null_rate timeseries): ✓ null_rate=0.003 within range
  r-vol-001 (row_count): ✓ 48,230 rows (within 10K–5M)

GET /api/v1/spoke/common/data/urn:li:dataset:...reviews.user_ratings_legacy/attr/validation/result

Response — last run FAILED:
  r-custom-ts-001 (null_rate timeseries): ✗ null_rate=0.30 — anomaly detected
    Historical baseline: 0.003 ±0.002 (30-day range model)
    Current: 0.30 — 150x above baseline
```

The agent picks `reviews.user_ratings` (all assertions passing) and avoids the legacy table.

**Step 2: Register validation rules for the new pipeline output**

```
PUT /api/v1/spoke/common/data/urn:li:dataset:...book_recommendation_features/attr/validation/conf

{
  "schedule": {"cron": "30 9 * * *", "manual": true},
  "rules": [
    {"type": "freshness", "lookback_interval": "24 hours", "last_modified_field": "created_at",
     "partition": {"field": "run_date", "order": "desc"}},
    {"type": "volume", "metric": "row_count",
     "condition": {"type": "greater_than", "value": 0},
     "partition": {"field": "run_date", "order": "desc"}},
    {"type": "field", "field": "user_id", "metric": "null_count",
     "condition": {"type": "equal_to", "value": 0},
     "partition": {"field": "run_date", "order": "desc"}}
  ]
}
```

**Step 3: DA validates fitness-for-use**

A marketing analyst checks `orders.purchase_history` before connecting to Tableau:

```
GET /api/v1/spoke/common/data/urn:li:dataset:...orders.purchase_history/attr/validation/result

Response — recent results:
  r-fresh-001 (freshness): ✓ Updated 45 min ago (lookback: 1 hour)
  r-schema-001 (schema): ✓ All required fields present
  r-vol-001 (row_count): ✓ 1.2M rows (within expected range)
→ All assertions passing — safe to connect.
```

#### DataHub Integration Points

DataSpoke is a **read/write** layer on DataHub's assertion framework:

| Operation | DataHub Aspect | REST API Path | Direction |
|-----------|---------------|---------------|-----------|
| Register assertion rules | `assertionInfo` | `POST /openapi/v3/entity/assertion` | **Write** |
| Report assertion results | `assertionRunEvent` | `POST /openapi/v3/entity/assertion` | **Write** |
| Retrieve result history | `assertionRunEvent` (timeseries) | `POST /aspects?action=getTimeseriesAspectValues` | Read |
| Schema for schema assertions | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | Read |

> **Key point**: DataHub stores assertion definitions and run results. Open-source DataHub does not execute assertions — DataSpoke is the execution engine that evaluates rules, runs SQL against source systems, performs ML-based validation, and reports results back to DataHub.

#### DataSpoke Custom Implementation

| Component | Responsibility | Why DataHub Can't Do This |
|-----------|---------------|--------------------------|
| **Assertion Config API** | Unified CRUD for all 6 rule types per dataset, with partition support | DataHub has no per-dataset config management layer |
| **Partition-Aware Executor** | Determine target partition, run rule against it, store result | Open-source DataHub doesn't execute assertions |
| **SQL-Based Timeseries Engine** | Execute data manipulation SQL, compute partition values, track history | DataHub stores profiles but can't query source systems |
| **ML-Based Anomaly Validation** | Range/model-based validation against historical partition values | DataHub has no statistical analysis |
| **Cron/Manual Scheduler** | Kestra-orchestrated cron + on-demand manual triggering | DataHub has no scheduler (open-source) |

#### Outcome

| Metric | Without DataSpoke | With DataSpoke |
|--------|------------------|----------------|
| Assertion setup effort | Manual per-assertion API calls to DataHub | Unified config per dataset |
| Execution | None (open-source DataHub) | Automated via cron + manual |
| Partition awareness | None | Built-in — latest partition targeted automatically |
| Custom timeseries checks | Not possible | SQL-based rules with ML validation |

---

### Use Case 3: Predictive SLA — Timeseries Validation for Early Warning

**Feature**: DataHub Assertion Management (timeseries monitoring)

This use case demonstrates how the SQL-based timeseries rule (the `custom` type from UC2) can be applied for SLA monitoring and pre-breach alerting.

#### Scenario: Shipping Partner API Rate-Limiting Threatens Order Fulfillment Dashboard

**Background:**
Imazon's `orders.daily_fulfillment_summary` table powers the logistics dashboard used by Operations, Finance, and Customer Support. It normally processes 1.5M rows daily by 9 AM, aggregating data from `orders.raw_events`, `shipping.carrier_status`, and an external shipping partner API.

##### Validation Configuration

The DE team has registered the following validation config:

```json
{
  "schedule": {
    "cron": "0 7,8 * * *",
    "manual": true
  },
  "rules": [
    {
      "rule_id": "r-fresh-ffs-001",
      "type": "freshness",
      "lookback_interval": "24 hours",
      "last_modified_field": "summary_date",
      "partition": {"field": "summary_date", "order": "desc"}
    },
    {
      "rule_id": "r-vol-ffs-001",
      "type": "volume",
      "metric": "row_count",
      "condition": {"type": "greater_than_or_equal_to", "value": 1000000},
      "partition": {"field": "summary_date", "order": "desc"}
    },
    {
      "rule_id": "r-custom-ffs-001",
      "type": "custom",
      "subtype": "sql_timeseries",
      "description": "Hourly volume accumulation trend for SLA prediction",
      "sql": "SELECT summary_date, hour_bucket, SUM(order_count) AS cumulative_orders, COUNT(DISTINCT carrier_id) AS active_carriers FROM orders.daily_fulfillment_summary GROUP BY summary_date, hour_bucket",
      "partition": ["summary_date"],
      "order": ["hour_bucket"],
      "values": ["cumulative_orders", "active_carriers"],
      "ml_validation": {
        "targets": ["cumulative_orders"],
        "model": "range",
        "lookback_partitions": 28,
        "validation_range": "day_of_week"
      }
    }
  ]
}
```

##### Without DataSpoke

```
9:00 AM — Alert: orders.daily_fulfillment_summary is empty
Status: SLA BREACH — logistics dashboard down
Response: Manual investigation begins. Root cause found at 10:30 AM (shipping API throttled).
Total downtime: 2.5 hours.
```

##### With DataSpoke

**7:00 AM — Cron-triggered validation runs:**

```
Cron fires for orders.daily_fulfillment_summary (schedule: "0 7,8 * * *")
Target: latest partition (summary_date=2025-03-10, hour_bucket=07)

Rule r-fresh-ffs-001 (freshness): ✓ last update 20 min ago
Rule r-vol-ffs-001 (volume): ✗ 320K rows — expected ≥1,000,000
Rule r-custom-ffs-001 (timeseries):
  Step 1 — values calculated:
    {"partitions": {"summary_date": "2025-03-10", "hour_bucket": "07"},
     "values": {"cumulative_orders": 320000, "active_carriers": 3}}
  Step 2 — ML validation against 28-day weekday baseline:
    cumulative_orders: 320K vs expected 900K ±5% (weekday 7AM) → ✗ FAIL
    {"validation": {"cumulative_orders": false}}
  Step 3 — stored to DataHub as assertionRunEvent (FAILURE)
```

**Alert generated from failed assertion result:**

```
⚠ Validation FAILED: orders.daily_fulfillment_summary

r-vol-ffs-001: row_count 320,000 < 1,000,000 (min threshold)
r-custom-ffs-001: cumulative_orders 320K — 64% below weekday 7AM baseline (900K ±5%)
  28-day model: Mon=790K, Tue–Fri=900K at hour 07
  Current: 320K — anomaly detected
```

**7:15 AM** — Operations engineer reviews the result, investigates upstream systems, finds shipping API throttling. Requests quota increase. Pipeline recovers by 8:00 AM.

**8:00 AM — Second cron run:**

```
Rule r-custom-ffs-001 (timeseries):
  {"partitions": {"summary_date": "2025-03-10", "hour_bucket": "08"},
   "values": {"cumulative_orders": 1120000, "active_carriers": 8}}
  ML validation: cumulative_orders 1.12M within expected range → ✓ PASS
```

SLA met with 1-hour buffer. Full result history available via:

```
GET /api/v1/spoke/common/data/urn:li:dataset:...orders.daily_fulfillment_summary/attr/validation/result

→ Returns assertionRunEvent timeseries: partition-by-partition pass/fail history
  with actual values, baseline comparison, and validation verdicts.
```

#### DataSpoke Custom Implementation

| Component | Responsibility | Why DataHub Can't Do This |
|-----------|---------------|--------------------------|
| **SQL-Based Timeseries Engine** | Execute accumulation SQL, compute hourly partition values | Open-source DataHub doesn't query source systems |
| **Day-of-Week Baseline Model** | Learn weekday patterns from 28-day history, adjust thresholds per day | DataHub stores raw data, no pattern learning |
| **Assertion-Based Alerting** | Generate alerts when `assertionRunEvent` records FAILURE | Open-source DataHub has no alerting engine |
| **Cron Scheduler** | Kestra flow triggers validation at 7 AM and 8 AM daily | DataHub has no scheduler (open-source) |

#### Outcome

| Metric | Traditional Monitoring | DataSpoke Timeseries Validation |
|--------|----------------------|--------------------------------|
| Detection time | 9:00 AM (SLA breach) | 7:00 AM (pre-breach) |
| Response window | 0 min (already late) | 120 min (proactive) |
| Business impact | 2.5hr dashboard downtime | Zero downtime |
| Setup effort | Custom monitoring code | Declarative config via API |

---

### Use Case 4: Doc Generation — Post-Acquisition Ontology Reconciliation

**Feature**: Automated Documentation Generation (taxonomy/ontology proposals)

#### Scenario: Imazon Acquires "eBookNow" Digital Startup

**Background:**
Imazon acquires eBookNow, a digital-only book platform. Post-merger, the combined DataHub catalog has 700+ datasets — 200 from eBookNow — with overlapping concepts. Six tables represent the idea of "a book/product" differently across the two companies. The data governance team cannot manually audit 700 datasets.

#### Without DataSpoke

```
Concept: "Book / Product"

Imazon (legacy):
  - catalog.title_master          → isbn, title, author_id, list_price
  - catalog.editions              → edition_id, isbn, format, pub_date
  - inventory.book_stock          → isbn, warehouse_id, qty_on_hand

eBookNow (acquired):
  - products.digital_catalog      → product_id, title, creator, price_usd
  - content.ebook_assets          → asset_id, product_ref, file_format
  - storefront.listing_items      → listing_id, item_name, seller_price

Problems:
  ✗ 6 tables represent "Book/Product" with different schemas and naming
  ✗ No documented relationship between isbn and product_id
  ✗ Downstream pipelines join across them inconsistently
  ✗ Recommendation engine double-counts titles available in both print and digital
```

#### With DataSpoke

**Phase 1: LLM-Powered Semantic Clustering**

DataSpoke uses an external LLM API to perform deep semantic analysis of all 700 datasets. Rather than relying solely on embedding cosine similarity, the LLM reasons over schema metadata, column names, descriptions, and sample values to identify conceptual overlaps that surface-level similarity metrics would miss.

```python
# Simplified illustration of LLM-driven clustering (via LangChain)
from langchain.chat_models import ChatOpenAI
from langchain.chains import LLMChain
from langchain.prompts import ChatPromptTemplate

llm = ChatOpenAI(model="gpt-4o", temperature=0)

# Step 1a: LLM classifies each dataset into business concept categories
classify_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a data catalog analyst. Given a table's schema and metadata, "
               "classify it into one or more business concept categories."),
    ("human", "Table: {table_name}\nColumns: {columns}\nDescription: {description}\n"
              "Sample values: {samples}\n\nClassify into business concepts.")
])
classify_chain = LLMChain(llm=llm, prompt=classify_prompt)

# Step 1b: For tables in the same concept category, LLM performs pairwise analysis
compare_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a data governance expert. Compare two tables that appear to "
               "represent the same business concept. Identify overlaps, key differences, "
               "and recommend merge/keep/deprecate."),
    ("human", "Table A: {table_a_schema}\nTable B: {table_b_schema}\n"
              "Lineage overlap: {shared_consumers}\nSample record overlap: {overlap_pct}")
])
compare_chain = LLMChain(llm=llm, prompt=compare_prompt)
```

```
DataSpoke Doc Generation — LLM-Powered Semantic Clustering:

Analyzed: 700 datasets (schema + descriptions + sample values sent to LLM)
Semantic clusters detected: 38
Clusters with conflicts: 9

Cluster: BOOK / PRODUCT (Critical)
6 tables detected representing the same concept

LLM Semantic Analysis:
  catalog.title_master   ←→ products.digital_catalog
    LLM Verdict: "Both represent a book/product entity. title_master is print-centric
                  (ISBN-required), digital_catalog is digital-only (product_id-based).
                  author_id (FK) vs creator (free-text) is a key structural difference."
    Confidence: 0.95

  catalog.editions       ←→ content.ebook_assets
    LLM Verdict: "Complementary views of the same concept — editions tracks physical
                  format variations, ebook_assets tracks digital file formats and DRM."
    Confidence: 0.91

  inventory.book_stock   ←→ storefront.listing_items
    LLM Verdict: "Low conceptual overlap — book_stock is warehouse inventory,
                  listing_items is marketplace pricing. Shared only by product reference."
    Confidence: 0.78

Evidence (LLM-augmented):
  - All 6 contain title-like fields (100% semantic match via LLM reasoning)
  - All 6 contain a price field (95% match — LLM noted currency differences)
  - Overlapping downstream lineage: 18 shared consumers
  - Sample record overlap (estimated): 72% by ISBN/title match
  - LLM insight: "creator" field in eBookNow is free-text, not a normalized FK —
    simple column-name matching would have missed this distinction
```

**Phase 1b: LLM-Assisted Source Code Reference Analysis**

DataSpoke scans eBookNow's linked GitHub repository (`ebooknow/catalog-service`, `ebooknow/storefront-api`) for inline SQL, DBT models, and application code that references the clustered tables. The LLM API interprets code context to generate accurate column descriptions and identify business logic embedded in application code:

```python
# LLM extracts business context from source code references
from langchain.chat_models import ChatOpenAI
from langchain.chains import LLMChain
from langchain.prompts import ChatPromptTemplate
from langchain.text_splitter import RecursiveCharacterTextSplitter

llm = ChatOpenAI(model="gpt-4o", temperature=0)

code_analysis_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a code analyst. Given source code that references a database column, "
               "generate a business-level column description. Include: purpose, constraints, "
               "relationships to other tables, and any business logic applied to this column."),
    ("human", "Column: {table}.{column}\nCode references:\n{code_snippets}\n"
              "Existing schema context: {schema_context}")
])
code_chain = LLMChain(llm=llm, prompt=code_analysis_prompt)
```

```
Source Code Reference Analysis — BOOK / PRODUCT cluster:

Repositories scanned: 3 (catalog-service, storefront-api, data-pipelines)
Code references found: 147
LLM-analyzed references: 147 (batch-processed via LangChain)

Sample Finding — products.digital_catalog.creator:
  File: catalog-service/src/models/product.py:42
  Usage: creator = db.Column(String(255))  # Free-text author name
  LLM Insight: "creator is a free-text field (not a normalized FK like author_id).
                No validation against an authors table. Publisher-entered, may contain
                multiple authors as comma-separated string."
  → Suggests differentiated description vs catalog.title_master.author_id

LLM-Generated Column Descriptions (from code + schema context):
  products.digital_catalog.creator    → "Free-text author/creator name entered by publisher.
                                         Unlike catalog.title_master.author_id, this is NOT
                                         a foreign key to authors table. May contain multiple
                                         names (comma-separated). No normalization applied."
  products.digital_catalog.price_usd  → "Publisher-set retail price in USD.
                                         Updated via storefront-api/pricing endpoint.
                                         No currency conversion (USD-only). Validated > 0
                                         in checkout flow (storefront-api/cart.py:88)."
  content.ebook_assets.file_format    → "Digital file format enum: EPUB, PDF, MOBI.
                                         Validated in catalog-service upload handler.
                                         MOBI deprecated since 2023 — LLM detected warning
                                         log in upload_handler.py:156."
```

**Phase 1c: LLM-Driven Similar Table Differentiation Report**

DataSpoke sends each table pair's full schema, sample data, lineage, and code references to the LLM, which generates a structured differentiation report with merge/keep/deprecate recommendations:

```python
# LLM generates differentiation report for each table pair
diff_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a data governance advisor. Given two similar tables, produce a "
               "differentiation report. For each pair, state: key structural difference, "
               "overlap percentage rationale, and a clear recommendation (MERGE / KEEP / DEPRECATE)."),
    ("human", "Table A: {table_a}\nSchema: {schema_a}\nDescription: {desc_a}\n"
              "Table B: {table_b}\nSchema: {schema_b}\nDescription: {desc_b}\n"
              "Shared consumers: {shared_consumers}\nRecord overlap: {overlap_pct}%")
])
diff_chain = LLMChain(llm=llm, prompt=diff_prompt)
```

```
LLM-Generated Differentiation Report — BOOK / PRODUCT cluster:

┌─────────────────────────┬──────────────────────────┬────────────┐
│ Table Pair              │ Key Difference           │ Overlap %  │
├─────────────────────────┼──────────────────────────┼────────────┤
│ catalog.title_master    │ Print-focused SSOT       │            │
│ vs                      │ Requires ISBN (NOT NULL) │ 72%        │
│ products.digital_catalog│ Digital-only, no ISBN    │            │
│                         │ required (30% lack ISBN) │            │
│ LLM Recommendation: MERGE — create catalog.product_master       │
│ LLM Rationale: "Core entity is identical (a book product).      │
│   ISBN optionality is the only structural blocker. Surrogate    │
│   product_id resolves this. creator→author_id mapping needs     │
│   a normalization pipeline."                                    │
├─────────────────────────┼──────────────────────────┼────────────┤
│ catalog.editions        │ Edition-level detail     │            │
│ vs                      │ (format, pub_date)       │ 65%        │
│ content.ebook_assets    │ Digital asset storage    │            │
│                         │ (file_format, DRM)       │            │
│ LLM Recommendation: KEEP both (complementary views)             │
│ LLM Rationale: "editions tracks publication variants, assets    │
│   tracks file delivery. Merging would conflate physical and     │
│   digital concerns. Link via edition_id FK instead."            │
├─────────────────────────┼──────────────────────────┼────────────┤
│ inventory.book_stock    │ Physical warehouse qty   │            │
│ vs                      │ (warehouse_id, qty)      │ 41%        │
│ storefront.listing_items│ Marketplace listing      │            │
│                         │ (seller_price, listing)  │            │
│ LLM Recommendation: KEEP book_stock, DEPRECATE listing_items    │
│ LLM Rationale: "listing_items mixes inventory and pricing       │
│   concerns. Migrate pricing to a canonical pricing table,       │
│   retire listing_items."                                        │
└─────────────────────────┴──────────────────────────┴────────────┘
```

**Phase 2: LLM-Generated Ontology Proposal**

The LLM synthesizes all prior analysis (clusters, code references, differentiation reports) into a comprehensive ontology proposal with merged schemas, table role assignments, and consistency rules:

```python
# LLM generates the full ontology proposal from accumulated context
from langchain.chat_models import ChatOpenAI
from langchain.chains import LLMChain
from langchain.prompts import ChatPromptTemplate

llm = ChatOpenAI(model="gpt-4o", temperature=0)

ontology_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are an enterprise data architect. Given a set of semantically similar "
               "tables with their schemas, code references, differentiation analysis, and "
               "lineage, propose a canonical ontology. Include: canonical entity schema, "
               "table role assignments (keep/deprecate/merge), and consistency rules."),
    ("human", "Cluster: {cluster_name}\n"
              "Tables: {tables_with_schemas}\n"
              "Differentiation report: {diff_report}\n"
              "Code analysis: {code_analysis}\n"
              "Downstream consumers: {consumers}")
])
ontology_chain = LLMChain(llm=llm, prompt=ontology_prompt)
```

```
LLM-Proposed Canonical Entity: catalog.product_master

Fields (merged schema — LLM-designed):
  - product_id          (surrogate key, new — LLM: "resolves ISBN optionality")
  - isbn                (nullable — digital-only titles lack ISBN)
  - title               (normalized)
  - format              (enum: print | ebook | audiobook)
  - source_system       ("imazon" | "ebooknow")
  - legacy_isbn         (maps to catalog.title_master.isbn)
  - legacy_product_id   (maps to products.digital_catalog.product_id)
  - list_price          (normalized to USD)
  - publication_date

LLM-Proposed Table Roles:
  catalog.product_master        → NEW canonical SSOT
  catalog.title_master          → Print view (keep, alias to canonical)
  catalog.editions              → Edition detail view (keep)
  products.digital_catalog      → Deprecated → migrate to canonical
  content.ebook_assets          → Digital asset view (keep)
  inventory.book_stock          → Inventory view (keep)
  storefront.listing_items      → Deprecated → migrate to canonical

LLM-Generated Consistency Rules:
  R1. New pipelines MUST join on catalog.product_master
  R2. title normalized: TRIM + title-case
  R3. product_id immutable once assigned
  R4. source_system tag required on all book-originating events
  R5. creator→author_id mapping must go through normalization pipeline (LLM-added)

Impact: 18 pipelines need update | Effort: Medium (schema additive)
LLM Migration Plan: Step-by-step SQL migration scripts auto-generated for each
  deprecated table, with rollback procedures and data validation checks.
```

**Phase 3: LLM-Powered Weekly Consistency Check** — DataSpoke uses the LLM to scan new and modified pipelines for ontology rule violations. The LLM analyzes SQL join patterns, column references, and data flow to detect semantic violations that regex-based rules would miss. Example: a new pipeline joins on `products.digital_catalog` instead of `catalog.product_master`, excluding 60% of print-only titles from recommendations. LLM-generated auto-correction proposed with 92% confidence, including the exact SQL changes needed.

#### DataHub Integration Points

Doc Generation is a **read + write** consumer. It reads schemas and properties for clustering analysis, then writes deprecation markers and tags back to DataHub:

| Analysis Step | DataHub Aspect | REST API Path | What It Returns / Stores |
|--------------|---------------|---------------|--------------------------|
| Schema similarity | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | Column names, types — input to embedding similarity |
| Description analysis | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | Descriptions for semantic matching |
| Shared consumers | `upstreamLineage` | GraphQL: `searchAcrossLineage` | Downstream overlap across candidate tables |
| Mark deprecated | `deprecation` | `POST /openapi/v3/entity/dataset` | `deprecated=true`, `note`, `replacement` URN |
| Tag source system | `globalTags` | `POST /openapi/v3/entity/dataset` | `urn:li:tag:source_imazon`, `urn:li:tag:source_ebooknow` |
| Source code refs | `datasetProperties.customProperties` | `POST /openapi/v3/entity/dataset` | `code_references`, `auto_generated_descriptions` from source analysis |

```python
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    DeprecationClass,
    SchemaMetadataClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))
emitter = DatahubRestEmitter(gms_server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN)

# Read schema — column names and types for embedding-based similarity
# REST: GET /aspects/{urn}?aspect=schemaMetadata
imazon_urn = make_dataset_urn(platform="oracle", name="catalog.title_master", env="PROD")
schema = graph.get_aspect(imazon_urn, SchemaMetadataClass)

# Mark deprecated — old eBookNow table replaced by canonical entity
# REST: POST /openapi/v3/entity/dataset  (aspect: deprecation)
ebooknow_urn = make_dataset_urn(platform="oracle", name="products.digital_catalog", env="PROD")
emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=ebooknow_urn,
    aspect=DeprecationClass(
        deprecated=True,
        note="Migrated to catalog.product_master per ontology reconciliation",
        replacement=make_dataset_urn(platform="oracle", name="catalog.product_master", env="PROD"),
    ),
))
```

> **Key point**: DataHub persists schema metadata and deprecation markers. DataSpoke adds embedding-based clustering, ontology proposal logic, and consistency rule enforcement.

#### DataSpoke Custom Implementation

| Component | Responsibility | Why DataHub Can't Do This |
|-----------|---------------|--------------------------|
| **LLM-Powered Semantic Clustering** | Send schema + descriptions + sample values to LLM API for deep conceptual analysis; classify datasets into business concept categories; pairwise semantic comparison beyond surface-level embedding similarity. Uses LangChain `LLMChain` for structured prompt orchestration. | DataHub has keyword search only, no semantic reasoning |
| **LLM Ontology Proposal Engine** | LLM synthesizes cluster analysis, code references, and differentiation reports to propose canonical entities with merged schemas, table roles, and consistency rules. Uses LangChain `ChatPromptTemplate` for multi-step reasoning. | DataHub stores metadata but has no schema merging or reasoning logic |
| **LLM Consistency Rule Engine** | LLM analyzes SQL patterns in new/modified pipelines against ontology rules (R1–R5); detects semantic violations that regex-based rules miss; generates auto-correction SQL | DataHub has no rule definition, violation scanning, or code analysis |
| **LLM Source Code Analyzer** | Scan linked repositories; send code snippets + schema context to LLM for business-level column description generation. Uses LangChain `RecursiveCharacterTextSplitter` for large codebases. | DataHub has no source code scanning or interpretation capability |
| **LLM Differentiation Report Generator** | LLM compares table pairs holistically (schema, lineage, code usage, sample data) and generates structured merge/keep/deprecate recommendations with rationale | DataHub stores individual schemas but cannot compare or reason about actions |
| **Ontology/Taxonomy Builder** *(shared with UC8)* | Reusable LLM-based service that builds and maintains business concept taxonomies from metadata. Provides concept categories, hierarchical relationships, and dataset-to-concept mappings consumed by both Doc Generation (UC4) and Multi-Perspective Overview (UC8). See cross-cutting note below. | DataHub has no taxonomy construction or LLM integration |

#### Outcome

| Metric | Manual Reconciliation | DataSpoke Doc Generation |
|--------|----------------------|---------------------------|
| Time to proposal | ~3 months (manual audit) | Hours (automated clustering) |
| Catalog AI-readiness | 58% | 91% |
| Ongoing violations/month | Untracked | 2–3 (auto-detected) |

---

## Data Analysis (DA) Group

### Use Case 5: NL Search — GDPR Compliance Audit

**Feature**: Natural Language Search

#### Scenario: Legal Team Searches for European Customer PII Used by Marketing

**Background:**
Imazon's Legal team is preparing for a GDPR audit and asks: "Find all tables containing European customer PII that are accessed by marketing analytics pipelines." This requires cross-referencing PII classification, geographic scope, and lineage — a multi-dimensional query that keyword search cannot handle.

#### Without DataSpoke

Manual process: search DataHub for "customer" tables, check each description for "EU"/"Europe", grep column names for `email`/`name`/`address`, manually trace lineage to marketing consumers, review 50+ tables. **Time: 4–6 hours. Accuracy: ~70%** (depends on documentation quality).

#### With DataSpoke

**Query:**

```
Natural Language Input (via /api/v1/spoke/common/search?q=...):
"Find tables with European customer PII used by marketing analytics"
```

**Response (2.3 seconds):**

```
Found 7 Tables Matching Criteria

HIGH PRIORITY (Direct PII + Marketing Usage)

1. customers.eu_profiles (Relevance: 98%)
   PII Fields: email, full_name, shipping_address, date_of_birth
   Geographic Tag: EU/GDPR
   Marketing Lineage:
     └─ marketing.eu_email_campaigns (active)
        └─ dashboards.eu_campaign_performance
   Compliance: ✓ Retention: 2yr | ✓ Encryption: at-rest + transit
              ⚠ Right-to-deletion: manual process

2. orders.eu_purchase_history (Relevance: 94%)
   PII Fields: customer_id (linkable), shipping_address, payment_last4
   Marketing Lineage:
     └─ marketing.eu_buyer_segmentation
        └─ recommendations.eu_personalized_picks
   Compliance: ✓ Anonymization after 90 days | ✓ GDPR retention

MEDIUM PRIORITY (Derived / Pseudonymized)

3. marketing.eu_reader_segments (Relevance: 87%)
   PII Fields: hashed_email, aggregate_reading_score
   Upstream PII: customers.eu_profiles (1 hop)
   Active campaigns: 8 | Dashboard deps: 4

[... 4 more tables ...]

Summary:
  Direct PII tables: 2 | Derived PII tables: 5
  Active marketing pipelines: 11
  GDPR compliance gaps: 1 (right-to-deletion automation)
```

**Follow-up:** "Which tables lack automated right-to-deletion?" → DataSpoke identifies `customers.eu_profiles` (requires manual SQL) and `reviews.eu_book_reviews_archive` (cold storage, 48hr restore). Recommends automated deletion jobs.

#### DataHub Integration Points

NL Search is a **read** consumer. It queries multiple DataHub aspects to build the vector search index and enrich query results:

| Search Step | DataHub Aspect | REST API Path | What It Returns |
|------------|---------------|---------------|----------------|
| Embedding source | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | Descriptions — vectorized into Qdrant |
| Column-level PII detection | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | Column names (`email`, `full_name`, etc.) |
| PII / GDPR tags | `globalTags` | `GET /aspects/{urn}?aspect=globalTags` | `urn:li:tag:PII`, `urn:li:tag:GDPR` |
| Marketing lineage | `upstreamLineage` | GraphQL: `searchAcrossLineage` | Downstream consumers in marketing domain |
| Ownership | `ownership` | `GET /aspects/{urn}?aspect=ownership` | Data steward for compliance contact |
| Usage frequency | `datasetUsageStatistics` (timeseries) | `POST /aspects?action=getTimeseriesAspectValues` | `uniqueUserCount`, `totalSqlQueries` |

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetUsageStatisticsClass,
    GlobalTagsClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))
dataset_urn = make_dataset_urn(platform="oracle", name="customers.eu_profiles", env="PROD")

# PII tags — check for GDPR-relevant classification tags
# REST: GET /aspects/{urn}?aspect=globalTags
tags = graph.get_aspect(dataset_urn, GlobalTagsClass)

# Downstream lineage — find marketing consumers via GraphQL
# GraphQL: searchAcrossLineage(input: {urn, direction: DOWNSTREAM, types: [DATASET]})
downstream = graph.execute_graphql("""
    query {
        searchAcrossLineage(input: {
            urn: "%s",
            direction: DOWNSTREAM,
            types: [DATASET],
            query: "*marketing*"
        }) { searchResults { entity { urn } } }
    }
""" % dataset_urn)

# Usage statistics — prioritize high-traffic tables in search results
# REST: POST /aspects?action=getTimeseriesAspectValues
usage = graph.get_timeseries_values(
    dataset_urn, DatasetUsageStatisticsClass, filter={}, limit=30,
)
```

> **Key point**: DataHub provides keyword search and raw metadata. DataSpoke adds natural language parsing, vector similarity, PII classification logic, and conversational refinement.

#### DataSpoke Custom Implementation

| Component | Responsibility | Why DataHub Can't Do This |
|-----------|---------------|--------------------------|
| **NL Query Parser** | Parse natural language into structured intent (entity type, filters, compliance context) | DataHub search is keyword-based, no intent parsing |
| **Vector Search (Qdrant)** | Hybrid search: vector similarity + graph traversal for multi-dimensional queries | DataHub has Elasticsearch keyword search only |
| **PII Classification Engine** | Detect PII fields by column name patterns + tag presence, classify into tiers | DataHub stores tags but has no classification logic |
| **Compliance Report Generator** | Auto-generate GDPR audit reports with lineage diagrams and gap analysis | DataHub provides raw metadata, no report generation |
| **Conversational Refinement** | Support follow-up queries in context ("Which tables lack...") | DataHub search is stateless, no conversation support |

#### Outcome

| Metric | Traditional Search | DataSpoke NL Search |
|--------|-------------------|---------------------|
| Time | 4–6 hours | 2–5 minutes |
| Accuracy | ~70% | ~98% |
| Follow-up queries | Start over | Conversational refinement |
| Audit report | Manual creation | Auto-generated |

---

### Use Case 7: Text-to-SQL Metadata — AI-Assisted Genre Analysis

**Feature**: Text-to-SQL Optimized Metadata

#### Scenario: Business Analyst Asks AI for Best-Selling Genres

**Background:**
A business analyst at Imazon asks an AI assistant: "What were Imazon's top 10 best-selling genres in Q4?" The AI needs to generate SQL, but DataHub's standard metadata only provides column names and types — no value profiles, no business glossary mappings, no join path hints. Without enriched context, the AI produces incorrect SQL that returns wrong results.

#### Without DataSpoke

The AI generates SQL using only column names and types from DataHub:

```sql
-- AI-generated SQL (WRONG)
SELECT genre, COUNT(*) as sales
FROM orders.purchase_history
JOIN catalog.title_master ON orders.purchase_history.isbn = catalog.title_master.isbn
WHERE purchase_date >= '2024-10-01'
  AND genre = 'Fiction'  -- Wrong: actual values are codes like 'FIC-001'
GROUP BY genre
ORDER BY sales DESC
LIMIT 10;

-- Problems:
-- 1. Uses "genre" column (display name) instead of "genre_code" (actual column)
-- 2. Joins on isbn directly — misses edition_id intermediate join
-- 3. Filters WHERE genre = 'Fiction' — actual values are codes (FIC-001, NF-002)
-- 4. Misses genre_hierarchy table needed for human-readable names
```

Result: 0 rows returned. Analyst spends 15–30 minutes debugging, asks a data engineer, iterates manually.

#### With DataSpoke

**Step 1: Metadata Context Retrieval**

```python
# GET /api/v1/spoke/common/search?q=...&sql_context=true
dataspoke.text_to_sql.get_context({
  "question": "What were Imazon's top 10 best-selling genres in Q4?",
  "candidate_tables": ["orders.purchase_history", "catalog.title_master", "catalog.genre_hierarchy"]
})

# Response:
{
  "column_profiles": {
    "catalog.title_master.genre_code": {
      "sample_values": ["FIC-001", "NF-002", "SCI-003", "MYS-004", "ROM-005"],
      "cardinality": 48,
      "distribution": "top 5 cover 62% of titles",
      "business_glossary": "Genre classification code — maps to genre_hierarchy.code for display name"
    },
    "catalog.genre_hierarchy.display_name": {
      "sample_values": ["Fiction", "Non-Fiction", "Science Fiction", "Mystery", "Romance"],
      "note": "Human-readable genre name — use this for output display"
    }
  },
  "join_paths": {
    "recommended": {
      "path": "orders.purchase_history → orders.order_items → catalog.editions → catalog.title_master → catalog.genre_hierarchy",
      "join_keys": [
        "purchase_history.order_id = order_items.order_id",
        "order_items.edition_id = editions.edition_id",
        "editions.isbn = title_master.isbn",
        "title_master.genre_code = genre_hierarchy.code"
      ],
      "confidence": 0.95,
      "source": "lineage + historical query analysis"
    }
  },
  "sample_queries": [
    {
      "description": "Monthly genre sales report (from topSqlQueries)",
      "sql": "SELECT gh.display_name, COUNT(*) FROM orders.order_items oi JOIN catalog.editions e ON oi.edition_id = e.edition_id JOIN catalog.title_master tm ON e.isbn = tm.isbn JOIN catalog.genre_hierarchy gh ON tm.genre_code = gh.code GROUP BY gh.display_name"
    }
  ],
  "date_conventions": {
    "orders.purchase_history.purchase_date": "DATE type, Q4 = WHERE purchase_date BETWEEN '2024-10-01' AND '2024-12-31'"
  }
}
```

**Step 2: AI Generates Correct SQL**

With DataSpoke's enriched context, the AI produces a working query:

```sql
-- AI-generated SQL (CORRECT — with DataSpoke context)
SELECT gh.display_name AS genre,
       COUNT(DISTINCT oi.order_item_id) AS total_sales
FROM orders.purchase_history ph
JOIN orders.order_items oi ON ph.order_id = oi.order_id
JOIN catalog.editions e ON oi.edition_id = e.edition_id
JOIN catalog.title_master tm ON e.isbn = tm.isbn
JOIN catalog.genre_hierarchy gh ON tm.genre_code = gh.code
WHERE ph.purchase_date BETWEEN '2024-10-01' AND '2024-12-31'
GROUP BY gh.display_name
ORDER BY total_sales DESC
LIMIT 10;

-- Result:
-- Fiction          42,381
-- Mystery          38,912
-- Romance          31,205
-- Science Fiction  28,744
-- Non-Fiction      25,891
-- ...
```

**Step 3: Feedback Loop**

The successful query is recorded. DataSpoke reinforces the business glossary mapping (`genre_code` → `genre_hierarchy.display_name`) and the 4-hop join path for future queries about genre-related topics.

#### DataHub Integration Points

Text-to-SQL context is a **read**-heavy consumer. It queries metadata aspects to build enriched context for AI SQL generation:

| Context Step | DataHub Aspect | REST API Path | What It Returns |
|-------------|---------------|---------------|----------------|
| Column names/types | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | Column definitions — base schema for SQL generation |
| Table descriptions | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | Business descriptions for table identification |
| Historical queries | `datasetUsageStatistics` (timeseries) | `POST /aspects?action=getTimeseriesAspectValues` | `topSqlQueries` — sample queries for pattern extraction |
| Join path inference | `upstreamLineage` | `GET /aspects/{urn}?aspect=upstreamLineage` | Lineage edges — basis for multi-hop join recommendations |

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetUsageStatisticsClass,
    SchemaMetadataClass,
    UpstreamLineageClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))
dataset_urn = make_dataset_urn(platform="oracle", name="catalog.title_master", env="PROD")

# Schema — column names and types for SQL generation
# REST: GET /aspects/{urn}?aspect=schemaMetadata
schema = graph.get_aspect(dataset_urn, SchemaMetadataClass)

# Usage statistics — topSqlQueries for sample query patterns
# REST: POST /aspects?action=getTimeseriesAspectValues
usage = graph.get_timeseries_values(
    dataset_urn, DatasetUsageStatisticsClass, filter={}, limit=30,
)

# Upstream lineage — join path inference from lineage edges
# REST: GET /aspects/{urn}?aspect=upstreamLineage
lineage = graph.get_aspect(dataset_urn, UpstreamLineageClass)
```

> **Key point**: DataHub stores schema, usage statistics, and lineage. DataSpoke adds column value profiling, business glossary mapping, join path recommendation, and LLM context optimization.

#### DataSpoke Custom Implementation

| Component | Responsibility | Why DataHub Can't Do This |
|-----------|---------------|--------------------------|
| **Column Value Profiler** | Sample values, cardinality, distribution analysis beyond DataHub's rowCount/nullCount | DataHub profiles store aggregate statistics, not value-level distributions |
| **Business Glossary Mapper** | Map technical columns to business terms with value translations (e.g., `FIC-001` → `Fiction`) | DataHub stores glossary terms but has no automated column-to-term mapping |
| **Join Path Recommender** | Combine lineage + usage patterns to recommend optimal multi-hop join paths | DataHub stores lineage edges but has no join path computation |
| **SQL Template Generator** | Generate SQL scaffolds from metadata + historical queries | DataHub provides raw metadata, no SQL generation capability |
| **Context Window Optimizer** | Select most relevant metadata to fit LLM token limits for accurate SQL generation | DataHub has no awareness of LLM context constraints |

#### Outcome

| Metric | Without DataSpoke | With DataSpoke |
|--------|------------------|----------------|
| SQL first-attempt accuracy | ~30% | ~90% |
| Time to working query | 15–30 min (manual iteration) | 1–2 min (AI-assisted) |
| Join path correctness | Frequent errors (wrong keys) | 95% correct (lineage-informed) |
| Business term resolution | Manual lookup | Automatic glossary mapping |

---

## Data Governance (DG) Group

### Use Case 6: Metrics Dashboard — Enterprise Metadata Health

**Feature**: Enterprise Metrics Time-Series Monitoring

#### Scenario: CDO Launches Metadata Health Initiative Across 6 Departments

**Background:**
Imazon's Chief Data Officer launches a company-wide initiative to improve data documentation and ownership accountability. Six departments manage 400+ datasets, but documentation coverage and ownership assignment vary wildly. The governance team currently runs quarterly manual audits that take 2 weeks each and produce point-in-time spreadsheets that go stale immediately.

#### Without DataSpoke

Manual audit cycle: governance team reviews tables, creates tracking spreadsheet, emails department leads, follows up in 2 weeks, repeats quarterly. **Problems:** labor-intensive (2 weeks/audit), point-in-time snapshots, no automated tracking, hard to measure improvement.

#### With DataSpoke

**Week 1 — Initial Assessment:**

```
DataSpoke Metrics Dashboard:
Enterprise Metadata Health Score: 59/100

Department Breakdown:
┌─────────────────────┬────────┬──────────┬────────┬─────────┐
│ Department          │ Score  │ Datasets │ Issues │ Trend   │
├─────────────────────┼────────┼──────────┼────────┼─────────┤
│ Engineering         │ 76/100 │ 95       │ 23     │ ↑ +3%   │
│ Data Science        │ 69/100 │ 72       │ 22     │ → 0%    │
│ Marketing           │ 54/100 │ 80       │ 37     │ ↓ -2%   │
│ Finance             │ 81/100 │ 38       │ 7      │ ↑ +5%   │
│ Operations          │ 45/100 │ 65       │ 36     │ → 0%    │
│ Publisher Relations  │ 40/100 │ 55       │ 33     │ ↓ -1%   │
└─────────────────────┴────────┴──────────┴────────┴─────────┘

Critical Issues: 42 | High: 78 | Medium: 118
```

**Detailed view — Marketing:**

```
Marketing Department — Score: 54/100 (Target: 70)

Critical (12 datasets):
  - Missing owners for high-usage tables
  - No description on marketing.campaign_metrics_daily (38 downstream users!)

Auto-generated Action Items → marketing-data-lead@imazon.com:
  Priority 1 (Due: 1 week):
  [ ] Assign owner to marketing.campaign_metrics_daily
  [ ] Add descriptions to top 5 high-usage undocumented tables
  Priority 2 (Due: 2 weeks):
  [ ] Add PII classification to customer-facing tables
  [ ] Document update frequencies for all metrics tables
```

**Week 2 — Automated Notifications** — DataSpoke emails dataset owners with specific action items, estimated fix time (~5–10 min each), and projected score impact.

**Month 1 — Progress:**

```
Enterprise Health Score: 59 → 70 (+11 points)

Most Improved: Marketing 54 → 71 (+17) — Department of the Month
  Resolved 35/37 critical issues | Avg response: 3 days (from 12)

Needs Attention: Publisher Relations 40 → 44 (+4) — below target pace
  Recommendation: Schedule 1:1 with team lead

Metrics:
  Documentation coverage: 64% → 78% (+14%)
  Owner assignment rate: 79% → 93% (+14%)
  Avg issue resolution: 4.1 days (target: 5) ✓
```

**Month 3 — Milestone:** Enterprise score reaches 77/100 (target: 70). All departments above minimum threshold. Documentation decay rate tracked at -2.1%/month (new tables created faster than documented). DataSpoke recommends mandatory documentation checklist for new table creation.

#### DataHub Integration Points

> **Pure aggregation principle**: The Metrics Dashboard does not observe data sources directly — it aggregates metadata that already exists in DataHub aspects and DataSpoke validation results.

The Metrics Dashboard is a **read** consumer. It queries across all datasets to compute aggregate health scores:

| Health Metric | DataHub Aspect | REST API Path | What It Returns |
|--------------|---------------|---------------|----------------|
| Description coverage | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | Presence/absence of `description` field |
| Owner assignment | `ownership` | `GET /aspects/{urn}?aspect=ownership` | Owner URN list — empty = unassigned |
| Column documentation | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | Per-column `description` — empty = undocumented |
| Tag coverage | `globalTags` | `GET /aspects/{urn}?aspect=globalTags` | PII classification tags present or missing |
| Usage popularity | `datasetUsageStatistics` (timeseries) | `POST /aspects?action=getTimeseriesAspectValues` | `uniqueUserCount` — prioritize high-usage gaps |
| Entity enumeration | — | GraphQL: `scrollAcrossEntities` | List all datasets per domain/department |

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    OwnershipClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))

# Enumerate all datasets — iterate for health scoring
# GraphQL: scrollAcrossEntities or REST filter
dataset_urns = list(graph.get_urns_by_filter(entity_types=["dataset"]))

for dataset_urn in dataset_urns:
    # Ownership — check if owner is assigned
    # REST: GET /aspects/{urn}?aspect=ownership
    ownership = graph.get_aspect(dataset_urn, OwnershipClass)

    # Description — check documentation coverage
    # REST: GET /aspects/{urn}?aspect=datasetProperties
    properties = graph.get_aspect(dataset_urn, DatasetPropertiesClass)
```

> **Key point**: DataHub stores per-dataset metadata aspects. DataSpoke aggregates them into cross-dataset health scores, department rankings, and trend analysis.

#### DataSpoke Custom Implementation

| Component | Responsibility | Why DataHub Can't Do This |
|-----------|---------------|--------------------------|
| **Health Score Aggregator** | Aggregate pre-existing metadata (descriptions, ownership) and validation results into 0–100 scores | DataHub has no cross-aspect scoring system |
| **Department Mapper** | Map datasets to departments via ownership → HR API lookup | DataHub stores ownership URNs but has no org-structure awareness |
| **Trend Analysis** | Track health scores over time, compute decay rates, forecast improvement | DataHub stores point-in-time aspects, no time-series aggregation of metadata quality |

#### Outcome

| Metric | Quarterly Manual Audit | DataSpoke Metrics Dashboard |
|--------|----------------------|----------------------------|
| Audit cycle | 2 weeks, quarterly | Real-time, continuous |
| Issue response time | 12 days avg | 3 days avg |
| Health score improvement | Unmeasured | 59 → 77 in 3 months |
| Governance team effort | 100% manual | 80% reduction |

---

### Use Case 8: Multi-Perspective Overview — Enterprise Data Visualization

**Feature**: Multi-Perspective Data Overview

#### Scenario: CDO Wants to Visualize the Entire Data Estate

**Background:**
After UC6's metadata health initiative raised the enterprise score from 59 to 77, Imazon's CDO asks the next question: "Show me our entire data landscape — not as a spreadsheet, but as something I can explore." With 700+ datasets across 8 domains, tabular health scores aren't enough. The governance team needs visual exploration to spot patterns, blind spots, and structural issues invisible in flat tables.

#### Without DataSpoke

Manual process: a governance analyst spends 3 days building a Lucidchart diagram. Nodes are hand-placed, colors hand-assigned based on tribal knowledge about data quality. Medallion layer classification (Bronze/Silver/Gold) is done by asking engineers team by team. The diagram is immediately stale — new datasets added next week won't appear. **Time: 3–5 days. Coverage: ~60% of datasets (the rest are "unknown").**

#### With DataSpoke

**View 1: Taxonomy/Ontology Graph**

Before rendering the graph, DataSpoke uses the shared **Ontology/Taxonomy Builder** (the same LLM-based service used in UC4) to map every dataset to ontology categories. The LLM analyzes each table's schema, descriptions, column names, sample values, and lineage context to assign business concept categories — producing the semantic groupings that drive the graph layout.

```python
# LLM-based ontology category mapping (via shared Ontology/Taxonomy Builder)
from langchain.chat_models import ChatOpenAI
from langchain.chains import LLMChain
from langchain.prompts import ChatPromptTemplate

llm = ChatOpenAI(model="gpt-4o", temperature=0)

# Step 1: LLM maps each dataset to ontology categories using table metadata
taxonomy_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are an enterprise data taxonomist. Given a dataset's schema metadata "
               "(table name, columns, types, description, tags, lineage), assign it to one "
               "or more business ontology categories. Return a structured classification with "
               "primary category, secondary categories, and confidence score.\n"
               "Standard categories: Product/Catalog, Customer, Order/Transaction, "
               "Shipping/Logistics, Marketing, Finance, Review/Rating, Recommendation, "
               "Inventory, Publishing, Analytics/Report, Infrastructure."),
    ("human", "Table: {table_name}\nColumns: {columns}\nDescription: {description}\n"
              "Tags: {tags}\nUpstream: {upstream}\nDownstream: {downstream}")
])
taxonomy_chain = LLMChain(llm=llm, prompt=taxonomy_prompt)

# Step 2: LLM refines cross-category relationships for graph edges
relationship_prompt = ChatPromptTemplate.from_messages([
    ("system", "Given two ontology categories and the datasets within each, determine "
               "the semantic relationship type: dependency, derivation, complementary, "
               "or overlap. This drives edge rendering in the taxonomy graph."),
    ("human", "Category A: {cat_a} ({datasets_a})\nCategory B: {cat_b} ({datasets_b})\n"
              "Shared lineage edges: {shared_edges}")
])
relationship_chain = LLMChain(llm=llm, prompt=relationship_prompt)
```

```
DataSpoke Multi-Perspective Overview — Taxonomy Graph:

Pre-charting: LLM Ontology Category Mapping
  700 datasets analyzed via LLM API (batch-processed)
  Ontology categories assigned: 12 primary, 34 secondary
  Mapping confidence: 94% avg (datasets with descriptions)
                      78% avg (datasets without descriptions — LLM inferred from schema)

  Sample mappings:
    catalog.title_master        → Product/Catalog (0.98)
    orders.purchase_history     → Order/Transaction (0.97)
    recommendations.book_features → Recommendation (0.92), Product/Catalog (0.71)
    marketing.eu_email_campaigns → Marketing (0.96), Customer (0.68)

Nodes: 700 datasets (colored by ontology category + health score)
Edges: 1,842 (1,204 lineage + 638 LLM-inferred semantic relationships)
Auto-detected domains: 12 business clusters (LLM-assigned ontology categories)

Visualization:
  Node color: Health score (🔴 <50 | 🟡 50-70 | 🟢 >70)
  Node size:  Usage volume (larger = more consumers)
  Node group: LLM-assigned ontology category (force-directed clustering)
  Edge type:  Solid = lineage | Dashed = LLM-inferred semantic relationship

Key Discovery — Governance Blind Spot:
  Cluster: recommendations.* (12 datasets)
  LLM Category: "Recommendation" — high business criticality inferred
  Status: ALL RED (health scores 12–38)
  Issues:
    - Zero documented lineage (no upstream/downstream recorded)
    - High usage: 45 unique users/week across the cluster
    - No ownership assigned to 8 of 12 tables
    - LLM-inferred upstream (from schema + naming analysis): catalog.title_master,
      reviews.user_ratings, orders.purchase_history
  Risk: Critical business feature (book recommendations) running on
        completely undocumented, unowned data infrastructure

  Drill-down:
    recommendations.book_features       → Health: 38 | Users: 22 | Owner: NONE
    recommendations.collaborative_scores → Health: 25 | Users: 18 | Owner: NONE
    recommendations.content_embeddings   → Health: 12 | Users: 8  | Owner: NONE
    [... 9 more ...]

  Action: Escalated to Engineering VP — mandatory ownership + documentation sprint
```

**View 2: Medallion Architecture**

```
DataSpoke Multi-Perspective Overview — Medallion Classification:

Auto-classified (by upstream count + naming patterns + schema analysis):

┌──────────────┬────────┬──────────────────────────────────────────────┐
│ Layer        │ Count  │ Characteristics                              │
├──────────────┼────────┼──────────────────────────────────────────────┤
│ 🥉 Bronze    │ 180    │ Raw ingestion, external sources, _raw suffix │
│ 🥈 Silver    │ 120    │ Cleaned/joined, _cleaned/_enriched suffix    │
│ 🥇 Gold      │ 55     │ Business-ready aggregates, reports.* domain  │
│ ❓ Unclassified│ 345  │ Cannot infer layer from available metadata   │
└──────────────┴────────┴──────────────────────────────────────────────┘

Gap Analysis:
  Bronze → Silver conversion rate: 60% (108/180 have Silver counterparts)
  40% of Bronze tables (72) have NO Silver counterpart
    → Ingested but never refined — candidates for cleanup

  Top Cleanup Candidates (Bronze with no downstream, low usage):
    publishers.feed_raw_legacy      — Last accessed: 8 months ago | 0 downstream
    shipping.carrier_raw_v1         — Last accessed: 6 months ago | 0 downstream
    marketing.campaign_import_2022  — Last accessed: 11 months ago | 0 downstream
    [... 12 more identified ...]

  Storage Impact: ~2.3 TB recoverable from stale Bronze tables

  Unclassified Triage:
    345 datasets need manual review or enriched metadata for auto-classification
    DataSpoke recommendation: Run Deep Ingestion (UC1) on top 50 by usage volume
      → Estimated to auto-classify 180 of 345 (52%)
```

#### DataHub Integration Points

The Multi-Perspective Overview is a **read**-heavy consumer. It queries broadly across all datasets to construct graph and classification views:

| Visualization Step | DataHub Aspect | REST API Path | What It Returns |
|-------------------|---------------|---------------|----------------|
| Domain hints | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | Descriptions, `customProperties` — domain classification input |
| Lineage edges | `upstreamLineage` | `GET /aspects/{urn}?aspect=upstreamLineage` | Upstream dataset URNs — graph construction |
| Medallion / domain tags | `globalTags` | `GET /aspects/{urn}?aspect=globalTags` | `urn:li:tag:bronze`, `urn:li:tag:gold`, domain tags |
| Department grouping | `ownership` | `GET /aspects/{urn}?aspect=ownership` | Owner URN → department mapping for cluster coloring |
| Usage for node sizing | `datasetUsageStatistics` (timeseries) | `POST /aspects?action=getTimeseriesAspectValues` | `uniqueUserCount`, `totalSqlQueries` — node size input |
| Schema similarity | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | Column names/types — overlap computation for similarity edges |

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    GlobalTagsClass,
    OwnershipClass,
    SchemaMetadataClass,
    UpstreamLineageClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))

# Enumerate all datasets for graph construction
# GraphQL: scrollAcrossEntities or REST filter
dataset_urns = list(graph.get_urns_by_filter(entity_types=["dataset"]))

for dataset_urn in dataset_urns:
    # Lineage — edges for graph construction
    # REST: GET /aspects/{urn}?aspect=upstreamLineage
    lineage = graph.get_aspect(dataset_urn, UpstreamLineageClass)

    # Schema — column overlap for similarity edges
    # REST: GET /aspects/{urn}?aspect=schemaMetadata
    schema = graph.get_aspect(dataset_urn, SchemaMetadataClass)

    # Tags — medallion layer and domain classification
    # REST: GET /aspects/{urn}?aspect=globalTags
    tags = graph.get_aspect(dataset_urn, GlobalTagsClass)

    # Properties — descriptions for domain clustering
    # REST: GET /aspects/{urn}?aspect=datasetProperties
    properties = graph.get_aspect(dataset_urn, DatasetPropertiesClass)
```

> **Key point**: DataHub provides per-dataset metadata (lineage, schema, tags, usage). DataSpoke aggregates this into interactive graph visualizations, auto-classification, and blind spot detection.

#### DataSpoke Custom Implementation

| Component | Responsibility | Why DataHub Can't Do This |
|-----------|---------------|--------------------------|
| **Ontology/Taxonomy Builder** *(shared with UC4)* | LLM-based service that maps every dataset to business ontology categories before graph rendering. Analyzes schema, descriptions, tags, and lineage via LLM API to assign primary/secondary categories. Same service used in UC4 for semantic clustering. See cross-cutting note below. | DataHub has no LLM-powered taxonomy construction |
| **Graph Layout Engine** | Force-directed graph from lineage + LLM-inferred semantic relationship edges; nodes grouped by LLM-assigned ontology categories; interactive zoom/filter | DataHub has a basic lineage viewer, not a full-estate graph with semantic grouping |
| **LLM Domain Classifier** | LLM analyzes schema + descriptions + lineage to auto-classify datasets into business domains; replaces embedding-only approach with richer semantic reasoning via LangChain | DataHub supports manual domain assignment only |
| **Medallion Layer Detector** | Infer Bronze/Silver/Gold from upstream count (0 = bronze, 1–2 = silver, 3+ = gold via `upstreamLineage` aspect), naming patterns, and transformation complexity | DataHub stores tags but has no inference logic for medallion classification |
| **Health Colorizer** | Map composite health scores to visual indicators (color, size, opacity) on graph nodes | DataHub has no visual health overlay capability |
| **Blind Spot Analyzer** | Detect orphaned datasets, missing lineage, dead-end Bronze tables, unowned high-usage clusters | DataHub provides raw metadata but has no cross-dataset anomaly detection |

#### Outcome

| Metric | Manual Diagramming | DataSpoke Multi-Perspective |
|--------|-------------------|----------------------------|
| Time to full estate view | 3–5 days | Minutes (auto-generated) |
| Dataset coverage | ~60% (known tables) | 100% (all registered datasets) |
| Blind spot detection | Ad-hoc, tribal knowledge | Systematic, automated |
| Medallion classification | Manual, per-team survey | Auto-inferred, continuously updated |
| Staleness | Immediately stale | Real-time, auto-refreshed |

---

## Cross-Cutting: Shared Ontology/Taxonomy Builder

UC4 (Doc Generation) and UC8 (Multi-Perspective Overview) both require mapping datasets to business concept categories — UC4 for semantic clustering and ontology reconciliation, UC8 for graph node grouping and domain classification. Rather than duplicating this logic, DataSpoke provides a **shared Ontology/Taxonomy Builder** as a reusable backend service.

**Architecture:**

```
┌─────────────────────────────────────────────────────────────┐
│              Ontology/Taxonomy Builder Service              │
│                                                             │
│  Input: DataHub metadata (schema, descriptions, tags,       │
│         lineage, sample values) for all datasets            │
│                                                             │
│  Processing (LLM-powered via LangChain):                    │
│    1. Dataset → Concept Classification (LLM per dataset)    │
│    2. Concept Hierarchy Construction (LLM synthesis)        │
│    3. Cross-Concept Relationship Inference (LLM pairwise)   │
│    4. Confidence Scoring & Human Review Queue               │
│                                                             │
│  Output: Persistent ontology graph stored in PostgreSQL     │
│    - concept_categories (id, name, parent_id, description)  │
│    - dataset_concept_map (dataset_urn, concept_id, score)   │
│    - concept_relationships (concept_a, concept_b, type)     │
│                                                             │
│  Consumers:                                                 │
│    UC4: Semantic clustering, ontology reconciliation        │
│    UC8: Graph node grouping, domain classification          │
│    Future: NL Search (UC5) concept-aware query expansion    │
└─────────────────────────────────────────────────────────────┘
```

**Key Design Points:**
- **LLM API is central**: Every classification, hierarchy inference, and relationship detection step uses the external LLM API via LangChain. The LLM reasons over schema structure, naming conventions, descriptions, and lineage — not just embedding similarity.
- **Incremental updates**: When new datasets are ingested (UC1) or schemas change, the builder re-classifies only affected datasets and propagates changes to downstream consumers (UC4, UC8).
- **Human-in-the-loop**: Low-confidence classifications (< 0.7) are queued for governance team review. LLM provides rationale to speed up human decisions.
- **Versioned taxonomy**: Each taxonomy build is versioned. UC4's ontology proposals reference a specific taxonomy version, ensuring reproducibility.

---

## Summary: Value Delivered

| Use Case | User Group | Feature | Traditional Approach | With DataSpoke | Improvement |
|----------|-----------|---------|---------------------|----------------|-------------|
| **Legacy Book Catalog Enrichment** | DE | Deep Ingestion | Manual metadata entry, no lineage | Automated multi-source enrichment | 89% enrichment, 210 lineage edges |
| **Recommendation Pipeline Verification** | DE / DA | Online Validator | ~30% failure rate from bad data | <5% failure with pre-verification | 83% reduction in incidents |
| **Fulfillment SLA Early Warning** | DE | Online Validator | Reactive alerts after breach | Predictive warnings 2+ hours early | Zero SLA breaches |
| **Post-Acquisition Ontology** | DE | Doc Generation | 3-month manual reconciliation | Automated proposal in hours | Orders-of-magnitude faster |
| **GDPR Compliance Audit** | DA | NL Search | 4–6 hours manual search | 2–5 minutes automated | 98% time savings |
| **Enterprise Metadata Health** | DG | Metrics Dashboard | Quarterly manual audits | Real-time continuous monitoring | 80% efficiency gain |
| **AI-Assisted Genre Analysis** | DA | Text-to-SQL Metadata | Manual SQL, wrong joins/values | 90% first-attempt accuracy | ~60% SQL accuracy gain |
| **Enterprise Data Visualization** | DG | Multi-Perspective Overview | Manual diagramming, days | Real-time auto-generated graphs | Systematic blind spot detection |

**Cross-cutting Benefits:**
- **AI-Ready:** Enables autonomous agents to work safely with Imazon's production data
- **Real-time Intelligence:** Shifts from reactive to proactive data management
- **Context-Aware:** Understands data relationships and business meaning across all departments
- **Measurable Impact:** Quantifiable improvements in quality, compliance, and efficiency
- **Ontology Health:** Catalog remains semantically consistent through acquisitions and organic growth
- **LLM-Native:** Extensive use of external LLM APIs (via LangChain) for semantic analysis, ontology construction, code interpretation, and consistency enforcement across UC4 and UC8
