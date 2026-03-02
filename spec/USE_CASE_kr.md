# DataSpoke: 상세 유스케이스 시나리오

> **문서 목적 안내**
> 이 문서는 아이디어 정립과 비전 정렬을 위한 개념적 시나리오를 제시한다. 각 유스케이스는 DataSpoke의 의도된 기능과 가치를 보여주지만, 구현 사양이나 기술 요구사항은 아니다. 실제 구현 세부사항, 기술 아키텍처, 기능 우선순위는 별도의 기술 사양 문서에서 정의한다.

이 문서는 DataSpoke가 세 가지 사용자 그룹 — **데이터 엔지니어링(DE)**, **데이터 분석(DA)**, **데이터 거버넌스(DG)** — 에 걸쳐 DataHub 기능을 어떻게 강화하는지 실제적 시나리오로 설명한다.

모든 시나리오는 가상의 온라인 서점 **Imazon**이라는 단일 회사 컨텍스트를 공유하며, 유스케이스들이 공존하고 상호 보완된다.

---

## 가상 회사 프로필: Imazon

Imazon은 설립 15년차 온라인 서점이다. 데이터 환경은 오랜 유기적 성장을 반영한다:

- **레거시 Oracle 데이터 웨어하우스** — 도서 카탈로그, 고객, 주문, 리뷰, 출판사, 재고, 배송을 다루는 500개 이상의 테이블
- **부서** — 엔지니어링, 데이터 사이언스, 마케팅, 재무, 법무, 운영, 출판사 관계, 고객 지원
- **주요 데이터 도메인** — `catalog.*` (도서, 저자, 장르), `customers.*`, `orders.*`, `reviews.*`, `recommendations.*`, `publishers.*`, `inventory.*`, `shipping.*`
- **DataHub 도입** — 최근 배포 완료. 표준 Oracle 커넥터가 스키마 메타데이터를 가져왔지만, 비즈니스 컨텍스트, 스토어드 프로시저 리니지, Confluence와 스프레드시트에 잠긴 조직 내 암묵지는 누락됨

---

## 기능 매핑

| 유스케이스 | 사용자 그룹 | 기능 |
|-----------|-----------|------|
| [유스케이스 1: Deep Ingestion — 레거시 도서 카탈로그 보강](#유스케이스-1-deep-ingestion--레거시-도서-카탈로그-보강) | DE | 심층 기술 사양 인제스천 |
| [유스케이스 2: Online Validator — 추천 파이프라인 검증](#유스케이스-2-online-validator--추천-파이프라인-검증) | DE / DA | 온라인 데이터 검증기 |
| [유스케이스 3: Predictive SLA — 배송 파이프라인 조기 경보](#유스케이스-3-predictive-sla--배송-파이프라인-조기-경보) | DE | 온라인 데이터 검증기 |
| [유스케이스 4: Doc Generation — 인수 후 온톨로지 통합](#유스케이스-4-doc-suggestions--인수-후-온톨로지-통합) | DE | 자동 문서화 생성 |
| [유스케이스 5: NL Search — GDPR 컴플라이언스 감사](#유스케이스-5-nl-search--gdpr-컴플라이언스-감사) | DA | 자연어 검색 |
| [유스케이스 6: Metrics Dashboard — 전사 메타데이터 건강도](#유스케이스-6-metrics-dashboard--전사-메타데이터-건강도) | DG | 전사 메트릭 시계열 모니터링 |
| [유스케이스 7: Text-to-SQL Metadata — AI 기반 장르 분석](#유스케이스-7-text-to-sql-metadata--ai-기반-장르-분석) | DA | Text-to-SQL 최적화 메타데이터 |
| [유스케이스 8: Multi-Perspective Overview — 전사 데이터 시각화](#유스케이스-8-multi-perspective-overview--전사-데이터-시각화) | DG | 다관점 데이터 오버뷰 |

---

## 데이터 엔지니어링(DE) 그룹

### 유스케이스 1: Deep Ingestion — 레거시 도서 카탈로그 보강

**기능**: 심층 기술 사양 인제스천

#### 시나리오: 레거시 Oracle 도서 카탈로그 메타데이터 보강

**배경:**
Imazon의 Oracle 데이터 웨어하우스는 15년에 걸쳐 구축된 500개 이상의 테이블을 보유하고 있다. 표준 DataHub 커넥터는 스키마 메타데이터(테이블명, 컬럼 타입, 기본키)만 수집했으며, 데이터베이스 외부에 저장된 풍부한 비즈니스 컨텍스트를 놓쳤다: 편집 분류 체계를 기술하는 Confluence 페이지, ISBN-출판사 매핑 Excel 피드, 장르 분류 내부 API, 그리고 베스트셀러 순위와 로열티 계산을 수행하는 PL/SQL 스토어드 프로시저에 숨겨진 리니지 등이 그것이다.

#### DataSpoke 없이

표준 Oracle 커넥터 결과: 컬럼 타입과 키만 있는 500개 테이블. 비즈니스 설명(Confluence에 저장), 출판사 메타데이터(Excel), 장르 분류 체계(API), 스토어드 프로시저 리니지 모두 없음. 데이터 소비자가 DataHub를 탐색해도 `catalog.title_master`가 실제로 무엇을 추적하는지, `reports.monthly_royalties`가 어떻게 계산되는지 알 수 없는 맨 기술 스키마만 보게 된다.

#### DataSpoke와 함께

**다중 소스 보강 설정 등록:**

```python
# POST /api/v1/spoke/de/ingestion/configs
dataspoke.ingestion.register_config({
  "name": "oracle_book_catalog_enriched",
  "source_type": "oracle",
  "schedule": "0 2 * * *",  # 매일 오전 2시

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

**커스텀 PL/SQL 리니지 추출기** (발췌):

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

**보강된 메타데이터 예시 — `catalog.title_master`:**

```yaml
Dataset: catalog.title_master
Platform: Oracle / DWPROD

# 기본 스키마 (표준 커넥터)
Columns: 62 | Primary Key: isbn, edition_id

# 보강 — 비즈니스 컨텍스트 (Confluence)
Description: |
  모든 도서 타이틀의 마스터 카탈로그. ISBN+에디션 당 1행.
  가격, 재고 현황, 편집 분류의 원천(Source of Truth).
  출판사 피드와 편집 검토 큐에서 매일 야간 업데이트.

# 보강 — 소유권 (Confluence + HR API)
Owner: maria.garcia@imazon.com | Team: 카탈로그 엔지니어링

# 보강 — 출판사 메타데이터 (Excel)
Publisher Domain: 전체 임프린트 | Genre Taxonomy: 4단계 계층

# 보강 — 리니지 (PL/SQL 파서)
Upstream: publishers.feed_raw, editorial.review_queue, pricing.base_rates
Generated By: PROC_NIGHTLY_CATALOG_REFRESH (스토어드 프로시저)
Downstream: recommendations.book_features, reports.catalog_summary

# 보강 — 품질 규칙 (CHECK 제약조건)
1. list_price > 0
2. publication_date <= SYSDATE
3. isbn IS NOT NULL AND LENGTH(isbn) IN (10, 13)
```

#### DataHub 연동 포인트

모든 보강된 메타데이터는 `DatahubRestEmitter`를 통해 DataHub에 저장되며, 내부적으로 OpenAPI 엔드포인트 `POST /openapi/v3/entity/dataset`을 호출한다. 각 카테고리는 MCP로 발행되는 DataHub aspect에 매핑된다:

| 인제스천 단계 | DataHub Aspect | REST API Path | 저장 내용 |
|-------------|---------------|---------------|----------|
| 기본 스키마 | `schemaMetadata` | `POST /openapi/v3/entity/dataset` | 컬럼명, 타입, 키 |
| 비즈니스 설명 | `datasetProperties` | `POST /openapi/v3/entity/dataset` | Confluence에서 가져온 `description` |
| PII / 편집 태그 | `globalTags` | `POST /openapi/v3/entity/dataset` | `urn:li:tag:PII`, `urn:li:tag:Editorial_Reviewed` |
| 출판사 분류 | `datasetProperties.customProperties` | `POST /openapi/v3/entity/dataset` | `publisher_domain`, `genre_taxonomy`, `content_rating` |
| 소유권 | `ownership` | `POST /openapi/v3/entity/dataset` | Owner URN + `BUSINESS_OWNER` 타입 |
| PL/SQL 리니지 | `upstreamLineage` | `POST /openapi/v3/entity/dataset` | 소스 → 타겟 데이터셋 URN 엣지 |
| 품질 규칙 | `assertionInfo` + `assertionRunEvent` | `POST /openapi/v3/entity/assertion` | CHECK 제약조건을 assertion으로 변환 |

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

# 설명 + 커스텀 속성 — Confluence에서 가져온 비즈니스 컨텍스트
# REST: POST /openapi/v3/entity/dataset  (aspect: datasetProperties)
emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=dataset_urn,
    aspect=DatasetPropertiesClass(
        description="모든 도서 타이틀의 마스터 카탈로그...",
        customProperties={"genre_taxonomy": "4-level", "publisher_domain": "All imprints"},
    ),
))

# 리니지 — PL/SQL 스토어드 프로시저에서 추출한 업스트림 테이블
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

> **핵심 포인트**: DataHub는 보강 로직을 제공하지 않는다 — DataSpoke가 보낸 것만 저장한다.

#### DataSpoke 커스텀 구현

| 컴포넌트 | 책임 | DataHub가 할 수 없는 이유 |
|---------|------|------------------------|
| **인제스천 설정 레지스트리** | 보강 설정(연결, 필드 매핑, 추출기) 저장 | DataHub 레시피는 표준 커넥터만 처리 |
| **보강 소스 커넥터** | Confluence, Excel/S3, 분류 체계 API에서 가져오기 | DataHub 커넥터는 데이터베이스/플랫폼 중심 |
| **커스텀 추출기 프레임워크** | PL/SQL 리니지 파싱, CHECK 제약조건 추출 플러그인 | 스토어드 프로시저 본문 파싱은 DataHub 범위 밖 |
| **필드 매핑 엔진** | Confluence 라벨 → 태그, Excel 컬럼 → 커스텀 속성 변환 | DataHub는 구조화된 aspect를 수용하지만 비정형 입력을 변환하지 않음 |
| **오케스트레이션 (Temporal)** | 스케줄링, 단계별 재시도, 알림 | DataHub는 레시피를 원자적으로 실행; 다중 소스 오케스트레이션은 Temporal 필요 |
| **벡터 인덱스 동기화** | 성공적 인제스천 시 임베딩 생성 → Qdrant | DataHub는 Elasticsearch 키워드 검색만 제공, 벡터 유사도 검색 없음 |

#### 결과

| 항목 | 표준 커넥터 | DataSpoke Deep Ingestion |
|-----|-----------|--------------------------|
| 스키마 커버리지 | 500 테이블 | 500 테이블 |
| 비즈니스 설명 | 0% | 89% (445/500) |
| 소유권 | 0% | 74% (370/500) |
| 장르 / 출판사 태그 | 0% | 100% |
| 스토어드 프로시저 리니지 | 미지원 | 210개 엣지 추출 |
| 품질 규칙 | 수동 입력만 가능 | 380개 자동 추출 |
| 갱신 주기 | 수동 재실행 | 자동 일일 실행 |

---

### 유스케이스 2: Online Validator — 추천 파이프라인 검증

**기능**: 온라인 데이터 검증기 (DA 그룹과 공유)

#### 시나리오: AI 에이전트가 도서 추천 파이프라인을 구축

**배경:**
데이터 사이언티스트가 AI 에이전트에게 요청한다: "`reviews.user_ratings`와 `orders.purchase_history`를 사용해서 일일 도서 추천 파이프라인을 만들어줘." Validator는 에이전트가 건강한 데이터 소스를 선택하고 규정을 준수하는 결과물을 생성하도록 보장한다.

#### DataSpoke 없이

AI 에이전트가 DataHub에서 "reviews"와 "orders" 테이블을 검색하고, 네이밍 규칙으로 후보를 선택하고, 데이터 품질을 이해하지 못한 채 코드를 생성한다. 지난주 마이그레이션 버그로 `rating_score`에 30% null 비율이 발생한 열화된 테이블(`reviews.user_ratings_legacy`)을 사용하는 파이프라인이 배포될 수 있다.

#### DataSpoke와 함께

**1단계: 시맨틱 탐색**

```
AI 에이전트 쿼리: "ML 학습에 적합한 리뷰와 구매 테이블 찾기"

DataSpoke 응답 (via /api/v1/spoke/de/validator):
- reviews.user_ratings (✓ 품질 점수: 96)
  최근 갱신: 1시간 전 | 완전성: 99.7% | 28개 다운스트림 소비자

- reviews.user_ratings_legacy (⚠ 품질 문제)
  이상: 2024-02-03부터 rating_score에 30% null 비율
  권장: 사용 금지 — reviews.user_ratings 사용 권장

- orders.purchase_history (✓ 권장)
  SLA: 99.9% 정시 | 문서화: 100% | ML 사용 인증 완료
```

**2단계: 컨텍스트 검증**

```python
# POST /api/v1/spoke/de/validator/context
dataspoke.validator.verify_context("reviews.user_ratings_legacy")

# 응답:
{
  "status": "degraded",
  "quality_issues": [{
    "type": "null_rate_anomaly",
    "severity": "high",
    "message": "rating_score null 비율이 0.3%에서 30%로 급증",
    "recommendation": "reviews.user_ratings 사용 권장"
  }],
  "alternative_entities": ["reviews.user_ratings"]
}
```

**3단계: 파이프라인 검증**

```yaml
Pipeline: book_recommendations_daily_v1
Author: AI Agent (claude-sonnet-4.5)

검증 결과:
✓ 문서화: 설명 존재, data-science-team에 소유자 할당됨
✓ 네이밍 규칙: book_recommendation_features (준수)
✓ 품질 검사: NULL 처리 구현됨, 스키마 하위 호환
✓ 리니지 영향: 2개 업스트림 테이블 (모두 정상), 순환 의존성 없음
⚠ 권장사항: 데이터 신선도 검사 추가, 모니터링 알림 구현
```

**4단계**: AI 에이전트가 검증된 소스, 규정 준수 구조, 품질 검사를 갖추고 배포한다.

**5단계: 분석가 검증 (DA 관점)**

마케팅 분석가가 분기별 구매자 세그먼트 리포팅을 위해 `orders.purchase_history`를 Tableau 대시보드에 연결하려 한다. 연결 전에 DA 엔드포인트를 통해 테이블을 검증한다:

```python
# POST /api/v1/spoke/da/validator/check
dataspoke.validator.check({
  "dataset": "orders.purchase_history",
  "use_case": "reporting_dashboard",
  "checks": ["freshness", "certification", "schema_stability"]
})

# 응답:
{
  "status": "approved",
  "certification": "Certified for reporting use",
  "freshness": "Updated 45 min ago (SLA: hourly) ✓",
  "schema_stability": "No breaking changes in 90 days ✓",
  "recommendation": "Safe to connect — certified, stable, and fresh"
}
```

DA 검증 흐름은 **사용 적합성**에 초점을 맞춥니다: 이 테이블이 리포팅 용도로 인증되었는가? 월요일 아침에 깨지지 않을 만큼 스키마가 안정적인가? 대상 사용자에게 충분히 신선한 데이터인가? 이는 **파이프라인 구축**에 초점을 맞추는 DE 흐름(1–4단계)과 대조된다: 소스가 구축할 만큼 건강한가? 품질 이상이 있는가? 출력 스키마가 규정을 준수하는가? 두 흐름 모두 동일한 품질 점수 엔진을 공유하지만, 각 그룹의 관심사에 맞춘 다른 검사 프로필을 적용한다.

#### DataHub 연동 포인트

Validator는 주로 **읽기** 소비자이다. 여러 DataHub aspect를 조회하여 건강도 평가를 구성한다:

| 검증 단계 | DataHub Aspect | REST API Path | 반환 내용 |
|----------|---------------|---------------|----------|
| 시맨틱 탐색 | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | 설명, 태그 — 의미적 의도 매칭 |
| 품질 점수 | `datasetProfile` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | 행 수, null 비율 시계열 |
| 신선도 | `operation` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | `lastUpdatedTimestamp` |
| 다운스트림 수 | `upstreamLineage` | GraphQL: `searchAcrossLineage` | 다운스트림 소비자 수 |
| 폐기 여부 | `deprecation` | `GET /aspects/{urn}?aspect=deprecation` | `deprecated` 플래그, 대체 URN |
| 검증 이력 | `assertionRunEvent` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | 합격/실패 이력 |
| 스키마 검증 | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | 컬럼명, 타입 (네이밍 검사용) |

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetProfileClass,
    OperationClass,
    UpstreamLineageClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))
dataset_urn = make_dataset_urn(platform="oracle", name="reviews.user_ratings_legacy", env="PROD")

# 프로필 이력 — 이상 탐지를 위한 null 비율 시계열
# REST: POST /aspects?action=getTimeseriesAspectValues
profiles = graph.get_timeseries_values(
    dataset_urn, DatasetProfileClass, filter={}, limit=30,
)

# 최근 작업 — 신선도 확인
# REST: POST /aspects?action=getTimeseriesAspectValues
operations = graph.get_timeseries_values(
    dataset_urn, OperationClass, filter={}, limit=1,
)

# 업스트림 리니지 — 의존성 건강 여부
# REST: GET /aspects/{urn}?aspect=upstreamLineage
upstream = graph.get_aspect(dataset_urn, UpstreamLineageClass)
```

> **핵심 포인트**: DataHub는 수동적 데이터 저장소이다. 원시 신호를 제공하며, DataSpoke가 품질 점수, 이상 탐지, 권장사항을 계산한다.

#### DataSpoke 커스텀 구현

| 컴포넌트 | 책임 | DataHub가 할 수 없는 이유 |
|---------|------|------------------------|
| **품질 점수 엔진** | 프로필, assertion, 문서, 신선도 → 0–100 단일 점수 집계 | DataHub는 aspect 간 교차 점수 체계 없음 |
| **Null 비율 이상 탐지** | `datasetProfile` null 비율에 대한 시계열 분석 | DataHub는 프로필을 저장하지만 통계 분석 없음 |
| **대안 추천** | Qdrant에서 의미적으로 유사한 건강한 데이터셋 조회 | DataHub는 키워드 검색만 제공 |
| **파이프라인 검증 엔진** | 네이밍, 스키마 호환성, 리니지 영향 검증 | DataHub는 메타데이터를 저장하지만 그것에 대해 검증하지 않음 |
| **검증 결과 캐시** | 빠른 코딩 루프의 AI 에이전트를 위한 Redis 캐시 | DataHub에는 계산 결과에 대한 캐싱 없음 |

#### 결과

| 지표 | DataSpoke 없이 | DataSpoke와 함께 |
|-----|---------------|----------------|
| 파이프라인 실패율 | ~30% (불량 데이터) | <5% (사전 검증) |
| 사람 리뷰 시간 | 파이프라인당 수 시간 | 수 분 (자동화) |
| 데이터 품질 사고 | 빈번 | 거의 제로 |

---

### 유스케이스 3: Predictive SLA — 배송 파이프라인 조기 경보

**기능**: 온라인 데이터 검증기 (시계열 모니터링)

#### 시나리오: 배송 파트너 API 속도 제한이 주문 이행 대시보드를 위협

**배경:**
Imazon의 `orders.daily_fulfillment_summary` 테이블은 운영, 재무, 고객 지원 부서가 사용하는 물류 대시보드를 구동한다. 이 테이블은 `orders.raw_events`, `shipping.carrier_status`, 그리고 외부 배송 파트너 API의 데이터를 집계하여 매일 오전 9시까지 150만 행을 처리한다.

#### DataSpoke 없이

```
오전 9:00 — 알림: orders.daily_fulfillment_summary가 비어 있음
상태: SLA 위반 — 물류 대시보드 다운
대응: 수동 조사 시작. 오전 10:30에 근본 원인 발견 (배송 API 쓰로틀링).
총 다운타임: 2.5시간.
```

#### DataSpoke와 함께

**오전 7:00 — 조기 경보 (SLA 2시간 전):**

```
DataSpoke 예측 알림:
⚠ 이상: orders.daily_fulfillment_summary

오전 7시 현재 볼륨: 320K 행
예상 (평일 오전 7시): 900K ±5%
편차: -64% (3σ 임계값 초과)

업스트림 분석:
  orders.raw_events: ✓ 정상 (오전 6:30 기준 1.4M 행)
  shipping.carrier_status: ⚠ 40분 지연 (비정상)
    └─ 의존성: shipping_partner_api.tracking
       └─ 문제: API 속도 제한 초과 (429 응답)

근본 원인 (추정): 배송 파트너 API 쓰로틀링
예측: 오전 9시 SLA를 ~1.5시간 초과할 가능성

권장 조치:
  1. shipping_partner_api 속도 제한 확인
  2. logistics-eng에 API 할당량 문의
  3. 대안 고려: 마지막 성공 풀의 캐시된 배송사 데이터 사용

영향:
  - 물류 대시보드 (12명 — 운영팀)
  - 재무 일일 배송 리포트 (오전 9:30 자동 예약)
  - 고객 지원 SLA 트래커 (8명)
```

**오전 7:15** — 운영 엔지니어가 API 쓰로틀링을 확인하고 할당량 증가를 요청한다. 파이프라인은 오전 8시에 복구된다. SLA를 1시간 여유로 충족한다.

**2주차 — 패턴 학습:**

```
DataSpoke 인사이트: orders.daily_fulfillment_summary
패턴: 월요일 오전 7시 볼륨이 다른 평일 대비 지속적으로 -12% (4주 추세)
가설: 주말 주문 백로그가 월요일 아침 배치 지연 발생
자동 조정 임계값: 월요일 오전 7시: 790K ±5% (기존 900K에서 변경)
```

#### DataHub 연동 포인트

Predictive SLA 엔진은 **읽기** 소비자이다. 시계열 프로필과 리니지를 조회하여 SLA 위반 전에 이상을 감지한다:

| 모니터링 단계 | DataHub Aspect | REST API Path | 반환 내용 |
|-------------|---------------|---------------|----------|
| 볼륨 추적 | `datasetProfile` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | 시간별 `rowCount` — 3σ 편차의 기초 |
| 신선도 확인 | `operation` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | `lastUpdatedTimestamp` — 예상 vs 실제 |
| 업스트림 의존성 | `upstreamLineage` | `GET /aspects/{urn}?aspect=upstreamLineage` | 근본 원인 추적을 위한 업스트림 데이터셋 URN |
| 다운스트림 영향 | — | GraphQL: `searchAcrossLineage` | SLA 위반 시 영향받는 대시보드/소비자 |

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetProfileClass,
    UpstreamLineageClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))
dataset_urn = make_dataset_urn(platform="oracle", name="orders.daily_fulfillment_summary", env="PROD")

# 프로필 이력 — 3σ 이상 탐지를 위한 시간별 rowCount
# REST: POST /aspects?action=getTimeseriesAspectValues
profiles = graph.get_timeseries_values(
    dataset_urn, DatasetProfileClass, filter={}, limit=30,
)

# 업스트림 리니지 — 근본 원인 분석을 위한 의존성 추적
# REST: GET /aspects/{urn}?aspect=upstreamLineage
upstream = graph.get_aspect(dataset_urn, UpstreamLineageClass)
```

> **핵심 포인트**: DataHub는 원시 프로필 이력과 리니지 그래프를 저장한다. DataSpoke가 그 위에 통계 모델링, SLA 정의, 예측 알림을 추가한다.

#### DataSpoke 커스텀 구현

| 컴포넌트 | 책임 | DataHub가 할 수 없는 이유 |
|---------|------|------------------------|
| **Prophet/Isolation Forest 엔진** | `rowCount` 이력에 대한 시계열 이상 탐지 | DataHub는 프로필을 저장하지만 통계 모델링 없음 |
| **SLA 설정** | 데이터셋별 SLA 목표 정의 (예: 오전 9시 마감) | DataHub에는 SLA 개념 없음 |
| **업스트림 근본 원인 분석기** | 리니지 추적, 각 업스트림 건강도 확인, 병목 지점 식별 | DataHub는 리니지 그래프를 제공하지만 건강도 평가 없음 |
| **예측 알림 시스템** | 신뢰도 점수가 포함된 위반 전 경고 생성 | DataHub에는 알림/예측 엔진 없음 |
| **임계값 자동 조정** | 요일별 패턴 학습, 기준선 자동 업데이트 | DataHub는 원시 데이터를 저장하지만 패턴 학습 없음 |

#### 결과

| 지표 | 기존 모니터링 | DataSpoke 예측 |
|-----|-------------|---------------|
| 탐지 시점 | 오전 9:00 (위반 후) | 오전 7:00 (위반 전) |
| 대응 시간 | 0분 (이미 늦음) | 120분 (선제적) |
| 비즈니스 영향 | 2.5시간 대시보드 다운 | 다운타임 제로 |
| 근본 원인 파악 | 90분 조사 | 2분 (자동 분석) |

---

### 유스케이스 4: Doc Generation — 인수 후 온톨로지 통합

**기능**: 자동 문서화 생성 (분류 체계/온톨로지 제안)

#### 시나리오: Imazon이 디지털 스타트업 "eBookNow"를 인수

**배경:**
Imazon이 디지털 전용 도서 플랫폼 eBookNow를 인수한다. 합병 후 통합 DataHub 카탈로그에는 700개 이상의 데이터셋(eBookNow 200개 포함)이 있으며 개념이 중복된다. 6개 테이블이 "도서/상품"이라는 개념을 두 회사에 걸쳐 서로 다르게 표현한다. 데이터 거버넌스 팀은 700개 데이터셋을 수동으로 감사할 수 없다.

#### DataSpoke 없이

```
개념: "도서 / 상품"

Imazon (레거시):
  - catalog.title_master          → isbn, title, author_id, list_price
  - catalog.editions              → edition_id, isbn, format, pub_date
  - inventory.book_stock          → isbn, warehouse_id, qty_on_hand

eBookNow (인수):
  - products.digital_catalog      → product_id, title, creator, price_usd
  - content.ebook_assets          → asset_id, product_ref, file_format
  - storefront.listing_items      → listing_id, item_name, seller_price

문제점:
  ✗ 6개 테이블이 서로 다른 스키마와 네이밍으로 "도서/상품"을 표현
  ✗ isbn과 product_id 간의 관계가 문서화되지 않음
  ✗ 다운스트림 파이프라인이 비일관적으로 조인
  ✗ 추천 엔진이 인쇄본과 디지털 모두 제공되는 타이틀을 이중 집계
```

#### DataSpoke와 함께

**1단계: LLM 기반 시맨틱 클러스터링**

DataSpoke는 외부 LLM API를 사용하여 700개 데이터셋 전체에 대한 심층 시맨틱 분석을 수행한다. 단순 임베딩 코사인 유사도에 의존하지 않고, LLM이 스키마 메타데이터, 컬럼명, 설명, 샘플 값을 종합적으로 추론하여 표면적 유사도 지표로는 발견할 수 없는 개념적 중복을 식별한다.

```python
# LLM 기반 클러스터링 간략 예시 (LangChain 사용)
from langchain.chat_models import ChatOpenAI
from langchain.chains import LLMChain
from langchain.prompts import ChatPromptTemplate

llm = ChatOpenAI(model="gpt-4o", temperature=0)

# 1a단계: LLM이 각 데이터셋을 비즈니스 개념 카테고리로 분류
classify_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a data catalog analyst. Given a table's schema and metadata, "
               "classify it into one or more business concept categories."),
    ("human", "Table: {table_name}\nColumns: {columns}\nDescription: {description}\n"
              "Sample values: {samples}\n\nClassify into business concepts.")
])
classify_chain = LLMChain(llm=llm, prompt=classify_prompt)

# 1b단계: 동일 개념 카테고리 내 테이블에 대해 LLM이 쌍별 분석 수행
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
DataSpoke Doc Generation — LLM 기반 시맨틱 클러스터링:

분석 대상: 700 데이터셋 (스키마 + 설명 + 샘플 값을 LLM에 전송)
감지된 시맨틱 클러스터: 38
충돌이 있는 클러스터: 9

클러스터: 도서 / 상품 (Critical)
동일 개념을 나타내는 6개 테이블 감지

LLM 시맨틱 분석:
  catalog.title_master   ←→ products.digital_catalog
    LLM 판정: "양쪽 모두 도서/상품 엔티티를 나타낸다. title_master는 인쇄 중심
               (ISBN 필수), digital_catalog는 디지털 전용 (product_id 기반).
               author_id(FK) vs creator(자유 텍스트)가 핵심 구조적 차이."
    신뢰도: 0.95

  catalog.editions       ←→ content.ebook_assets
    LLM 판정: "동일 개념의 상호 보완적 뷰 — editions는 물리적 포맷 변형을 추적하고,
               ebook_assets는 디지털 파일 포맷과 DRM을 추적한다."
    신뢰도: 0.91

  inventory.book_stock   ←→ storefront.listing_items
    LLM 판정: "개념적 중복 낮음 — book_stock은 창고 재고,
               listing_items는 마켓플레이스 가격 책정. 상품 참조만 공유."
    신뢰도: 0.78

근거 (LLM 보강):
  - 6개 모두 타이틀 유사 필드 포함 (LLM 추론을 통한 100% 의미 매칭)
  - 6개 모두 가격 필드 포함 (95% 매칭 — LLM이 통화 차이 감지)
  - 중복 다운스트림 리니지: 18개 공유 소비자
  - 샘플 레코드 중복 (추정): ISBN/타이틀 매칭 기준 72%
  - LLM 인사이트: eBookNow의 "creator" 필드는 자유 텍스트이며 정규화된 FK가 아님 —
    단순 컬럼명 매칭으로는 이 차이를 발견할 수 없었을 것
```

**1b단계: LLM 지원 소스 코드 참조 분석**

DataSpoke가 eBookNow의 연결된 GitHub 저장소(`ebooknow/catalog-service`, `ebooknow/storefront-api`)를 스캔하여 클러스터링된 테이블을 참조하는 인라인 SQL, DBT 모델, 애플리케이션 코드를 탐색한다. LLM API가 코드 컨텍스트를 해석하여 정확한 컬럼 설명을 생성하고 애플리케이션 코드에 내재된 비즈니스 로직을 식별한다:

```python
# LLM이 소스 코드 참조에서 비즈니스 컨텍스트 추출
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
소스 코드 참조 분석 — 도서 / 상품 클러스터:

스캔된 저장소: 3 (catalog-service, storefront-api, data-pipelines)
발견된 코드 참조: 147
LLM 분석된 참조: 147 (LangChain을 통한 배치 처리)

샘플 발견 — products.digital_catalog.creator:
  파일: catalog-service/src/models/product.py:42
  사용: creator = db.Column(String(255))  # Free-text author name
  LLM 인사이트: "creator는 자유 텍스트 필드 (author_id와 같은 정규화된 FK가 아님).
                 authors 테이블에 대한 검증 없음. 출판사가 입력하며, 쉼표로 구분된
                 복수 저자를 포함할 수 있음."
  → catalog.title_master.author_id와 차별화된 설명 제안

LLM 생성 컬럼 설명 (코드 + 스키마 컨텍스트 기반):
  products.digital_catalog.creator    → "출판사가 입력한 자유 텍스트 저자/크리에이터명.
                                         catalog.title_master.author_id와 달리 authors
                                         테이블에 대한 외래키가 아님. 복수 이름 포함 가능
                                         (쉼표 구분). 정규화 미적용."
  products.digital_catalog.price_usd  → "출판사가 설정한 USD 소매 가격.
                                         storefront-api/pricing 엔드포인트를 통해 업데이트.
                                         통화 변환 없음 (USD 전용). > 0 검증은
                                         체크아웃 플로우에서 수행 (storefront-api/cart.py:88)."
  content.ebook_assets.file_format    → "디지털 파일 포맷 enum: EPUB, PDF, MOBI.
                                         catalog-service 업로드 핸들러에서 검증.
                                         MOBI는 2023년부터 폐기 — LLM이 upload_handler.py:156에서
                                         경고 로그 감지."
```

**1c단계: LLM 기반 유사 테이블 차별화 보고서**

DataSpoke가 각 테이블 쌍의 전체 스키마, 샘플 데이터, 리니지, 코드 참조를 LLM에 전송하고, LLM이 병합/유지/폐기 권장사항이 포함된 구조화된 차별화 보고서를 생성한다:

```python
# LLM이 각 테이블 쌍에 대한 차별화 보고서 생성
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
LLM 생성 차별화 보고서 — 도서 / 상품 클러스터:

┌─────────────────────────┬──────────────────────────┬────────────┐
│ 테이블 쌍                 │ 핵심 차이점                 │ 중복률      │
├─────────────────────────┼──────────────────────────┼────────────┤
│ catalog.title_master    │ 인쇄 중심 SSOT            │            │
│ vs                      │ ISBN 필수 (NOT NULL)     │ 72%        │
│ products.digital_catalog│ 디지털 전용, ISBN 불필요    │            │
│                         │ (30%가 ISBN 없음)        │            │
│ LLM 권장: MERGE — catalog.product_master 생성                   │
│ LLM 근거: "핵심 엔티티는 동일 (도서 상품). ISBN 선택성이 유일한           │
│   구조적 장벽. 대체키 product_id로 해결. creator→author_id            │
│   매핑에 정규화 파이프라인 필요."                                      │
├─────────────────────────┼──────────────────────────┼────────────┤
│ catalog.editions        │ 에디션 수준 상세             │            │
│ vs                      │ (format, pub_date)       │ 65%        │
│ content.ebook_assets    │ 디지털 자산 저장소            │            │
│                         │ (file_format, DRM)       │            │
│ LLM 권장: 양쪽 유지 (상호 보완적 뷰)                                   │
│ LLM 근거: "editions는 출판 변형을 추적하고, assets는 파일 전달을          │
│   추적한다. 병합 시 물리적/디지털 관심사가 혼합된다.                        │
│   edition_id FK로 연결하는 것이 적절."                               │
├─────────────────────────┼──────────────────────────┼────────────┤
│ inventory.book_stock    │ 물리적 창고 수량             │            │
│ vs                      │ (warehouse_id, qty)      │ 41%        │
│ storefront.listing_items│ 마켓플레이스 리스팅           │            │
│                         │ (seller_price, listing)  │            │
│ LLM 권장: book_stock 유지, listing_items 폐기                      │
│ LLM 근거: "listing_items는 재고와 가격 관심사를 혼합한다.                │
│   가격 정보를 정규 가격 테이블로 이전하고                                 │
│   listing_items를 폐기하는 것이 적절."                               │
└─────────────────────────┴──────────────────────────┴────────────┘
```

**2단계: LLM 생성 온톨로지 제안**

LLM이 이전 분석(클러스터, 코드 참조, 차별화 보고서)을 종합하여 병합 스키마, 테이블 역할 할당, 일관성 규칙이 포함된 포괄적 온톨로지 제안을 생성한다:

```python
# LLM이 축적된 컨텍스트에서 전체 온톨로지 제안 생성
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
LLM 제안 정규 엔티티: catalog.product_master

필드 (병합 스키마 — LLM 설계):
  - product_id          (대체키, 신규 — LLM: "ISBN 선택성 문제를 해결")
  - isbn                (nullable — 디지털 전용 타이틀은 ISBN 없음)
  - title               (정규화)
  - format              (enum: print | ebook | audiobook)
  - source_system       ("imazon" | "ebooknow")
  - legacy_isbn         (catalog.title_master.isbn에 매핑)
  - legacy_product_id   (products.digital_catalog.product_id에 매핑)
  - list_price          (USD로 정규화)
  - publication_date

LLM 제안 테이블 역할:
  catalog.product_master        → 신규 정규 SSOT
  catalog.title_master          → 인쇄본 뷰 (유지, 정규 테이블에 별칭)
  catalog.editions              → 에디션 상세 뷰 (유지)
  products.digital_catalog      → 폐기 → 정규 테이블로 마이그레이션
  content.ebook_assets          → 디지털 자산 뷰 (유지)
  inventory.book_stock          → 재고 뷰 (유지)
  storefront.listing_items      → 폐기 → 정규 테이블로 마이그레이션

LLM 생성 일관성 규칙:
  R1. 새 파이프라인은 반드시 catalog.product_master로 조인
  R2. title 정규화: TRIM + title-case
  R3. product_id는 할당 후 불변
  R4. 도서 기원 이벤트에 source_system 태그 필수
  R5. creator→author_id 매핑은 정규화 파이프라인을 거쳐야 함 (LLM 추가)

영향: 18개 파이프라인 업데이트 필요 | 난이도: 중간 (스키마 추가 방식)
LLM 마이그레이션 계획: 폐기 대상 테이블별 단계별 SQL 마이그레이션 스크립트 자동 생성,
  롤백 절차 및 데이터 검증 체크 포함.
```

**3단계: LLM 기반 주간 일관성 검사** — DataSpoke가 LLM을 사용하여 신규 및 수정된 파이프라인의 온톨로지 규칙 위반을 스캔한다. LLM이 SQL 조인 패턴, 컬럼 참조, 데이터 흐름을 분석하여 정규식 기반 규칙으로는 감지할 수 없는 의미적 위반을 탐지한다. 예시: 새 파이프라인이 `catalog.product_master` 대신 `products.digital_catalog`로 조인하여 인쇄 전용 타이틀의 60%가 추천에서 제외됨. LLM이 생성한 자동 수정 제안(92% 신뢰도)에 필요한 정확한 SQL 변경 사항 포함.

#### DataHub 연동 포인트

Doc Generation는 **읽기 + 쓰기** 소비자이다. 클러스터링 분석을 위해 스키마와 속성을 읽고, 폐기 마커와 태그를 DataHub에 다시 기록한다:

| 분석 단계 | DataHub Aspect | REST API Path | 반환/저장 내용 |
|----------|---------------|---------------|--------------|
| 스키마 유사도 | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | 컬럼명, 타입 — 임베딩 유사도 입력 |
| 설명 분석 | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | 시맨틱 매칭을 위한 설명 |
| 공유 소비자 | `upstreamLineage` | GraphQL: `searchAcrossLineage` | 후보 테이블 간 다운스트림 중복 |
| 폐기 표시 | `deprecation` | `POST /openapi/v3/entity/dataset` | `deprecated=true`, `note`, `replacement` URN |
| 소스 시스템 태깅 | `globalTags` | `POST /openapi/v3/entity/dataset` | `urn:li:tag:source_imazon`, `urn:li:tag:source_ebooknow` |
| 소스 코드 참조 | `datasetProperties.customProperties` | `POST /openapi/v3/entity/dataset` | 소스 분석에서 추출한 `code_references`, `auto_generated_descriptions` |

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

# 스키마 읽기 — 임베딩 기반 유사도를 위한 컬럼명과 타입
# REST: GET /aspects/{urn}?aspect=schemaMetadata
imazon_urn = make_dataset_urn(platform="oracle", name="catalog.title_master", env="PROD")
schema = graph.get_aspect(imazon_urn, SchemaMetadataClass)

# 폐기 표시 — 정규 엔티티로 대체된 eBookNow 테이블
# REST: POST /openapi/v3/entity/dataset  (aspect: deprecation)
ebooknow_urn = make_dataset_urn(platform="oracle", name="products.digital_catalog", env="PROD")
emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=ebooknow_urn,
    aspect=DeprecationClass(
        deprecated=True,
        note="온톨로지 통합에 따라 catalog.product_master로 마이그레이션됨",
        replacement=make_dataset_urn(platform="oracle", name="catalog.product_master", env="PROD"),
    ),
))
```

> **핵심 포인트**: DataHub는 스키마 메타데이터와 폐기 마커를 저장한다. DataSpoke가 LLM API를 활용한 시맨틱 클러스터링, 온톨로지 제안 로직, 일관성 규칙 강제를 추가한다.

#### DataSpoke 커스텀 구현

| 컴포넌트 | 책임 | DataHub가 할 수 없는 이유 |
|---------|------|------------------------|
| **LLM 기반 시맨틱 클러스터링** | 스키마 + 설명 + 샘플 값을 LLM API에 전송하여 심층 개념 분석; 데이터셋을 비즈니스 개념 카테고리로 분류; 표면적 임베딩 유사도를 넘어서는 쌍별 의미 비교. LangChain `LLMChain`으로 구조화된 프롬프트 오케스트레이션. | DataHub는 키워드 검색만 제공, 의미 추론 없음 |
| **LLM 온톨로지 제안 엔진** | LLM이 클러스터 분석, 코드 참조, 차별화 보고서를 종합하여 병합 스키마, 테이블 역할, 일관성 규칙이 포함된 정규 엔티티 제안. LangChain `ChatPromptTemplate`으로 다단계 추론. | DataHub는 메타데이터를 저장하지만 스키마 병합이나 추론 로직 없음 |
| **LLM 일관성 규칙 엔진** | LLM이 신규/수정 파이프라인의 SQL 패턴을 온톨로지 규칙(R1–R5) 대비 분석; 정규식 규칙이 놓치는 의미적 위반 탐지; 자동 수정 SQL 생성 | DataHub에는 규칙 정의, 위반 스캔, 코드 분석 없음 |
| **LLM 소스 코드 분석기** | 연결된 저장소 스캔; 코드 스니펫 + 스키마 컨텍스트를 LLM에 전송하여 비즈니스 수준 컬럼 설명 생성. 대규모 코드베이스에 LangChain `RecursiveCharacterTextSplitter` 사용. | DataHub에는 소스 코드 스캔이나 해석 기능 없음 |
| **LLM 차별화 보고서 생성기** | LLM이 테이블 쌍을 전체적으로 비교(스키마, 리니지, 코드 사용, 샘플 데이터)하고 근거가 포함된 구조화된 병합/유지/폐기 권장 생성 | DataHub는 개별 스키마를 저장하지만 비교나 조치 추론 불가 |
| **온톨로지/분류 체계 빌더** *(UC8과 공유)* | 메타데이터에서 비즈니스 개념 분류 체계를 구축·유지하는 재사용 가능한 LLM 기반 서비스. 개념 카테고리, 계층적 관계, 데이터셋-개념 매핑을 Doc Generation(UC4)와 다관점 오버뷰(UC8) 양쪽에 제공. 아래 공통 컴포넌트 참조. | DataHub에는 분류 체계 구축이나 LLM 연동 없음 |

#### 결과

| 지표 | 수동 통합 | DataSpoke Doc Generation |
|-----|---------|---------------------------|
| 제안까지 소요 시간 | ~3개월 (수동 감사) | 수 시간 (자동 클러스터링) |
| 카탈로그 AI 준비도 | 58% | 91% |
| 월간 위반 건수 | 미추적 | 2–3건 (자동 감지) |

---

## 데이터 분석(DA) 그룹

### 유스케이스 5: NL Search — GDPR 컴플라이언스 감사

**기능**: 자연어 검색

#### 시나리오: 법무팀이 마케팅에서 사용하는 유럽 고객 PII를 검색

**배경:**
Imazon 법무팀이 GDPR 감사를 준비하며 다음을 요청한다: "마케팅 분석 파이프라인에서 접근하는 유럽 고객 PII가 포함된 모든 테이블을 찾아주세요." 이는 PII 분류, 지리적 범위, 리니지를 교차 참조해야 하며 — 키워드 검색으로는 처리할 수 없는 다차원 쿼리이다.

#### DataSpoke 없이

수동 프로세스: DataHub에서 "customer" 테이블 검색, 각 설명에서 "EU"/"Europe" 확인, 컬럼명에서 `email`/`name`/`address` 검색, 마케팅 소비자까지 수동으로 리니지 추적, 50개 이상 테이블 검토. **소요 시간: 4–6시간. 정확도: ~70%** (문서화 품질에 따라 다름).

#### DataSpoke와 함께

**쿼리:**

```
자연어 입력 (via /api/v1/spoke/da/search):
"마케팅 분석에서 사용하는 유럽 고객 PII가 있는 테이블 찾기"
```

**응답 (2.3초):**

```
7개 테이블 매칭 결과

높은 우선순위 (직접 PII + 마케팅 사용)

1. customers.eu_profiles (관련도: 98%)
   PII 필드: email, full_name, shipping_address, date_of_birth
   지역 태그: EU/GDPR
   마케팅 리니지:
     └─ marketing.eu_email_campaigns (활성)
        └─ dashboards.eu_campaign_performance
   컴플라이언스: ✓ 보존: 2년 | ✓ 암호화: at-rest + transit
               ⚠ 삭제권: 수동 프로세스

2. orders.eu_purchase_history (관련도: 94%)
   PII 필드: customer_id (연결 가능), shipping_address, payment_last4
   마케팅 리니지:
     └─ marketing.eu_buyer_segmentation
        └─ recommendations.eu_personalized_picks
   컴플라이언스: ✓ 90일 후 익명화 | ✓ GDPR 보존 준수

중간 우선순위 (파생 / 가명화)

3. marketing.eu_reader_segments (관련도: 87%)
   PII 필드: hashed_email, aggregate_reading_score
   업스트림 PII: customers.eu_profiles (1 홉)
   활성 캠페인: 8 | 대시보드 의존: 4

[... 4개 테이블 추가 ...]

요약:
  직접 PII 테이블: 2 | 파생 PII 테이블: 5
  활성 마케팅 파이프라인: 11
  GDPR 컴플라이언스 격차: 1 (삭제권 자동화)
```

**후속 질문:** "자동 삭제권이 없는 테이블은?" → DataSpoke가 `customers.eu_profiles` (수동 SQL 필요)와 `reviews.eu_book_reviews_archive` (콜드 스토리지, 48시간 복원)를 식별한다. 자동 삭제 작업을 권장한다.

#### DataHub 연동 포인트

NL Search는 **읽기** 소비자이다. 벡터 검색 인덱스를 구축하고 쿼리 결과를 보강하기 위해 여러 DataHub aspect를 조회한다:

| 검색 단계 | DataHub Aspect | REST API Path | 반환 내용 |
|----------|---------------|---------------|----------|
| 임베딩 소스 | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | 설명 — Qdrant에 벡터화 |
| 컬럼 수준 PII 탐지 | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | 컬럼명 (`email`, `full_name` 등) |
| PII / GDPR 태그 | `globalTags` | `GET /aspects/{urn}?aspect=globalTags` | `urn:li:tag:PII`, `urn:li:tag:GDPR` |
| 마케팅 리니지 | `upstreamLineage` | GraphQL: `searchAcrossLineage` | 마케팅 도메인의 다운스트림 소비자 |
| 소유권 | `ownership` | `GET /aspects/{urn}?aspect=ownership` | 컴플라이언스 담당자를 위한 데이터 스튜어드 |
| 사용 빈도 | `datasetUsageStatistics` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | `uniqueUserCount`, `totalSqlQueries` |

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetUsageStatisticsClass,
    GlobalTagsClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))
dataset_urn = make_dataset_urn(platform="oracle", name="customers.eu_profiles", env="PROD")

# PII 태그 — GDPR 관련 분류 태그 확인
# REST: GET /aspects/{urn}?aspect=globalTags
tags = graph.get_aspect(dataset_urn, GlobalTagsClass)

# 다운스트림 리니지 — GraphQL을 통한 마케팅 소비자 탐색
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

# 사용 통계 — 검색 결과에서 고트래픽 테이블 우선 표시
# REST: POST /aspects?action=getTimeseriesAspectValues
usage = graph.get_timeseries_values(
    dataset_urn, DatasetUsageStatisticsClass, filter={}, limit=30,
)
```

> **핵심 포인트**: DataHub는 키워드 검색과 원시 메타데이터를 제공한다. DataSpoke가 자연어 파싱, 벡터 유사도, PII 분류 로직, 대화형 정제를 추가한다.

#### DataSpoke 커스텀 구현

| 컴포넌트 | 책임 | DataHub가 할 수 없는 이유 |
|---------|------|------------------------|
| **NL 쿼리 파서** | 자연어를 구조화된 의도(엔티티 타입, 필터, 컴플라이언스 컨텍스트)로 파싱 | DataHub 검색은 키워드 기반, 의도 파싱 없음 |
| **벡터 검색 (Qdrant)** | 하이브리드 검색: 다차원 쿼리를 위한 벡터 유사도 + 그래프 순회 | DataHub는 Elasticsearch 키워드 검색만 제공 |
| **PII 분류 엔진** | 컬럼명 패턴 + 태그 존재로 PII 필드 탐지, 등급별 분류 | DataHub는 태그를 저장하지만 분류 로직 없음 |
| **컴플라이언스 보고서 생성기** | 리니지 다이어그램과 갭 분석이 포함된 GDPR 감사 보고서 자동 생성 | DataHub는 원시 메타데이터를 제공하지만 보고서 생성 없음 |
| **대화형 정제** | 맥락 내 후속 질문 지원 ("어떤 테이블이 부족한지...") | DataHub 검색은 무상태, 대화 지원 없음 |

#### 결과

| 지표 | 기존 검색 | DataSpoke NL Search |
|-----|---------|---------------------|
| 시간 | 4–6시간 | 2–5분 |
| 정확도 | ~70% | ~98% |
| 후속 질문 | 처음부터 다시 | 대화형 정제 |
| 감사 보고서 | 수동 작성 | 자동 생성 |

---

### 유스케이스 7: Text-to-SQL Metadata — AI 기반 장르 분석

**기능**: Text-to-SQL 최적화 메타데이터

#### 시나리오: 비즈니스 분석가가 AI에게 베스트셀러 장르를 질문

**배경:**
Imazon의 비즈니스 분석가가 AI 어시스턴트에게 묻습니다: "Imazon의 Q4 베스트셀러 장르 Top 10은?" AI가 SQL을 생성해야 하지만, DataHub의 표준 메타데이터는 컬럼명과 타입만 제공한다 — 값 프로필, 비즈니스 용어집 매핑, 조인 경로 힌트가 없다. 보강된 컨텍스트 없이 AI는 잘못된 결과를 반환하는 부정확한 SQL을 생성한다.

#### DataSpoke 없이

AI가 DataHub의 컬럼명과 타입만으로 SQL을 생성한다:

```sql
-- AI 생성 SQL (오류)
SELECT genre, COUNT(*) as sales
FROM orders.purchase_history
JOIN catalog.title_master ON orders.purchase_history.isbn = catalog.title_master.isbn
WHERE purchase_date >= '2024-10-01'
  AND genre = 'Fiction'  -- 오류: 실제 값은 'FIC-001' 같은 코드
GROUP BY genre
ORDER BY sales DESC
LIMIT 10;

-- 문제점:
-- 1. "genre" 컬럼(표시명) 사용 — "genre_code"(실제 컬럼)가 아님
-- 2. isbn으로 직접 조인 — edition_id 중간 조인 누락
-- 3. WHERE genre = 'Fiction' — 실제 값은 코드 (FIC-001, NF-002)
-- 4. 사람이 읽을 수 있는 이름에 필요한 genre_hierarchy 테이블 누락
```

결과: 0행 반환. 분석가가 15–30분간 디버깅하고, 데이터 엔지니어에게 문의하며, 수동으로 반복한다.

#### DataSpoke와 함께

**1단계: 메타데이터 컨텍스트 조회**

```python
# POST /api/v1/spoke/da/text-to-sql/context
dataspoke.text_to_sql.get_context({
  "question": "Imazon의 Q4 베스트셀러 장르 Top 10은?",
  "candidate_tables": ["orders.purchase_history", "catalog.title_master", "catalog.genre_hierarchy"]
})

# 응답:
{
  "column_profiles": {
    "catalog.title_master.genre_code": {
      "sample_values": ["FIC-001", "NF-002", "SCI-003", "MYS-004", "ROM-005"],
      "cardinality": 48,
      "distribution": "상위 5개가 전체 타이틀의 62% 차지",
      "business_glossary": "장르 분류 코드 — 표시명은 genre_hierarchy.code로 매핑"
    },
    "catalog.genre_hierarchy.display_name": {
      "sample_values": ["Fiction", "Non-Fiction", "Science Fiction", "Mystery", "Romance"],
      "note": "사람이 읽을 수 있는 장르명 — 출력 표시에 사용"
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
      "source": "리니지 + 과거 쿼리 분석"
    }
  },
  "sample_queries": [
    {
      "description": "월별 장르 판매 리포트 (topSqlQueries에서)",
      "sql": "SELECT gh.display_name, COUNT(*) FROM orders.order_items oi JOIN catalog.editions e ON oi.edition_id = e.edition_id JOIN catalog.title_master tm ON e.isbn = tm.isbn JOIN catalog.genre_hierarchy gh ON tm.genre_code = gh.code GROUP BY gh.display_name"
    }
  ],
  "date_conventions": {
    "orders.purchase_history.purchase_date": "DATE 타입, Q4 = WHERE purchase_date BETWEEN '2024-10-01' AND '2024-12-31'"
  }
}
```

**2단계: AI가 올바른 SQL 생성**

DataSpoke의 보강된 컨텍스트로 AI가 작동하는 쿼리를 생성한다:

```sql
-- AI 생성 SQL (정확 — DataSpoke 컨텍스트 활용)
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

-- 결과:
-- Fiction          42,381
-- Mystery          38,912
-- Romance          31,205
-- Science Fiction  28,744
-- Non-Fiction      25,891
-- ...
```

**3단계: 피드백 루프**

성공한 쿼리가 기록된다. DataSpoke가 비즈니스 용어집 매핑(`genre_code` → `genre_hierarchy.display_name`)과 장르 관련 주제에 대한 4홉 조인 경로를 강화한다.

#### DataHub 연동 포인트

Text-to-SQL 컨텍스트는 **읽기** 중심 소비자이다. AI SQL 생성을 위한 보강 컨텍스트를 구축하기 위해 메타데이터 aspect를 조회한다:

| 컨텍스트 단계 | DataHub Aspect | REST API Path | 반환 내용 |
|-------------|---------------|---------------|----------|
| 컬럼명/타입 | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | 컬럼 정의 — SQL 생성의 기본 스키마 |
| 테이블 설명 | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | 테이블 식별을 위한 비즈니스 설명 |
| 과거 쿼리 | `datasetUsageStatistics` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | `topSqlQueries` — 패턴 추출을 위한 샘플 쿼리 |
| 조인 경로 추론 | `upstreamLineage` | `GET /aspects/{urn}?aspect=upstreamLineage` | 리니지 엣지 — 다홉 조인 권장의 기초 |

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

# 스키마 — SQL 생성을 위한 컬럼명과 타입
# REST: GET /aspects/{urn}?aspect=schemaMetadata
schema = graph.get_aspect(dataset_urn, SchemaMetadataClass)

# 사용 통계 — 샘플 쿼리 패턴을 위한 topSqlQueries
# REST: POST /aspects?action=getTimeseriesAspectValues
usage = graph.get_timeseries_values(
    dataset_urn, DatasetUsageStatisticsClass, filter={}, limit=30,
)

# 업스트림 리니지 — 리니지 엣지에서 조인 경로 추론
# REST: GET /aspects/{urn}?aspect=upstreamLineage
lineage = graph.get_aspect(dataset_urn, UpstreamLineageClass)
```

> **핵심 포인트**: DataHub는 스키마, 사용 통계, 리니지를 저장한다. DataSpoke가 컬럼 값 프로파일링, 비즈니스 용어집 매핑, 조인 경로 추천, LLM 컨텍스트 최적화를 추가한다.

#### DataSpoke 커스텀 구현

| 컴포넌트 | 책임 | DataHub가 할 수 없는 이유 |
|---------|------|------------------------|
| **컬럼 값 프로파일러** | DataHub의 rowCount/nullCount를 넘어서는 샘플 값, 카디널리티, 분포 분석 | DataHub 프로필은 집계 통계를 저장하며 값 수준 분포는 없음 |
| **비즈니스 용어집 매퍼** | 기술 컬럼을 비즈니스 용어에 값 변환과 함께 매핑 (예: `FIC-001` → `Fiction`) | DataHub는 용어집을 저장하지만 자동 컬럼-용어 매핑 없음 |
| **조인 경로 추천기** | 리니지 + 사용 패턴을 결합하여 최적의 다홉 조인 경로 추천 | DataHub는 리니지 엣지를 저장하지만 조인 경로 계산 없음 |
| **SQL 템플릿 생성기** | 메타데이터 + 과거 쿼리에서 SQL 스캐폴드 생성 | DataHub는 원시 메타데이터를 제공하며 SQL 생성 기능 없음 |
| **컨텍스트 윈도우 최적화기** | 정확한 SQL 생성을 위해 LLM 토큰 한도에 맞는 가장 관련 높은 메타데이터 선택 | DataHub에는 LLM 컨텍스트 제약 인식 없음 |

#### 결과

| 지표 | DataSpoke 없이 | DataSpoke와 함께 |
|-----|---------------|----------------|
| SQL 첫 시도 정확도 | ~30% | ~90% |
| 작동하는 쿼리까지 시간 | 15–30분 (수동 반복) | 1–2분 (AI 지원) |
| 조인 경로 정확성 | 빈번한 오류 (잘못된 키) | 95% 정확 (리니지 기반) |
| 비즈니스 용어 해석 | 수동 조회 | 자동 용어집 매핑 |

---

## 데이터 거버넌스(DG) 그룹

### 유스케이스 6: Metrics Dashboard — 전사 메타데이터 건강도

**기능**: 전사 메트릭 시계열 모니터링

#### 시나리오: CDO가 6개 부서에 걸쳐 메타데이터 건강도 이니셔티브 시작

**배경:**
Imazon의 CDO(Chief Data Officer)가 데이터 문서화와 소유권 책임을 개선하기 위한 전사 이니셔티브를 시작한다. 6개 부서가 400개 이상의 데이터셋을 관리하지만, 문서화 커버리지와 소유권 할당은 부서마다 크게 다르다. 거버넌스 팀은 현재 2주가 걸리는 분기별 수동 감사를 수행하며, 작성 즉시 낡아버리는 시점별 스프레드시트를 생성한다.

#### DataSpoke 없이

수동 감사 사이클: 거버넌스 팀이 테이블을 검토하고, 추적 스프레드시트를 만들고, 부서 리드에게 이메일을 보내고, 2주 후 후속 조치하고, 분기별로 반복한다. **문제점:** 노동 집약적 (감사당 2주), 시점별 스냅샷, 자동 추적 없음, 개선 측정 어려움.

#### DataSpoke와 함께

**1주차 — 초기 평가:**

```
DataSpoke 메트릭 대시보드:
전사 메타데이터 건강도 점수: 59/100

부서별 현황:
┌─────────────────────┬────────┬──────────┬────────┬─────────┐
│ 부서                │ 점수   │ 데이터셋 │ 이슈   │ 추세    │
├─────────────────────┼────────┼──────────┼────────┼─────────┤
│ 엔지니어링          │ 76/100 │ 95       │ 23     │ ↑ +3%   │
│ 데이터 사이언스      │ 69/100 │ 72       │ 22     │ → 0%    │
│ 마케팅              │ 54/100 │ 80       │ 37     │ ↓ -2%   │
│ 재무                │ 81/100 │ 38       │ 7      │ ↑ +5%   │
│ 운영                │ 45/100 │ 65       │ 36     │ → 0%    │
│ 출판사 관계          │ 40/100 │ 55       │ 33     │ ↓ -1%   │
└─────────────────────┴────────┴──────────┴────────┴─────────┘

Critical 이슈: 42 | High: 78 | Medium: 118
```

**상세 뷰 — 마케팅:**

```
마케팅 부서 — 점수: 54/100 (목표: 70)

Critical (12 데이터셋):
  - 사용량 높은 테이블에 소유자 누락
  - marketing.campaign_metrics_daily에 설명 없음 (38명의 다운스트림 사용자!)

자동 생성 조치 항목 → marketing-data-lead@imazon.com:
  우선순위 1 (기한: 1주):
  [ ] marketing.campaign_metrics_daily에 소유자 할당
  [ ] 사용량 상위 5개 미문서화 테이블에 설명 추가
  우선순위 2 (기한: 2주):
  [ ] 고객 대면 테이블에 PII 분류 추가
  [ ] 모든 메트릭 테이블의 갱신 주기 문서화
```

**2주차 — 자동 알림** — DataSpoke가 데이터셋 소유자에게 구체적인 조치 항목, 예상 수정 시간 (~5–10분), 예상 점수 영향을 이메일로 발송한다.

**1개월차 — 진행 상황:**

```
전사 건강도 점수: 59 → 70 (+11포인트)

최다 개선: 마케팅 54 → 71 (+17) — 이달의 부서
  35/37 critical 이슈 해결 | 평균 대응: 3일 (기존 12일)

주의 필요: 출판사 관계 40 → 44 (+4) — 목표 속도 미달
  권장: 팀 리드와 1:1 미팅 예약

지표:
  문서화 커버리지: 64% → 78% (+14%)
  소유자 할당률: 79% → 93% (+14%)
  평균 이슈 해결: 4.1일 (목표: 5일) ✓
```

**3개월차 — 마일스톤:** 전사 점수 77/100 도달 (목표: 70). 모든 부서가 최소 임계값 초과. 문서화 감쇠율 -2.1%/월 추적 (새 테이블 생성이 문서화보다 빠름). DataSpoke가 새 테이블 생성 시 필수 문서화 체크리스트를 권장한다.

#### DataHub 연동 포인트

메트릭 대시보드는 **읽기** 소비자이다. 모든 데이터셋을 조회하여 집계 건강도 점수를 계산한다:

| 건강도 지표 | DataHub Aspect | REST API Path | 반환 내용 |
|-----------|---------------|---------------|----------|
| 설명 커버리지 | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | `description` 필드 존재/부재 |
| 소유자 할당 | `ownership` | `GET /aspects/{urn}?aspect=ownership` | Owner URN 목록 — 비어있으면 미할당 |
| 컬럼 문서화 | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | 컬럼별 `description` — 비어있으면 미문서화 |
| 태그 커버리지 | `globalTags` | `GET /aspects/{urn}?aspect=globalTags` | PII 분류 태그 존재 또는 누락 |
| 사용 인기도 | `datasetUsageStatistics` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | `uniqueUserCount` — 사용량 높은 격차 우선 처리 |
| 엔티티 열거 | — | GraphQL: `scrollAcrossEntities` | 도메인/부서별 모든 데이터셋 목록 |

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    OwnershipClass,
)

graph = DataHubGraph(DatahubClientConfig(server=DATASPOKE_DATAHUB_GMS_URL, token=DATASPOKE_DATAHUB_TOKEN))

# 모든 데이터셋 열거 — 건강도 점수 계산을 위한 반복
# GraphQL: scrollAcrossEntities 또는 REST 필터
dataset_urns = list(graph.get_urns_by_filter(entity_types=["dataset"]))

for dataset_urn in dataset_urns:
    # 소유권 — 소유자 할당 여부 확인
    # REST: GET /aspects/{urn}?aspect=ownership
    ownership = graph.get_aspect(dataset_urn, OwnershipClass)

    # 설명 — 문서화 커버리지 확인
    # REST: GET /aspects/{urn}?aspect=datasetProperties
    properties = graph.get_aspect(dataset_urn, DatasetPropertiesClass)
```

> **핵심 포인트**: DataHub는 데이터셋별 메타데이터 aspect를 저장한다. DataSpoke가 이를 데이터셋 간 건강도 점수, 부서 순위, 추세 분석으로 집계한다.

#### DataSpoke 커스텀 구현

| 컴포넌트 | 책임 | DataHub가 할 수 없는 이유 |
|---------|------|------------------------|
| **건강도 점수 집계기** | 설명, 소유권, 태그, 컬럼 문서, 신선도로부터 0–100 점수 계산 | DataHub에는 aspect 간 교차 점수 체계 없음 |
| **부서 매퍼** | 소유권 → HR API 조회를 통한 데이터셋-부서 매핑 | DataHub는 소유권 URN을 저장하지만 조직 구조 인식 없음 |
| **이슈 트래커** | PostgreSQL에서 메타데이터 격차 탐지, 우선순위화, 추적 (critical/high/medium) | DataHub에는 이슈 수명주기 관리 없음 |
| **알림 엔진** | 데이터셋 소유자에게 조치 항목, 예상 수정 시간, 예상 점수 영향 이메일 발송 | DataHub에는 외부 알림 시스템 없음 |
| **추세 분석** | 시간별 건강도 점수 추적, 감쇠율 계산, 개선 예측 | DataHub는 시점별 aspect를 저장하지만 메타데이터 품질의 시계열 집계 없음 |

#### 결과

| 지표 | 분기별 수동 감사 | DataSpoke 메트릭 대시보드 |
|-----|---------------|------------------------|
| 감사 주기 | 2주, 분기별 | 실시간, 상시 |
| 이슈 대응 시간 | 평균 12일 | 평균 3일 |
| 건강도 점수 개선 | 미측정 | 3개월 만에 59 → 77 |
| 거버넌스 팀 노력 | 100% 수동 | 80% 절감 |

---

### 유스케이스 8: Multi-Perspective Overview — 전사 데이터 시각화

**기능**: 다관점 데이터 오버뷰

#### 시나리오: CDO가 전체 데이터 자산을 시각화하고자 함

**배경:**
UC6의 메타데이터 건강도 이니셔티브가 전사 점수를 59에서 77로 올린 후, Imazon의 CDO가 다음 질문을 던집니다: "우리 전체 데이터 환경을 보여주세요 — 스프레드시트가 아니라 탐색할 수 있는 것으로." 8개 도메인에 걸친 700개 이상의 데이터셋으로, 표 형태의 건강도 점수만으로는 부족한다. 거버넌스 팀은 평면 테이블에서 보이지 않는 패턴, 사각지대, 구조적 문제를 발견하기 위한 시각적 탐색이 필요하다.

#### DataSpoke 없이

수동 프로세스: 거버넌스 분석가가 3일간 Lucidchart 다이어그램을 작성한다. 노드는 수동으로 배치하고, 데이터 품질에 대한 암묵지를 기반으로 색상을 수동으로 지정한다. 메달리온 레이어 분류(Bronze/Silver/Gold)는 팀별로 엔지니어에게 물어보며 수행한다. 다이어그램은 즉시 구식이 된다 — 다음 주에 추가된 새 데이터셋은 나타나지 않는다. **소요 시간: 3–5일. 커버리지: 데이터셋의 ~60% (나머지는 "알 수 없음").**

#### DataSpoke와 함께

**뷰 1: 분류 체계/온톨로지 그래프**

그래프를 렌더링하기 전에, DataSpoke는 공유 **온톨로지/분류 체계 빌더**(UC4에서 사용하는 것과 동일한 LLM 기반 서비스)를 사용하여 모든 데이터셋을 온톨로지 카테고리에 매핑한다. LLM이 각 테이블의 스키마, 설명, 컬럼명, 샘플 값, 리니지 컨텍스트를 분석하여 비즈니스 개념 카테고리를 할당한다 — 이 의미적 그룹핑이 그래프 레이아웃을 구동한다.

```python
# LLM 기반 온톨로지 카테고리 매핑 (공유 온톨로지/분류 체계 빌더 사용)
from langchain.chat_models import ChatOpenAI
from langchain.chains import LLMChain
from langchain.prompts import ChatPromptTemplate

llm = ChatOpenAI(model="gpt-4o", temperature=0)

# 1단계: LLM이 테이블 메타데이터를 사용하여 각 데이터셋을 온톨로지 카테고리에 매핑
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

# 2단계: LLM이 그래프 엣지를 위한 카테고리 간 관계 정제
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
DataSpoke 다관점 오버뷰 — 분류 체계 그래프:

사전 처리: LLM 온톨로지 카테고리 매핑
  700개 데이터셋을 LLM API로 분석 (배치 처리)
  할당된 온톨로지 카테고리: 주 12개, 부 34개
  매핑 신뢰도: 평균 94% (설명이 있는 데이터셋)
               평균 78% (설명이 없는 데이터셋 — LLM이 스키마에서 추론)

  샘플 매핑:
    catalog.title_master        → Product/Catalog (0.98)
    orders.purchase_history     → Order/Transaction (0.97)
    recommendations.book_features → Recommendation (0.92), Product/Catalog (0.71)
    marketing.eu_email_campaigns → Marketing (0.96), Customer (0.68)

노드: 700 데이터셋 (온톨로지 카테고리 + 건강도 점수로 색상 지정)
엣지: 1,842 (1,204 리니지 + 638 LLM 추론 의미적 관계)
자동 감지 도메인: 12개 비즈니스 클러스터 (LLM 할당 온톨로지 카테고리)

시각화:
  노드 색상: 건강도 점수 (🔴 <50 | 🟡 50-70 | 🟢 >70)
  노드 크기: 사용량 (클수록 소비자 많음)
  노드 그룹: LLM 할당 온톨로지 카테고리 (포스 다이렉티드 클러스터링)
  엣지 유형: 실선 = 리니지 | 점선 = LLM 추론 의미적 관계

핵심 발견 — 거버넌스 사각지대:
  클러스터: recommendations.* (12개 데이터셋)
  LLM 카테고리: "Recommendation" — 높은 비즈니스 중요도 추론
  상태: 전부 빨간색 (건강도 점수 12–38)
  문제:
    - 문서화된 리니지 제로 (업스트림/다운스트림 기록 없음)
    - 높은 사용량: 클러스터 전체에 주당 45명의 고유 사용자
    - 12개 중 8개 테이블에 소유자 미할당
    - LLM 추론 업스트림 (스키마 + 네이밍 분석 기반): catalog.title_master,
      reviews.user_ratings, orders.purchase_history
  리스크: 핵심 비즈니스 기능(도서 추천)이 완전히 미문서화되고
          소유자 없는 데이터 인프라에서 구동 중

  상세 드릴다운:
    recommendations.book_features       → 건강도: 38 | 사용자: 22 | 소유자: 없음
    recommendations.collaborative_scores → 건강도: 25 | 사용자: 18 | 소유자: 없음
    recommendations.content_embeddings   → 건강도: 12 | 사용자: 8  | 소유자: 없음
    [... 9개 더 ...]

  조치: 엔지니어링 VP에게 에스컬레이션 — 필수 소유권 + 문서화 스프린트
```

**뷰 2: 메달리온 아키텍처**

```
DataSpoke 다관점 오버뷰 — 메달리온 분류:

자동 분류 (리니지 깊이 + 네이밍 패턴 + 스키마 분석 기반):

┌──────────────┬────────┬──────────────────────────────────────────────┐
│ 레이어       │ 수량   │ 특성                                             │
├──────────────┼────────┼──────────────────────────────────────────────┤
│ 🥉 Bronze    │ 180    │ 원시 인제스천, 외부 소스, _raw 접미사               │
│ 🥈 Silver    │ 120    │ 정제/조인, _cleaned/_enriched 접미사             │
│ 🥇 Gold      │ 55     │ 비즈니스 준비 집계, reports.* 도메인               │
│ ❓ 미분류     │ 345    │ 가용 메타데이터로 레이어 추론 불가                    │
└──────────────┴────────┴──────────────────────────────────────────────┘

격차 분석:
  Bronze → Silver 전환율: 60% (180개 중 108개가 Silver 대응 테이블 보유)
  Bronze 테이블의 40% (72개)가 Silver 대응 테이블 없음
    → 인제스천되었지만 정제되지 않음 — 정리 후보

  상위 정리 후보 (다운스트림 없고 사용량 낮은 Bronze):
    publishers.feed_raw_legacy      — 마지막 접근: 8개월 전 | 0 다운스트림
    shipping.carrier_raw_v1         — 마지막 접근: 6개월 전 | 0 다운스트림
    marketing.campaign_import_2022  — 마지막 접근: 11개월 전 | 0 다운스트림
    [... 12개 더 식별 ...]

  스토리지 영향: 부실 Bronze 테이블에서 ~2.3 TB 복구 가능

  미분류 분류 작업:
    345개 데이터셋이 수동 검토 또는 보강된 메타데이터를 통한 자동 분류 필요
    DataSpoke 권장: 사용량 상위 50개에 Deep Ingestion (UC1) 실행
      → 345개 중 180개 (52%) 자동 분류 예상
```

#### DataHub 연동 포인트

다관점 오버뷰는 **읽기** 중심 소비자이다. 그래프와 분류 뷰를 구축하기 위해 모든 데이터셋을 광범위하게 조회한다:

| 시각화 단계 | DataHub Aspect | REST API Path | 반환 내용 |
|-----------|---------------|---------------|----------|
| 도메인 힌트 | `datasetProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | 설명, `customProperties` — 도메인 분류 입력 |
| 리니지 엣지 | `upstreamLineage` | `GET /aspects/{urn}?aspect=upstreamLineage` | 업스트림 데이터셋 URN — 그래프 구축 |
| 메달리온 / 도메인 태그 | `globalTags` | `GET /aspects/{urn}?aspect=globalTags` | `urn:li:tag:bronze`, `urn:li:tag:gold`, 도메인 태그 |
| 부서 그룹핑 | `ownership` | `GET /aspects/{urn}?aspect=ownership` | Owner URN → 클러스터 색상을 위한 부서 매핑 |
| 노드 크기를 위한 사용량 | `datasetUsageStatistics` (시계열) | `POST /aspects?action=getTimeseriesAspectValues` | `uniqueUserCount`, `totalSqlQueries` — 노드 크기 입력 |
| 스키마 유사도 | `schemaMetadata` | `GET /aspects/{urn}?aspect=schemaMetadata` | 컬럼명/타입 — 유사도 엣지를 위한 중복 계산 |

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

# 그래프 구축을 위한 모든 데이터셋 열거
# GraphQL: scrollAcrossEntities 또는 REST 필터
dataset_urns = list(graph.get_urns_by_filter(entity_types=["dataset"]))

for dataset_urn in dataset_urns:
    # 리니지 — 그래프 구축을 위한 엣지
    # REST: GET /aspects/{urn}?aspect=upstreamLineage
    lineage = graph.get_aspect(dataset_urn, UpstreamLineageClass)

    # 스키마 — 유사도 엣지를 위한 컬럼 중복
    # REST: GET /aspects/{urn}?aspect=schemaMetadata
    schema = graph.get_aspect(dataset_urn, SchemaMetadataClass)

    # 태그 — 메달리온 레이어 및 도메인 분류
    # REST: GET /aspects/{urn}?aspect=globalTags
    tags = graph.get_aspect(dataset_urn, GlobalTagsClass)

    # 속성 — 도메인 클러스터링을 위한 설명
    # REST: GET /aspects/{urn}?aspect=datasetProperties
    properties = graph.get_aspect(dataset_urn, DatasetPropertiesClass)
```

> **핵심 포인트**: DataHub는 데이터셋별 메타데이터(리니지, 스키마, 태그, 사용량)를 제공한다. DataSpoke가 LLM 기반 온톨로지 카테고리 매핑을 선행한 후, 인터랙티브 그래프 시각화, 자동 분류, 사각지대 탐지로 집계한다.

#### DataSpoke 커스텀 구현

| 컴포넌트 | 책임 | DataHub가 할 수 없는 이유 |
|---------|------|------------------------|
| **온톨로지/분류 체계 빌더** *(UC4와 공유)* | 그래프 렌더링 전 모든 데이터셋을 비즈니스 온톨로지 카테고리에 매핑하는 LLM 기반 서비스. LLM API로 스키마, 설명, 태그, 리니지를 분석하여 주/부 카테고리 할당. UC4의 시맨틱 클러스터링과 동일한 서비스 사용. 아래 공통 컴포넌트 참조. | DataHub에는 LLM 기반 분류 체계 구축 없음 |
| **그래프 레이아웃 엔진** | 리니지 + LLM 추론 의미적 관계 엣지에서 포스 다이렉티드 그래프 생성; LLM 할당 온톨로지 카테고리로 노드 그룹핑; 인터랙티브 줌/필터 | DataHub는 기본 리니지 뷰어가 있지만 의미적 그룹핑을 포함한 전체 자산 그래프 없음 |
| **LLM 도메인 분류기** | LLM이 스키마 + 설명 + 리니지를 분석하여 데이터셋의 비즈니스 도메인 자동 분류; 임베딩 전용 접근 방식을 LangChain을 통한 풍부한 의미 추론으로 대체 | DataHub는 수동 도메인 할당만 지원 |
| **메달리온 레이어 탐지기** | 리니지 깊이, 네이밍 패턴, 변환 복잡도에서 Bronze/Silver/Gold 추론 | DataHub는 태그를 저장하지만 메달리온 분류를 위한 추론 로직 없음 |
| **건강도 색상화기** | 복합 건강도 점수를 그래프 노드의 시각적 지표(색상, 크기, 투명도)로 매핑 | DataHub에는 시각적 건강도 오버레이 기능 없음 |
| **사각지대 분석기** | 고아 데이터셋, 누락된 리니지, 막다른 Bronze 테이블, 소유자 없는 고사용량 클러스터 탐지 | DataHub는 원시 메타데이터를 제공하지만 데이터셋 간 이상 탐지 없음 |

#### 결과

| 지표 | 수동 다이어그래밍 | DataSpoke 다관점 |
|-----|----------------|-----------------|
| 전체 자산 뷰까지 시간 | 3–5일 | 수 분 (자동 생성) |
| 데이터셋 커버리지 | ~60% (알려진 테이블) | 100% (등록된 모든 데이터셋) |
| 사각지대 탐지 | 임기응변, 암묵지 | 체계적, 자동화 |
| 메달리온 분류 | 수동, 팀별 설문 | 자동 추론, 상시 업데이트 |
| 구식 여부 | 즉시 구식 | 실시간, 자동 갱신 |

---

## 공통 컴포넌트: 공유 온톨로지/분류 체계 빌더

UC4(Doc Generation)와 UC8(Multi-Perspective Overview)는 모두 데이터셋을 비즈니스 개념 카테고리에 매핑해야 한다 — UC4는 시맨틱 클러스터링과 온톨로지 통합을 위해, UC8은 그래프 노드 그룹핑과 도메인 분류를 위해. 이 로직을 중복하지 않고, DataSpoke는 재사용 가능한 백엔드 서비스로 **공유 온톨로지/분류 체계 빌더**를 제공한다.

**아키텍처:**

```
┌─────────────────────────────────────────────────────────────┐
│              온톨로지/분류 체계 빌더 서비스                        │
│                                                             │
│  입력: DataHub 메타데이터 (스키마, 설명, 태그,                      │
│        리니지, 샘플 값) — 전체 데이터셋 대상                        │
│                                                             │
│  처리 (LangChain을 통한 LLM 기반):                              │
│    1. 데이터셋 → 개념 분류 (데이터셋별 LLM 호출)                     │
│    2. 개념 계층 구조 생성 (LLM 종합)                              │
│    3. 개념 간 관계 추론 (LLM 쌍별 분석)                           │
│    4. 신뢰도 점수 산정 및 휴먼 리뷰 큐                             │
│                                                             │
│  출력: PostgreSQL에 저장된 영속 온톨로지 그래프                     │
│    - concept_categories (id, name, parent_id, description)  │
│    - dataset_concept_map (dataset_urn, concept_id, score)   │
│    - concept_relationships (concept_a, concept_b, type)     │
│                                                             │
│  소비자:                                                     │
│    UC4: 시맨틱 클러스터링, 온톨로지 통합                           │
│    UC8: 그래프 노드 그룹핑, 도메인 분류                            │
│    향후: NL Search (UC5) 개념 인식 쿼리 확장                      │
└─────────────────────────────────────────────────────────────┘
```

**핵심 설계 포인트:**
- **LLM API가 중심**: 모든 분류, 계층 추론, 관계 탐지 단계에서 LangChain을 통해 외부 LLM API를 사용한다. LLM은 스키마 구조, 네이밍 관례, 설명, 리니지를 종합적으로 추론한다 — 단순 임베딩 유사도가 아님.
- **증분 업데이트**: 새 데이터셋이 인제스천(UC1)되거나 스키마가 변경되면, 빌더가 영향받는 데이터셋만 재분류하고 다운스트림 소비자(UC4, UC8)에 변경 사항을 전파한다.
- **휴먼 인 더 루프**: 낮은 신뢰도 분류(< 0.7)는 거버넌스 팀 리뷰 큐에 추가된다. LLM이 근거를 제공하여 사람의 의사결정을 가속한다.
- **버전 관리된 분류 체계**: 각 분류 체계 빌드는 버전이 지정된다. UC4의 온톨로지 제안은 특정 분류 체계 버전을 참조하여 재현성을 보장한다.

---

## 요약: 전달 가치

| 유스케이스 | 사용자 그룹 | 기능 | 기존 방식 | DataSpoke와 함께 | 개선 효과 |
|-----------|-----------|------|---------|----------------|----------|
| **레거시 도서 카탈로그 보강** | DE | Deep Ingestion | 수동 메타데이터 입력, 리니지 없음 | 자동 다중 소스 보강 | 89% 보강, 210개 리니지 엣지 |
| **추천 파이프라인 검증** | DE / DA | Online Validator | ~30% 실패율 (불량 데이터) | <5% 실패 (사전 검증) | 사고 83% 감소 |
| **배송 SLA 조기 경보** | DE | Online Validator | 위반 후 사후 알림 | 2시간 이상 사전 예측 경보 | SLA 위반 제로 |
| **인수 후 온톨로지** | DE | Doc Generation | 3개월 수동 통합 | 수 시간 내 자동 제안 | 수십 배 빠름 |
| **GDPR 컴플라이언스 감사** | DA | NL Search | 4–6시간 수동 검색 | 2–5분 자동 검색 | 98% 시간 절약 |
| **전사 메타데이터 건강도** | DG | Metrics Dashboard | 분기별 수동 감사 | 실시간 상시 모니터링 | 80% 효율 향상 |
| **AI 기반 장르 분석** | DA | Text-to-SQL Metadata | 수동 SQL, 잘못된 조인/값 | 90% 첫 시도 정확도 | ~60% SQL 정확도 향상 |
| **전사 데이터 시각화** | DG | Multi-Perspective Overview | 수동 다이어그래밍, 수일 | 실시간 자동 생성 그래프 | 체계적 사각지대 탐지 |

**공통 이점:**
- **AI 준비 완료:** 자율 에이전트가 Imazon의 프로덕션 데이터와 안전하게 작업 가능
- **실시간 인텔리전스:** 사후 대응에서 선제적 데이터 관리로 전환
- **컨텍스트 인식:** 모든 부서에 걸쳐 데이터 관계와 비즈니스 의미를 이해
- **측정 가능한 영향:** 품질, 컴플라이언스, 효율성의 정량적 개선
- **온톨로지 건강:** 인수와 유기적 성장을 통해서도 카탈로그의 의미적 일관성 유지
- **LLM 네이티브:** UC4와 UC8 전반에서 시맨틱 분석, 온톨로지 구축, 코드 해석, 일관성 강제를 위해 외부 LLM API(LangChain 경유)를 광범위하게 활용
