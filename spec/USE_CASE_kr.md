# DataSpoke: 유스케이스 시나리오

> **문서 목적 안내**
> 이 문서는 아이디어 정립과
> 통합 테스트 케이스의 기반을 마련하기 위한
> 개념적 시나리오를 제시한다.
> 각 시나리오는 DataSpoke의 의도된 기능을 보여주지만,
> 구현 사양은 아니다.
> 기술 아키텍처와 기능 우선순위는 별도의 기술 사양 문서
> (`ARCHITECTURE.md`, `feature/*.md`)에서 정의한다.
> 시나리오가 하위 우선순위 사양에 아직 반영되지 않은 개념을 도입할 때는
> `(하위 사양 후속 반영 필요)` 메모로 그 갭을 표시한다.

이 문서는 `MANIFESTO_kr.md` §2.1에서 정의한 다섯 기능 —
**Ingestion Control**, **Validation**, **Ontology Generation**, **Metadata Generation**, **Governance** —
을 DataSpoke가 어떻게 구현하는지를 보여준다.
모든 시나리오는 가상의 온라인 서점 **Imazon**이라는
단일 회사 컨텍스트를 공유하므로, 유스케이스가 공존하고 서로 보완된다.

사용자 그룹 구분(데이터 엔지니어링 / 데이터 분석 / 데이터 거버넌스)은
UI와 API의 확장 지점으로 남지만,
기능 자체는 사용자 그룹별로 분할되지 않는다.

---

## 가상 회사 프로필: Imazon

Imazon은 온라인 서점이다.
데이터 자산은 작고 건강하며, 이미 DataHub 위에서 동작한다.
Imazon이 DataSpoke를 도입하는 이유는
레거시 정리가 아니라,
인제스천·품질·문서화·거버넌스에 대해
한 곳에서의 가시성과 관리성을 더 얻기 위해서다.

**이 문서 전체에서 사용하는 데이터 소스**

- **PostgreSQL OLTP**
  - `catalog.books` — 도서 카탈로그 (도서 한 행)
  - `orders.line_items` — 주문 항목 (주문 내 도서 한 행)
  - `customers.profiles` — 등록 고객 프로필
- **Kafka 토픽**
  - `orders.shipments` — 외부 배송 서비스가 발행하는 배송 이벤트
  - `orders.events` — 주문 서비스가 발행하는 주문 상태 변경 이벤트

일부는 DataSpoke가 DataHub로 인제스트하고,
일부는 Imazon이 이미 운영하는 외부 파이프라인이 인제스트한다.
DataSpoke는 두 모드를 모두 지원한다.

**기능 매핑**

| # | MANIFESTO 기능 | 유스케이스 |
|---|---|---|
| UC1 | Ingestion Control | [Active와 Passive 인제스천](#uc1-ingestion-control) |
| UC2 | Validation | [규칙 등록, 스케줄·Dry-Run 실행](#uc2-validation) |
| UC3 | Ontology Generation | [Imazon 데이터셋 전반의 개념 추론](#uc3-ontology-generation) |
| UC4 | Metadata Generation | [설명·MD 문서 제안](#uc4-metadata-generation) |
| UC5 | Governance | [인제스천 신선도와 검증 점수](#uc5-governance) |

---

## UC1: Ingestion Control

**MANIFESTO §2.1 기능**:
*Ingestion Control — 데이터 인제스천의 설정·제어·관리를
한 곳에서 수행하는 편의 기능.*

### User Story

> *데이터 팀원으로서*,
> *DataSpoke가 직접 인제스트하든 외부 파이프라인이 DataHub로 인제스트하든
> 관계없이 모든 데이터셋을 등록·실행·관찰하고 싶다*,
> *그래서* 단일 DataSpoke surface가 자산 전체의
> 인제스천 설정·실행·이벤트 이력을 다루도록 한다.

지원하는 인제스천 모드는 두 가지다:

- **Active** — DataSpoke가 인제스터다.
  Airflow tier DAG이 설정된 `schedule_tier`(`hourly` / `daily` / `weekly`)에 따라
  플랫폼별 추출기를 실행하고 결과를 DataHub로 emit한다.
  수동·dry-run 실행도 지원된다.
- **Passive** — 외부 시스템이 DataHub에 직접 인제스트한다.
  DataSpoke는 추출기를 실행하지 않고,
  데이터셋의 인제스천 설정을 `mode: passive`로 표시할 뿐이다.
  `datahub-ingestion-status-sync` Airflow DAG이 **시간마다** 실행되어,
  passive로 표시된 모든 데이터셋의 DataHub 인제스천 실행 이력을 폴링하고
  결과 상태를 `event/ingestion`에 한 행씩 기록한다.
  덕분에 클라이언트 입장에서는 모드와 무관하게 동일한 API surface로 보인다.

> *(하위 사양 후속 반영 필요)*
> 여기서 도입한 `mode: active | passive` 필드와
> `datahub-ingestion-status-sync` DAG은
> `API.md`, `feature/BACKEND.md`, `feature/BACKEND_SCHEMA.md`에
> 후속 반영이 필요하다 —
> `ingestion_configs`에 `mode` 필드를 모델링하고,
> 동기화 DAG을 등록하고,
> DataHub 실행 이력이 `event/ingestion`에 어떻게 매핑되는지 기술해야 한다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/ingestion/conf` | 인제스천 설정 등록·읽기·갱신·삭제 (`mode`, `platform`, `locator`, `identifier`, `auth`, `is_enabled`, active 모드용 `schedule_tier`) |
| `POST /spoke/common/data/{urn}/method/ingestion/run` | 수동 실행 (`dry_run: true`로 연결 점검) — active 설정에서만 |
| `GET /spoke/common/data/{urn}/event/ingestion` | 데이터셋별 인제스천 이벤트 이력 (active: DataSpoke 실행이 기록; passive: 시간별 DataHub 동기화가 기록) |
| `GET /spoke/common/ingestion` | 데이터셋별 `attr/ingestion/*`을 집계하는 크로스 데이터셋 리스트 뷰 |

### Imazon 예시

**Active — `catalog.books` (Postgres, daily).**

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "active",
  "platform": "postgres",
  "locator": {"host": "pg-oltp.imazon.internal", "port": 5432},
  "identifier": {"database": "imazon", "schema_name": "catalog", "table": "books"},
  "auth": {"username": "spoke_reader", "secret_ref": "vault://imazon/pg/spoke_reader"},
  "is_enabled": true,
  "schedule_tier": "daily"
}
```

스케줄을 켜기 전에 코딩 에이전트가 연결을 검증한다:

```http
POST .../method/ingestion/run    { "dry_run": true }
```

일간 Airflow tier DAG 실행 후, 팀이 데이터셋별 이벤트 이력을 조회한다:

```http
GET .../event/ingestion?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

**Passive — `orders.shipments` (Kafka, 외부 인제스트).**

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:kafka,orders.shipments,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "kafka",
  "locator": {"bootstrap_servers": "kafka.imazon.internal:9092"},
  "identifier": {"topic": "orders.shipments", "cluster": "PROD"},
  "is_enabled": true
}
```

`schedule_tier`는 없다.
DataSpoke가 추출기를 돌리지 않기 때문이다.
외부 데이터 파이프라인(DataSpoke 외부의 Airflow DAG)이
스키마와 속성을 DataHub로 직접 emit한다.

매시간 DataSpoke의 `datahub-ingestion-status-sync` DAG이
passive 표시된 데이터셋의 DataHub 실행 이력을 폴링해
events 테이블에 한 행씩 기록한다.
Imazon은 동일한 API로 이벤트를 읽는다:

```http
GET .../event/ingestion?from=…&to=…
```

**크로스 데이터셋 오버뷰.**

```http
GET /api/v1/spoke/common/ingestion?limit=100
```

데이터셋별로 한 행을 반환한다.
각 행은 `attr/ingestion/*` 집합(모드, 스케줄, 마지막 이벤트 상태)을 담는다.
대시보드와 일괄 감사에 유용하다.

---

## UC2: Validation

**MANIFESTO §2.1 기능**:
*Validation — 검증 규칙의 등록·실행·관리.
Dry-run 검증, 시점 과거 데이터 검증, 실시간 API를 지원한다.*

### User Story

> *데이터 팀원으로서*,
> *데이터셋별로 규칙을 등록하고, 스케줄 또는 온디맨드로 실행하고,
> 코딩 에이전트가 파이프라인 배포 전에 dry-run으로 검증하고,
> 과거 결과를 조회하고 싶다*,
> *그래서* 직접 만든 점검 없이도
> 데이터 품질이 관찰·검증 가능하도록 한다.

`validation/conf`는 `rules` 배열을 담으며, 각 규칙은 아래 카탈로그에서 `type`을
선택한다. 앞의 다섯 타입은 DataHub
[Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec)의
assertion 타입과 1:1 매핑되며 `assertionRunEvent` aspect로 보고되어, DataSpoke가
관리하는 검증이 DataHub 네이티브 assertion UI에 그대로 노출된다.
여섯 번째(`custom`)는 DataSpoke 확장이다.

| `type` | 품질 차원 | 예시 |
|---|---|---|
| `freshness` | 신선도(Timeliness) | 최근 24시간 내 테이블이 갱신되었는가 |
| `volume` | 완전성(Completeness) | 행 수가 1,000~100,000 사이인가 |
| `field` | 정확성/유효성(Accuracy/Validity) | `email` 컬럼이 정규식과 일치하는가 |
| `schema` | 적합성(Conformance) | 필수 컬럼이 예상 타입으로 존재하는가 |
| `sql` | 커스텀 SQL | 위반 시 행을 반환하는 임의 SELECT |
| `custom` | DataSpoke 확장 | 파티션 인식 SQL과 (선택적) 과거 데이터 기반 ML 이상 탐지 (`subtype: "sql_timeseries"`로 예: 오늘의 행 수와 요일별 베이스라인 비교) |

**Conf 사전 조건.** PUT `validation/conf`는 데이터셋이 이미 DataHub에 존재하는
경우에만 허용된다 — DataHub가 모르는 URN에 규칙을 등록하면
`422 DATASET_NOT_IN_DATAHUB`을 반환한다. 인제스천(필요 시 데이터셋을 생성)과 달리
검증은 항상 DataHub가 이미 추적하는 데이터셋에 대해서만 동작하며, 이는
DataHub-as-SSOT 원칙과 정렬된다.

**Result 행 형태.** 모든 `method/run`은 `attr/result`에 **규칙당·실행당 한 행**을
남기며, 각 행은 `rule_id`, `assertion_result`(`SUCCESS` / `FAILURE` / `ERROR`),
`run_id`, 그리고 해당 규칙이 평가한 파티션을 담는다. 따라서 `attr/result` 시간
범위 조회만으로 규칙별·파티션별 이력에 답할 수 있다 — 검증을 다시 실행할 필요
없다.

**실행 시맨틱.** 동일 데이터셋의 실행은 직렬화된다: 실행 중에 `method/run`을 다시
부르면 `409 VALIDATION_RUNNING`을 반환한다. 동기 응답은
`{run_id, status, total, passed, failed, errored}`를 담아, 호출자가
`event/validation`을 폴링하지 않고도 다음 단계를 결정할 수 있게 한다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/validation/conf` | 규칙 세트 등록·읽기·갱신·삭제 (DataHub Open Assertions Spec 호환). DataHub에 없는 URN에 PUT하면 `422 DATASET_NOT_IN_DATAHUB` |
| `POST /spoke/common/data/{urn}/method/validation/run` | 수동 실행; `dry_run: true`는 Online Verifier(결과 미기록)용. 동일 데이터셋 동시 실행은 `409 VALIDATION_RUNNING`. 응답: `{run_id, status, total, passed, failed, errored}` |
| `GET /spoke/common/data/{urn}/attr/validation/result?from=…&to=…&partition=…` | 과거 결과 — 규칙당·실행당 한 행 (`rule_id`, `assertion_result`, `run_id`, 파티션) |
| `GET /spoke/common/data/{urn}/event/validation` | 데이터셋별 검증 이벤트 이력 |
| `GET /spoke/common/validation` | 설정과 최신 결과를 담은 크로스 데이터셋 리스트 |

### Imazon 예시

주문 팀이 `orders.line_items`에 네 가지 규칙을 등록한다 — 팀이 필요로 하는
각 룰 타입을 하나씩 사용한다:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,orders.line_items,PROD)/attr/validation/conf
```
```json
{
  "is_enabled": true,
  "schedule_tier": "daily",
  "rules": [
    {"rule_id": "fresh_daily", "type": "freshness", "max_age": "24h"},
    {"rule_id": "daily_volume", "type": "volume",
     "comparison": "ratio", "threshold": 0.8, "window": "7d",
     "partition": "event_date"},
    {"rule_id": "qty_positive", "type": "field", "column": "quantity",
     "condition": "between", "min": 1, "max": 100},
    {"rule_id": "qty_anomaly", "type": "custom", "subtype": "sql_timeseries",
     "value_sql": "SELECT sum(quantity) FROM orders.line_items WHERE event_date = :partition",
     "partition": "event_date",
     "ml_validation": {"model": "day_of_week", "lookback": "8w"}}
  ]
}
```

**스케줄 실행.**
일간 Airflow 검증 DAG이 네 규칙을 모두 실행하고
DataHub에 `assertionRunEvent` aspect를,
`validation_results`에 행을 기록한다.

**코딩 에이전트의 dry-run.**
신규 배송 파이프라인을 배포하는 중에 AI 코딩 에이전트가 호출한다:

```http
POST .../method/validation/run    { "dry_run": true, "partition": {"event_date": "2026-04-25"} }
```

머지 전에 어제 데이터로 규칙이 통과하는지 확인하는 용도다.

**과거 데이터 조회.**
1주일 후 분석가가 지난주 결과를 본다:

```http
GET .../attr/validation/result?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

**크로스 데이터셋 오버뷰.**
운영팀이 `GET /spoke/common/validation`에서
데이터셋별 최신 통과/실패를 본다.

---

## UC3: Ontology Generation

**MANIFESTO §2.1 기능**:
*Ontology Generation — 소스 코드, SQL 로그, 외부 문서 등을 분석해
자율적으로 온톨로지를 구축하고
graph DB와 vector DB에 유지한다.*

### User Story

> *분석가 또는 거버넌스 멤버로서*,
> *DataSpoke가 데이터셋 전반에 존재하는 비즈니스 개념과
> 그 사이의 관계를 자율적으로 추론해 주기를 원한다*,
> *그래서* 개념 단위로 데이터셋을 탐색하고,
> 제안을 승인 전에 리뷰할 수 있도록 한다.

베이스라인 온톨로지는 **단일 레벨**이다 —
개념은 동등한 peer이며 중첩되지 않는다.
관계는 개념 간 엣지로 표현되고,
멤버 데이터셋은 각 개념 아래에 나열된다.

**Conf는 싱글톤.** UC1 / UC2 / UC4의 데이터셋별 conf와 달리, 온톨로지는 글로벌
아티팩트이므로 conf는 데이터셋 URN 아래가 아니라
`/spoke/common/ontogen/attr/conf`에 위치한다. conf는 추론 DAG 실행 시점, 사용 입력
소스, 스코프 데이터셋을 제어한다.

| `attr/conf` 필드 | 용도 |
|---|---|
| `is_enabled` | 추론 DAG 마스터 스위치 |
| `schedule_tier` | `hourly` / `daily` / `weekly` 재추론 주기 |
| `sources` | 사용할 입력 소스 — 최소 `datahub_aspects`; 선택적으로 `sql_logs`, `github_repos`, `external_docs` |
| `dataset_filter` | 선택적 스코프 필터 — `tags`(DataHub 태그 URN 리스트)와 `glossary_terms`(glossary term URN 리스트); UC5의 `measurement_query.dataset_filter`와 동일 형태 |

**실행 시맨틱.** 추론 실행은 직렬화된다: 실행 중에 `method/run`을 다시 부르면
`409 ONTOGEN_RUNNING`을 반환한다. `dry_run: true`는 추론을 평가하고 변경 사항을
기록하지 않으므로, `sources`나 `dataset_filter` 변경의 효과를 적용 전에 미리 보는
데 유용하다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/ontogen/attr/conf` | 싱글톤 conf — 위 필드 표 참조 |
| `POST /spoke/common/ontogen/method/run` | 수동 재추론 트리거; `dry_run: true`는 기록 없이 평가만. 동시 실행은 `409 ONTOGEN_RUNNING` |
| `GET /spoke/common/ontogen/event` | 글로벌 추론 실행 이력(`ONTOGEN.RUN_COMPLETE`, `ONTOGEN.SOURCE_FAILED`) |
| `GET /spoke/common/ontogen` | 개념 리스트(confidence·상태 포함) |
| `GET /spoke/common/ontogen/{concept_id}` | 멤버 데이터셋과 발신 관계 포함 개념 상세 |
| `GET /spoke/common/ontogen/{concept_id}/attr` | 개념 속성(confidence, 근거) |
| `GET /spoke/common/ontogen/{concept_id}/event` | 개념 변경 이력(제안 → 승인/거부, 멤버 추가) |
| `POST /spoke/common/ontogen/{concept_id}/method/review` | 대기 중 개념 제안의 승인·거부 |

### Imazon 예시

**Conf.** 거버넌스 팀이 온톨로지 생성을 활성화한다:

```http
PUT /api/v1/spoke/common/ontogen/attr/conf
```
```json
{
  "is_enabled": true,
  "schedule_tier": "daily",
  "sources": ["datahub_aspects", "sql_logs", "github_repos"],
  "dataset_filter": {"tags": ["urn:li:tag:env:PROD"]}
}
```

**입력.** 위 conf에 따라 DataSpoke는 세 OLTP 테이블에 대한 DataHub aspect
(`schemaMetadata`, `datasetProperties`, `upstreamLineage`)와
SQL 쿼리 로그, 일부 `imazon/order-service` GitHub 저장소를 읽는다.

**추론 출력.** 두 개의 관계를 가진 세 peer 개념:

```
Concept: BOOK                       confidence 0.96   status: pending_review
  members:
    catalog.books                   (primary)

Concept: CUSTOMER                   confidence 0.94   status: pending_review
  members:
    customers.profiles              (primary)

Concept: ORDER_LINE                 confidence 0.71   status: pending_review
  members:
    orders.line_items               (primary)
  evidence:
    - 외래 키 book_id → catalog.books.book_id (스키마)
    - customers.profiles와의 조인이 order-service 쿼리의 84%에 등장 (SQL 로그)

Relationships:
  ORDER_LINE  --references-->  BOOK         (FK book_id,     confidence 0.95)
  ORDER_LINE  --placed_by-->   CUSTOMER     (FK customer_id, confidence 0.87)
```

세 개념 모두 리뷰 큐에 들어간다.
`ORDER_LINE`은 confidence가 가장 낮아(0.71, LLM이 "주문"과 "주문 항목"을 구분하는 데
모호함이 있음) 리뷰어가 우선 검토한다:

```http
GET /api/v1/spoke/common/ontogen
```

거버넌스 리뷰어가 상세와 이벤트 이력을 조회한다:

```http
GET /api/v1/spoke/common/ontogen/order_line
GET /api/v1/spoke/common/ontogen/order_line/event
```

…그리고 제안을 승인한다:

```http
POST /api/v1/spoke/common/ontogen/order_line/method/review
```
```json
{ "verdict": "approve", "reason": "FK 구조 확인. 추후 이름 변경 가능." }
```

승인 후 개념 멤버십은
멤버 데이터셋의 glossary term 연결로 DataHub에 반영된다.

---

## UC4: Metadata Generation

**MANIFESTO §2.1 기능**:
*Metadata Generation — 온톨로지를 바탕으로 데이터 문서의 상태를 점검하고
생성 AI로 메타데이터를 제안한다. API와 리뷰 프로세스를 포함한다.*

이 기능은 DataHub 메타데이터에 이미 존재하는
문서 필드의 값을 제안한다.
온톨로지 구조 자체는 제안하지 않는다 (UC3가 담당).

### User Story

> *데이터셋 오너 또는 거버넌스 리뷰어로서*,
> *DataSpoke가 문서가 부족한 데이터셋의 문서를 제안하고,
> 필드 단위로 승인·편집·거부할 수 있기를 원한다*,
> *그래서* 모든 설명을 일일이 작성하지 않고도
> 문서 커버리지가 향상되도록 한다.

**베이스라인에서 지원하는 문서 필드**

DataSpoke는 DataHub의 **편집 가능(editable)** aspect에만 기록한다.
편집 불가능한 짝(`datasetProperties.description`,
`schemaMetadata.fields[].description`)은 인제스천 커넥터가 사용하므로,
거기에 기록하면 다음 커넥터 실행이 사람의 승인 결과를 덮어쓸 수 있다.
DataHub은 두 편집 가능 설명 필드를 모두 리치 텍스트로 취급하며,
UI는 Markdown으로 렌더링한다.

| 범위 | 필드 | 형식 | DataHub 타깃 |
|---|---|---|---|
| Per-data | 테이블 설명 | Markdown | `editableDatasetProperties.description` |
| Per-data | 컬럼 설명 | Markdown | `editableSchemaMetadata.editableSchemaFieldInfo[].description` (`fieldPath` 키) |
| Cross-data | 크로스 데이터 문서 | Markdown | `dataProduct` 엔티티(`assets`에 관련 데이터셋들을 담음)의 `dataProductProperties.description`. 생성기가 생성·수정·분리·제목 변경 액션을 제안할 수 있다 — 아래 설계 결정 참조 |

향후 범위(언급만, 여기서는 모델링하지 않음):
`domains`와 `globalTags` 제안.

> *(하위 사양 후속 반영 필요)*
> `attr/metagen/conf`의 `targets` enum은
> `dataset.description`, `column.description`, `cross_data.md`의
> 세 구체 값을 가져야 하며, 각각 위 표의 편집 가능 aspect에 매핑된다.
> `cross_data.md`의 경우 result 페이로드는 단일 aspect 쓰기가 아니라
> `dataProduct`의 생성·수정·분리·제목 변경 액션 묶음을 담는다.
> `feature/BACKEND.md`와 `DATAHUB_INTEGRATION.md`에 후속 반영한다.

> *(설계 결정)* `cross_data.md` 제안은 UC3 컨셉을 키로 삼지 않는다.
> 문서 생성기는 기존 `dataProduct` 엔티티(제목과 본문)를 입력 컨텍스트로 읽고,
> 무엇을 제안할지 스스로 결정한다.
> 하나의 `cross_data.md` 제안은 **액션 묶음**이며, 각 액션은 다음 중 하나다:
> - **생성(create)** — 누락된 주제를 발견했고 기존 데이터 프로덕트는 그대로 둬도
>   괜찮다고 판단되면, 생성기가 정한 서술적 제목(주제 구문)과 본문을 가진
>   `dataProduct`를 새로 만든다.
> - **수정(modify)** — 기존 `dataProduct`의 제목·URN은 유지한 채 본문만 교체한다.
> - **분리(split)** — 기존 `dataProduct` 하나가 분리 가능한 여러 주제를 섞어 다룬다고
>   판단되면, 해당 데이터 프로덕트를 제거하고 둘 이상의 대체 데이터 프로덕트를 만든다.
> - **제목 변경(retitle)** — 기존 `dataProduct`의 제목(과 URN) 변경을 제안한다.
>   신규 생성과 함께 제안할 수도 있다.
>
> 리뷰어는 각 액션을 동일한 PATCH 메커니즘으로 개별 승인·편집·거부한다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/metagen/conf` | 타깃 필드·주기·상태 설정 |
| `POST /spoke/common/data/{urn}/method/metagen/run` | 생성 실행 트리거 |
| `GET /spoke/common/data/{urn}/attr/metagen/result?latest=true` | 데이터셋의 최신 제안 조회 |
| `PATCH /spoke/common/data/{urn}/attr/metagen/result/{result_id}` | 승인/부분 승인/거부 — body `{ "verdict": "approve"\|"reject", "fields": [...], "reason": "…" }`. 승인 시 선택된 부분이 DataHub에 기록된다. |
| `GET /spoke/common/data/{urn}/event/metagen` | 데이터셋별 생성 이벤트 이력 |
| `GET /spoke/common/metagen` | 설정과 최신 결과를 담은 크로스 데이터셋 리스트 |

### Imazon 예시

카탈로그 팀이 `catalog.books`에 문서 생성을 활성화한다:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)/attr/metagen/conf
```
```json
{
  "targets": ["dataset.description", "column.description", "cross_data.md"],
  "period": "weekly",
  "is_enabled": true
}
```

**실행.**

```http
POST .../method/metagen/run
```

**최신 제안.**

```http
GET .../attr/metagen/result?latest=true
```

반환:

```
result_id: 7e8b…
status:    pending_review

dataset.description (markdown, confidence 0.92):
  "# Books\n\nImazon이 제공하는 모든 타이틀의 마스터 카탈로그...\n## 메모\n- 기본 키: `book_id`."

column.description 제안(markdown):
  book_id   — "도서의 안정적이고 불투명한 식별자."
  title     — "고객에게 노출되는 표시 제목."
  author    — "자유 텍스트 저자/작성자명."
  isbn      — "ISBN-13 문자열; 미상이면 '0000000000000'."
  price     — "USD 정가, 소수점 두 자리."

cross_data.md actions:
  검토한 기존 dataProduct: (없음)
  제안:
    - action: create
      title:  "주문이 도서를 어떻게 참조하는가"
      body:   "`orders.line_items.book_id`는 `catalog.books.book_id`와 조인된다 ..."
      confidence: 0.81
```

**리뷰.** 리뷰어가 테이블 설명과 5개 컬럼 중 4개를 승인하고,
이어서 `author`를 편집해 승인하고, cross-data MD는 거부한다:

```http
PATCH .../attr/metagen/result/7e8b…
```
```json
{
  "verdict": "approve",
  "fields": ["dataset.description",
             "column.description.book_id",
             "column.description.title",
             "column.description.isbn",
             "column.description.price"],
  "reason": "생성된 그대로 승인."
}
```

두 번째 PATCH는 편집한 `author` 설명을 승인하고,
세 번째 PATCH는 제안된 `cross_data.md` create 액션을 사유와 함께 거부한다.
DataSpoke는 같은 호출 안에서 승인된 액션을 DataHub에 기록한다.

이후 팀은 제안 라이프사이클을 관찰한다:

```http
GET .../event/metagen
```

---

## UC5: Governance

**MANIFESTO §2.1 기능**:
*Governance — 문서 커버리지, 데이터 신선도 같은
거버넌스 메트릭을 설정·모니터링하는 API.*

### User Story

> *거버넌스 리드 또는 CDO로서*,
> *항상 켜져 있는 작은 시그널 세트 —
> 인제스천 신선도와 검증 점수 — 를 갖고,
> 한눈에 보여주는 단일 오버뷰를 원한다*,
> *그래서* 대시보드를 직접 큐레이션하지 않고도
> 건강도를 모니터링할 수 있도록 한다.

**베이스라인 메트릭**

베이스라인은 두 메트릭으로 출발하며, 조직은 같은 `attr/conf` 엔드포인트로
새로운 `measurement_query` 타입을 정의해 메트릭을 추가할 수 있다.

| 메트릭 ID | 정의 |
|---|---|
| `ingestion-freshness` | 활성화된 인제스천 설정 중 마지막 성공 `event/ingestion`이 신선도 윈도우 안에 들어오는 비율 (active는 `schedule_tier` 기준; passive는 고정 윈도우 기준). |
| `validation-score` | 최신 실행에서 `assertion_result = SUCCESS`인 검증 규칙의 비율. 규칙이 하나라도 있는 데이터셋 전반에 걸쳐 평균. |

**Result 행 형태.** 모든 측정 실행은 `attr/result`에 한 행을 남기며, 각 행은
집계 `value`와 함께 데이터셋별 `breakdown`(어떤 데이터셋이 어떤 부분 값에
기여했는지)을 담는다. 덕분에 `attr/result` 시간 범위 조회만으로 "지난주 화요일
어떤 데이터셋이 실패했는가"에 답할 수 있다 — 메트릭을 다시 실행할 필요 없다.

**실행 시맨틱.** 동일 메트릭의 실행은 직렬화된다: 실행 중에 `method/run`을 다시
부르면 `409 METRIC_RUNNING`을 반환한다. `dry_run: true`는 쿼리를 평가만 하고
`attr/result`에 기록하거나 이벤트를 발행하지 않으므로, 새로운
`measurement_query`를 스케줄에 올리기 전에 시험하기에 유용하다.

**베이스라인 오버뷰(한 개)**

단일 대시보드가 모든 활성화된 메트릭의 최신 값과 데이터셋별 분해(어느
데이터셋이 신선하지 않은지, 어느 데이터셋의 규칙이 실패하는지), 그리고
**블라인드 스팟(blind spots)** — DataHub에는 존재하지만 어떤 UC3 온톨로지
컨셉에도 매핑되지 않은 데이터셋 — 을 반환한다. 블라인드 스팟 자체가 거버넌스
시그널이다: 온톨로지가 데이터 자산을 아직 따라잡지 못한 커버리지 갭을 드러낸다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/dg/metric/{metric_id}/attr/conf` | 메트릭 정의·갱신·읽기 (제목, 테마, 쿼리, schedule_tier, 활성화 플래그) |
| `POST /spoke/dg/metric/{metric_id}/method/run` | 측정 실행 트리거; `dry_run: true`는 기록 없이 평가만. 동일 메트릭의 동시 실행은 `409 METRIC_RUNNING` |
| `GET /spoke/dg/metric/{metric_id}/attr/result?from=…&to=…` | 과거 측정의 시계열 (각 행은 집계 `value`와 데이터셋별 `breakdown`을 함께 담음) |
| `GET /spoke/dg/metric/{metric_id}/event` | 실행 완료·정의 변경 이벤트 |
| `GET /spoke/dg/metric` | 모든 메트릭 리스트 |
| `GET /spoke/dg/overview` | 모든 활성화된 메트릭 값 + 데이터셋별 분해 + 블라인드 스팟(어떤 온톨로지 컨셉에도 매핑되지 않은 데이터셋) |
| `GET/PATCH /spoke/dg/overview/attr` | 시각화 설정 읽기·갱신 |

### Imazon 예시

CDO가 두 메트릭을 등록한다:

```http
PUT /api/v1/spoke/dg/metric/ingestion-freshness/attr/conf
```
```json
{
  "title": "인제스천 신선도",
  "theme": "freshness",
  "measurement_query": {"dataset_filter": {}, "aggregation": "pct_fresh"},
  "schedule_tier": "hourly",
  "is_enabled": true
}
```

```http
PUT /api/v1/spoke/dg/metric/validation-score/attr/conf
```
```json
{
  "title": "검증 점수",
  "theme": "quality",
  "measurement_query": {"dataset_filter": {}, "aggregation": "pct_rules_passing"},
  "schedule_tier": "hourly",
  "is_enabled": true
}
```

스케줄을 기다리지 않고 CDO가 즉시 첫 실행을 트리거한다:

```http
POST /api/v1/spoke/dg/metric/ingestion-freshness/method/run
POST /api/v1/spoke/dg/metric/validation-score/method/run
```

1주일 후, 보드 보고용으로 추세를 가져온다:

```http
GET /api/v1/spoke/dg/metric/ingestion-freshness/attr/result?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
GET /api/v1/spoke/dg/metric/validation-score/attr/result?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

대시보드 뷰는 오버뷰 엔드포인트를 소비한다:

```http
GET /api/v1/spoke/dg/overview
```

…그리고 두 메트릭 값과 함께,
`catalog.books`, `orders.line_items`, `customers.profiles`,
`orders.shipments`, `orders.events`를
신선도와 검증 상태로 묶은 데이터셋별 분해를 반환한다.
또한 DataHub에는 보이지만 UC3 컨셉에 아직 매핑되지 않은 데이터셋이 있다면
블라인드 스팟으로 함께 반환한다.
