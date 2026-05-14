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
| UC1 | Ingestion Control | [Active-custom과 Passive 인제스천](#uc1-ingestion-control) |
| UC2 | Validation | [단일 슬롯, 파이프라인이 POST하는 결과, 과거 베이스라인](#uc2-validation) |
| UC3 | Ontology Generation | [Imazon 데이터셋 전반의 노드·엣지·트리플 추론](#uc3-ontology-generation) |
| UC4 | Metadata Generation | [설명·MD 문서 제안](#uc4-metadata-generation) |
| UC5 | Governance | [인제스천 신선도와 검증 점수](#uc5-governance) |

---

## UC1: Ingestion Control

**MANIFESTO §2.1 기능**:
*Ingestion Control — 데이터 인제스천의 설정·제어·관리를
한 곳에서 수행하는 편의 기능.*

### User Story

> *데이터 팀원으로서*,
> *DataSpoke가 직접 인제스트하든 외부 시스템이 인제스트하든
> 관계없이 모든 데이터셋을 등록·실행·관찰하고 싶다*,
> *그래서* 단일 DataSpoke surface가 자산 전체의
> 인제스천 설정·실행·이벤트 이력을 다루도록 한다.

지원하는 인제스천 모드는 두 가지다:

- **`active-custom`** — DataSpoke가 자체 in-house 추출기 프레임워크로
  인제스터 역할을 한다.
  Airflow tier DAG이 설정된 `schedule_tier`(`hourly` / `daily` / `weekly`)에 따라
  플랫폼별 추출기를 실행하고 결과를 DataHub로 emit한다.
  수동·dry-run 실행도 지원된다.
  DataSpoke가 구현해 둔 플랫폼(현재 `postgres`, `kafka`)에 한정된다.
  실행마다 표준 스키마 aspect와 함께 **`DataProcessInstance`를 emit하며**,
  이것이 `event/ingestion`의 근거가 된다.
- **`passive`** — DataSpoke가 추출기를 실행하지 않고,
  실행을 일으키기 위한 어떠한 프로그램적 조작도 하지 않는다.
  사용자가 원하는 방식으로 추출을 설정한다:
  DataHub Managed Ingestion(UI 또는 GraphQL)에서 레시피를 구성하거나,
  `acryl-datahub` SDK로 일회성 Python 스크립트를 실행하거나,
  외부 파이프라인에 연결한다.
  DataSpoke는 URN을 등록하고 시간별 `ingestion-passive-hourly` DAG으로 관찰만 한다.
  이 DAG은 DataHub의 `DataProcessInstance` 레코드를 폴링해
  실행마다 한 행씩 `event/ingestion`에 기록한다.
  외부 인제스터가 무엇이든 **실행마다 `DataProcessInstance`를 emit해야**
  DataSpoke 이벤트에 노출된다
  ([DATAHUB_INTEGRATION §Custom Ingestor Guide](DATAHUB_INTEGRATION.md#custom-ingestor-guide)).

DPI emission 계약은 두 모드에 동일하게 적용된다.
DataSpoke의 active-custom 추출기도 외부 passive 인제스터와 똑같이 DPI를 emit하므로,
누가 실행했는지와 무관하게 관찰 동작은 일관된다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/ingestion/conf` | 인제스천 설정 등록·읽기·갱신·삭제 (`mode`, `platform`, `identifier`; `active-custom`에 한해 `locator`/`auth`/`schedule_tier` 추가) |
| `POST /spoke/common/data/{urn}/method/ingestion/run` | 수동 실행 (`dry_run: true`로 연결 점검) — **`active-custom` 설정에서만**. passive 설정은 `409 INGESTION_NOT_APPLICABLE`을 반환한다 |
| `GET /spoke/common/data/{urn}/event/ingestion` | 데이터셋별 인제스천 이벤트 이력 (active-custom: DataSpoke 실행이 기록; passive: DataHub의 DataProcessInstance 레코드를 시간별 폴링이 기록) |
| `GET /spoke/common/ingestion` | 데이터셋별 `attr/ingestion/*`을 집계하는 크로스 데이터셋 리스트 뷰 |

### Imazon 예시

#### Case 1 — Active-custom, Postgres `catalog.title_master` (daily)

DataSpoke가 추출을 소유한다.
Airflow `ingestion-active-daily` DAG이 매일 in-house Postgres 추출기를 호출하고,
수동 실행도 가능하다.

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.title_master,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "active-custom",
  "platform": "postgres",
  "locator": {"host": "pg-oltp.imazon.internal", "port": 5432},
  "identifier": {"database": "imazon", "schema_name": "catalog", "table": "title_master"},
  "auth": {"username": "spoke_reader", "secret_ref": {"name": "dataspoke-source-cred-title-master", "key": "password"}},
  "is_enabled": true,
  "schedule_tier": "daily"
}
```

스케줄을 켜기 전에 코딩 에이전트가 연결을 검증한다:

```http
POST .../method/ingestion/run    { "dry_run": true }
```

`is_enabled=false` 동안 `method/ingestion/run`을 호출할 수 있는 유일한 방법은 dry-run이다.
non-dry-run은 `409 INGESTION_DISABLED`를 반환한다.

일간 Airflow tier DAG 실행 후, 팀이 데이터셋별 이벤트 이력을 조회한다:

```http
GET .../event/ingestion?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

각 행은 실행 중에 DataSpoke 추출기가 DataHub에 emit한 `DataProcessInstance` aspect에 기반한다.
즉, 동일한 레코드가 DataHub UI에서도 보인다.

#### Case 2 — Passive, Postgres `catalog.reviews` (DataHub Managed Ingestion 경유)

팀이 컬럼 단위 lineage와 프로파일 통계를 원한다.
DataSpoke의 in-house 추출기는 이를 생성하지 않는다.
DataHub Managed Ingestion을 직접 설정한다:
**`http://datahub.<domain>/ingestion`**에서
`catalog.reviews` 대상의 postgres 레시피를 daily cron으로 만들고,
DataHub의 executor가 실행하도록 둔다.
DataSpoke는 이 설정에 손을 대지 않는다.

데이터셋을 DataSpoke surface에 노출하고 이벤트 이력을 받기 위해 passive로 등록한다:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.reviews,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "postgres",
  "identifier": {"database": "imazon", "schema_name": "catalog", "table": "reviews"},
  "is_enabled": true
}
```

`locator`, `auth`, `schedule_tier`는 없다 — 외부 인제스터의 영역이다.
이 URN에 대한 `POST .../method/ingestion/run`은 `409 INGESTION_NOT_APPLICABLE`을 반환한다.

DataHub의 executor가 실행을 마칠 때마다 데이터셋에 대한 `DataProcessInstance`를 기록한다.
시간별 `ingestion-passive-hourly` DAG이 이를 픽업한다:

```http
GET .../event/ingestion?from=…&to=…
```
```json
{
  "events": [
    {
      "event_type": "INGESTION.COMPLETE",
      "status": "success",
      "occurred_at": "2026-04-25T03:14:00Z",
      "detail": {"source": "passive", "datahub_status": "SUCCEEDED", "run_id": "..."}
    }
  ]
}
```

#### Case 3 — Passive, Kafka `imazon.orders.events` (커스텀 일회성 스크립트)

Imazon이 일회성 맥락에서 Kafka 토픽 메타데이터를 적재해야 한다:
개발자가 `acryl-datahub` SDK를 사용하는 Python 스크립트를 실행한다.
이 스크립트는 호출마다 Status, SchemaMetadata, `DataProcessInstance`를 emit한다.
스크립트는 DataSpoke 외부에 있고 스케줄링되지 않는다.

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:kafka,imazon.orders.events,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "kafka",
  "identifier": {"topic": "orders.events", "cluster": "PROD"},
  "is_enabled": true
}
```

스크립트가 실행되어 DPI를 emit하면, 다음 시간별 폴링에서 Case 2와 동일한 형태로
`event/ingestion`에 한 행이 노출된다.
**스크립트가 DPI를 emit하지 않으면** 해당 URN의 이벤트 리스트는 비어 있게 된다.
데이터셋은 여전히 `GET /spoke/common/ingestion`에 노출되고,
스키마는 여전히 DataHub에 있으며,
[`ingestion-freshness` 메트릭](#uc5-governance)도 DataHub 타임스탬프로 추적한다.
다만 `event/ingestion`을 통한 실행 단위 드릴다운은 불가능해진다.
스크립트 작성자가 따라야 할 DPI emission 계약은
[DATAHUB_INTEGRATION §Custom Ingestor Guide](DATAHUB_INTEGRATION.md#custom-ingestor-guide)에 정의되어 있다.
DataSpoke의 active-custom 추출기도 동일한 계약을 따른다.

#### 크로스 데이터셋 오버뷰

```http
GET /api/v1/spoke/common/ingestion?limit=100
```

데이터셋별로 한 행을 반환한다.
각 행은 `attr/ingestion/*` 집합(모드, 적용되는 경우 스케줄, 마지막 이벤트 상태)을 담는다.
대시보드와 일괄 감사에 유용하다.

### 범위 노트

DataSpoke 인제스천의 책임은 **소스 연결, 스키마 디스커버리, 신선도 신호**다.
프로파일링·컬럼 단위 lineage·사용량 분석은 in-house `active-custom` 경로의 범위 밖이다.
이를 필요로 하는 팀은 DataHub Managed Ingestion을 직접 설정하고,
해당 데이터셋을 DataSpoke에 `mode: passive`로 등록한다.
이로써 DataSpoke 추출기 surface를 작게 유지하고,
"DataSpoke는 control surface, DataHub는 메타데이터의 SSOT" 원칙과 일관성을 지킨다.

---

## UC2: Validation

**MANIFESTO §2.1 기능**:
*Validation — 데이터셋당 한 개의 검증 슬롯(설명 + 변수 이름 목록)과,
파이프라인이 산출한 시계열 결과의 적재. 검증 로직은 데이터 파이프라인이 수행하고,
DataSpoke는 설정과 결과 시계열을 저장하며 DataHub assertion aspect를 emit하고
과거 결과를 베이스라인 캐시로 제공한다.*

### User Story

> *데이터 팀원으로서*,
> *데이터셋당 한 개의 검증 규칙(자유 형식 설명과 파이프라인이 보고할 변수 이름
> 목록)을 구성하고, 파티션 작성 후 파이프라인이 결과를 DataSpoke로 POST하고,
> 과거 결과를 베이스라인으로 조회하고 싶다*,
> *그래서* 데이터 품질 결과가 한 곳에 모이고 DataHub에서 보이도록 하면서도,
> DataSpoke가 운영용 자격증명을 가질 필요가 없도록 한다.

`validation/conf`는 자유 형식 `description`과 변수 이름 목록(`variables`)으로 이루어진
작은 고정 문서다. 설정에는 **규칙 로직이 없다**. 데이터 파이프라인이 점검을 수행하고,
`score`(0..1)와 명명된 변수 값을 계산해 POST한다. DataSpoke는 결과를 저장하고
DataHub에 `assertionRunEvent`를 emit하며, 과거 시계열을 조회 가능하게 제공한다.

데이터셋당 여러 개의 점검(별도의 freshness/volume/field assertion, 컬럼 단위 검증,
다중 팀 소유 등)이 필요한 팀은 **DataHub의 native assertion API**를 직접 사용한다 —
DataSpoke는 80% 케이스를 위한 의견 있는 단일 슬롯 단축 경로일 뿐, 유일한 경로가 아니다.
전체 계약은 [`spec/feature/VALIDATION.md`](feature/VALIDATION.md) 참조.

**Conf 사전 조건.** PUT `validation/conf`는 데이터셋이 이미 DataHub에 존재해야 한다 —
DataHub가 모르는 URN에 슬롯을 구성하면 `422 DATASET_NOT_IN_DATAHUB`을 반환한다.
인제스천(필요 시 데이터셋을 생성)과 달리, 검증은 항상 DataHub가 이미 추적하는
데이터셋에 대해서만 동작한다.

**Result 행 형태.** 파이프라인의 각 `POST .../attr/validation/result`는
`data_time`(보통 파티션 타임스탬프)을 키로 한 행을 기록하며 `score`와 명명된 변수
맵을 담는다. 같은 `data_time`에 대한 다중 POST는 **append-only**이다 — 각 POST가
DataHub의 별도 `assertionRunEvent` 행이 되며, GET 엔드포인트는 동일 `data_time`별로
가장 최근 결과(last-write-wins)를 반환한다.

**Soft-delete + 부활.** `DELETE .../attr/validation/conf`는 assertion URN에
`status.removed = true`를 emit한다. 이후 `PUT`은 동일 결정적 URN을 부활시키며
(`removed`를 해제하고 `assertionInfo`를 덮어쓴다).

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `GET/PUT/PATCH/DELETE /spoke/common/data/{urn}/attr/validation/conf` | 검증 슬롯의 읽기 / 생성·교체 / 부분 갱신 / soft-delete (`description` + `variables`). DataHub에 없는 URN에 PUT하면 `422 DATASET_NOT_IN_DATAHUB` |
| `POST /spoke/common/data/{urn}/attr/validation/result` | 결과 `{data_time, score, variables}`를 추가. 미선언 변수 키는 `422 UNKNOWN_VARIABLE`; `score`가 `[0,1]` 범위를 벗어나면 `422 INVALID_SCORE` |
| `GET /spoke/common/data/{urn}/attr/validation/result?from=…&until=…&limit=…` | `data_time`을 기준으로 한 과거 결과 (RFC 3339, `from` 포함, `until` 미포함). 기본 `limit=1000`, 서버 상한 `10000` |
| `GET /spoke/common/data/{urn}/event/validation` | 데이터셋별 검증 이벤트 이력 |
| `GET /spoke/common/validation` | 설정(설명 + 변수 이름 목록)과 최신 결과(data_time, score)를 담은 크로스 데이터셋 리스트 |

### Imazon 예시

주문 팀이 `orders.line_items`에 검증 슬롯을 한 개 구성하고, 일간 품질 태스크가
보고할 변수 이름을 선언한다:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,orders.line_items,PROD)/attr/validation/conf
```
```json
{
  "description": "일간 fitness check: 행 수, 수량 sanity, 핵심 컬럼 null 수",
  "variables": ["row_cnt", "qty_negative_cnt", "qty_total", "user_id_null_cnt"]
}
```

**파이프라인이 emit하는 결과.** 일간 파티션을 작성하는 동일 Airflow DAG이 직후
팀의 품질 태스크를 실행해 네 변수를 계산하고 POST한다:

```http
POST .../attr/validation/result
```
```json
{
  "data_time": "2026-05-08T00:00:00Z",
  "score": 1.0,
  "variables": {
    "row_cnt": 12480.0,
    "qty_negative_cnt": 0.0,
    "qty_total": 38712.0,
    "user_id_null_cnt": 0.0
  }
}
```

결과는 DataHub Quality 탭에 `data_time`을 기준으로 timestamp된
`assertionRunEvent`로 노출된다. `score < 1.0`이면 DataHub UI에서 assertion이
`FAILURE`로 표시된다. 원본 score는 partial-success 시맨틱을 위해
`actualAggValue`에 보존된다.

**과거 데이터 베이스라인 캐시.** 다음 날의 품질 태스크는 14일 롤링 베이스라인 대비
오늘의 행 수 anomaly를 계산한다. `orders.line_items`를 다시 집계하는 대신 다음을
호출해 과거 `row_cnt` 시계열을 그대로 사용한다:

```http
GET .../attr/validation/result?from=2026-04-24T00:00:00Z&until=2026-05-08T00:00:00Z
```

**크로스 데이터셋 오버뷰.** 운영팀이 `GET /spoke/common/validation`에서
데이터셋별 description, 변수 개수, 최신 score를 본다.

---

## UC3: Ontology Generation

**MANIFESTO §2.1 기능**:
*Ontology Generation — DataHub에 등재된 메타데이터를 바탕으로
자율적으로 온톨로지를 구축하고 DataSpoke 내부 graph DB와 vector DB에 유지한다.*

### User Story

> *분석가 또는 거버넌스 멤버로서*,
> *DataSpoke가 데이터셋 전반에 존재하는 비즈니스 개념(주어·목적어),
> 관계 유형(술어), 그리고 그것들을 잇는 구체적인 사실(트리플)을
> 자율적으로 추론해 주기를 원한다*,
> *그래서* 개념 단위로 데이터셋을 탐색하고,
> 의미 있는 관계를 둘러보고,
> 각 레이어를 승인 전에 리뷰할 수 있도록 한다.

베이스라인 온톨로지는 **주어 / 술어 / 목적어 트리플 모델**을 따르며,
서로 독립적으로 리뷰되는 세 결과 유형을 가진다.

- **Node** — *주어* 또는 *목적어*: 한 개 이상의 데이터셋에 뿌리를 둔
  비즈니스 개념(예: `BOOK`, `CUSTOMER`).
- **Edge** — *술어*: 관계 유형(예: `references`, `placed_by`).
- **Triple** — `(subject_node, edge, object_node)` 사실. 트리플은 사전에 승인된
  노드와 엣지로만 구성되므로, 개념 어휘를 한 번 승인해 여러 사실에서 재사용한다.

노드와 엣지 ID는 slug(`book`, `placed_by`)이며, 노드·엣지 slug에는 `__`를 포함할
수 없다(트리플 ID 구분자로 예약). 트리플 ID는
`subject_node_id__edge_id__object_node_id` 형태의 결합 slug
(예: `order_line__references__book`)로, ID 자체가 사실을 인코딩하므로 재추론 사이에
자연히 idempotent하다.

**Conf는 싱글톤.** UC1 / UC2 / UC4의 데이터셋별 conf와 달리, 온톨로지는 글로벌
아티팩트이다. `/spoke/common/ontogen/attr/conf`의 운영 conf는 추론 DAG 실행 시점과
스코프 데이터셋을 제어한다.

**입력(검증된 DataHub 경계).** UC3는 UC4와 동일한 DataHub aspect 집합 —
`datasetProperties`, `schemaMetadata`, `editableDatasetProperties`,
`editableSchemaMetadata`, `glossaryTerms`, 그리고 스코프 데이터셋을
`relatedAssets`로 참조하는 `document` 엔티티의
`documentInfo.contents.text`(관례상 Markdown 본문) — 만 입력으로 사용한다.
DataSpoke는 UC4 리뷰어가 제안을 승인한 뒤에만 해당 editable aspect를
DataHub에 쓰므로, DataHub에 *존재*한다는 사실 자체가 승인 신호다 — UC3는
별도 조인이 필요 없고, UC4의 *초안* 상태(`pending` / `edited`)는 DataHub에
기록되지 않으므로 LLM이 다른 LLM의 미승인 추측을 학습할 수 없다.

| `attr/conf` 필드 | 용도 |
|---|---|
| `is_enabled` | 추론 DAG 마스터 스위치 |
| `schedule_tier` | `hourly` / `daily` / `weekly` 재추론 주기 |
| `dataset_filter` | 선택적 스코프 필터 — `tags`(DataHub 태그 URN 리스트), `glossary_terms`(glossary term URN 리스트), `dataset_urns`(고정된 데이터셋 집합을 지정하는 명시적 `urn:li:dataset:(…)` URN 리스트). 세 차원은 OR로 합쳐지며, 어느 한 차원의 빈 배열은 기여하지 않고, `{}`는 모든 데이터셋을 의미한다. URN 포맷은 PUT/PATCH 시점에 검증하고, 실행 시점에 DataHub에서 해석되지 않는 항목은 skip하며 run-complete 이벤트의 `unresolved_urns`에 보고한다. UC5의 `measurement_query.dataset_filter`와 동일 형태 |
| `default_run_prompt` | 본문 없이 호출되는 실행(주기적 Airflow 실행, 본문 없는 수동 `POST /method/run`)의 일회성 프롬프트로 사용되는 선택적 Markdown 문자열. null이면 기본값 비활성 |

**Seed가 추론을 안내한다.** seed는 데이터 소스와 함께 추론 실행이 소비하는
사람이 작성한 **Markdown 문서**(프롬프트, 도메인 힌트, 명명 규칙)이다.
seed 본문(요청·응답 모두)은 원시 Markdown(`Content-Type: text/markdown`)이며,
`seed_id`와 타임스탬프만 별도로 관리된다.
여러 seed가 공존할 수 있다. POST는 생성(서버가 `seed_id` 부여),
PATCH는 문서 교체, DELETE는 폐기한다.

**실행 시맨틱.** 추론 실행은 직렬화된다: 실행 중에 `method/run`을 다시 부르면
`409 ONTOGEN_RUNNING`을 반환한다. `?dry_run=true`는 추론을 평가하고 노드 / 엣지 /
트리플 결과를 기록 없이 반환하므로, `seed`·`dataset_filter` 변경의 효과를 적용
전에 미리 보는 데 유용하다.

**증분 추론.** 각 실행은 기존의 재사용 가능한 온톨로지를 기반으로 시작한다 —
LLM이 매번 온톨로지를 처음부터 다시 도출하지 않는다.
새 제안은 그 위에 쌓인다: 후보가 이름 또는 임베딩 유사도(`node_embeddings`)로
기존 노드와 일치하면 기존 노드 ID를 재사용한다. 재사용 풀은
`rejected`가 아닌 모든 상태(`llm_pending`, `llm_approved`, `approved`)를 포함하므로,
사람 리뷰를 기다리는 동안 같은 개념이 중복 행으로 분기되지 않는다.
일치가 없으면 새 `llm_pending` 노드로 제안한다.
엣지와 트리플도 동일한 재사용 규칙을 따른다.
`rejected` 결과는 입력으로 이월되지 않는다.

**일회성 실행 프롬프트.** `POST /method/run`은 Markdown 본문(`Content-Type: text/markdown`)을
실을 수 있으며, 영속 seed 위에 해당 실행에만 적용되는 일회성 프롬프트로 작동한다.
저장되지 않는다. seed로 굳히지 않은 채 "이번 한 번만 조정해 본다"는 실험에 쓴다.

**기본 일회성 프롬프트.** 본문을 직접 싣지 않는 실행 — 주기적 Airflow 실행과 본문이
빈 수동 `POST /method/run` — 은 `attr/conf.default_run_prompt`(Markdown)로 폴백한다.
"매 스케줄 실행을 어떻게 조정할지"의 지침은 여기에 적는다.
수동 실행에서 본문을 명시하면 기본값을 덮어쓰며, 빈 본문은 항상 기본값을 사용한다.

**리뷰 의존성.** 트리플을 사람이 승인하려면 양쪽 끝 노드와 엣지가 모두
`status='approved'`여야 한다(`llm_approved` 의존성은 게이트를 충족하지 않는다 —
각 구성 요소를 사람이 명시적으로 먼저 승인해야 한다).
이를 어기고 시도하면 `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`을 반환한다.
따라서 리뷰어는 일반적으로 **노드 → 엣지 → 트리플** 순서로 처리한다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/ontogen/attr/conf` | 싱글톤 운영 conf — 위 필드 표 참조 |
| `GET /spoke/common/ontogen/attr/seed` | seed 리스트 — `[{seed_id, updated_at, preview}]` (Markdown 본문은 아래 항목으로 개별 조회) |
| `POST /spoke/common/ontogen/attr/seed` | 추론 seed 생성 — 본문은 원시 Markdown(`Content-Type: text/markdown`); 서버가 `seed_id` 부여 |
| `GET/PATCH/DELETE /spoke/common/ontogen/attr/seed/{seed_id}` | seed 조회·보강·폐기 |
| `POST /spoke/common/ontogen/method/run` | 수동 재추론 트리거. 선택적 `Content-Type: text/markdown` 본문은 해당 실행에만 적용되는 일회성 프롬프트로 작동; `?dry_run=true`는 기록 없이 평가만. 동시 실행은 `409 ONTOGEN_RUNNING` |
| `GET /spoke/common/ontogen/event` | 글로벌 추론 실행 이력(`ONTOGEN.RUN_COMPLETE`, `ONTOGEN.RUN_FAILED`) |
| `GET /spoke/common/ontogen/result/node` | 노드(주어 / 목적어) 리스트(confidence·상태 포함) |
| `GET /spoke/common/ontogen/result/node/{node_id}` | 멤버 데이터셋 포함 노드 상세 |
| `GET /spoke/common/ontogen/result/node/{node_id}/attr` | 노드 속성(confidence, 근거) |
| `GET /spoke/common/ontogen/result/node/{node_id}/event` | 노드 변경 이력(제안 → 승인/거부, 멤버 추가) |
| `POST /spoke/common/ontogen/result/node/{node_id}/method/review` | 대기 중 노드 제안의 승인·거부 |
| `GET /spoke/common/ontogen/result/edge` | 엣지(술어) 리스트(confidence·상태 포함) |
| `GET /spoke/common/ontogen/result/edge/{edge_id}` | 엣지 상세 |
| `GET /spoke/common/ontogen/result/edge/{edge_id}/attr` | 엣지 속성(confidence, 근거) |
| `GET /spoke/common/ontogen/result/edge/{edge_id}/event` | 엣지 변경 이력 |
| `POST /spoke/common/ontogen/result/edge/{edge_id}/method/review` | 대기 중 엣지 제안의 승인·거부 |
| `GET /spoke/common/ontogen/result/triple` | 트리플 — `(subject_node_id, edge_id, object_node_id)` 사실 — 리스트(confidence·상태 포함) |
| `GET /spoke/common/ontogen/result/triple/{triple_id}` | 해석된 주어 노드·엣지·목적어 노드 포함 트리플 상세 |
| `GET /spoke/common/ontogen/result/triple/{triple_id}/attr` | 트리플 속성(confidence, 근거) |
| `GET /spoke/common/ontogen/result/triple/{triple_id}/event` | 트리플 변경 이력 |
| `POST /spoke/common/ontogen/result/triple/{triple_id}/method/review` | 대기 중 트리플 승인·거부 — 주어 노드·엣지·목적어 노드 중 하나라도 미승인이면 `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING` |

### Imazon 예시

**Conf.** 거버넌스 팀이 온톨로지 생성을 활성화한다:

```http
PUT /api/v1/spoke/common/ontogen/attr/conf
```
```json
{
  "is_enabled": true,
  "schedule_tier": "daily",
  "dataset_filter": {"tags": ["urn:li:tag:env:PROD"]}
}
```

**Seed.** LLM이 서점 도메인 친화적인 이름을 쓰도록 도메인 seed(Markdown)를 등록한다:

```http
POST /api/v1/spoke/common/ontogen/attr/seed
Content-Type: text/markdown
```
```markdown
# Imazon 서점 도메인

Imazon은 온라인 서점이다. *order*를 헤더 개념으로, *order line*을 권당 행으로 다룬다.
테이블명보다 비즈니스 친화적인 명명을 선호한다.
```

**입력.** 위 conf에 따라 DataSpoke는 세 OLTP 테이블에 대해 다음 DataHub
aspect 만 읽는다: `datasetProperties`, `schemaMetadata`,
`editableDatasetProperties`, `editableSchemaMetadata`, `glossaryTerms`,
그리고 스코프 데이터셋을 `relatedAssets`로 참조하는 `document` 엔티티의
`documentInfo.contents.text`(Markdown 본문). seed가 명명 선택을 안내한다.

**추론 출력.** 노드 셋, 엣지 둘, 트리플 둘. 각 행의 `status`는 Adversarial Debate가
신뢰도 ≥ `ONTOLOGY_CONFIDENCE_THRESHOLD`로 수락하면 `llm_approved`, 그렇지 않으면
`llm_pending`이다:

```
Nodes (subjects / objects):
  BOOK         confidence 0.96   member: catalog.books         (primary)
  CUSTOMER     confidence 0.94   member: customers.profiles    (primary)
  ORDER_LINE   confidence 0.71   member: orders.line_items     (primary)
    evidence:
      - 외래 키 book_id → catalog.books.book_id (schemaMetadata)
      - 컬럼 단위 외래 키 customer_id → customers.profiles.customer_id (schemaMetadata)

Edges (predicates):
  references   confidence 0.95   semantics: foreign-key reference
  placed_by    confidence 0.87   semantics: agent / actor

Triples (subject — predicate — object):
  ORDER_LINE  --references--> BOOK       confidence 0.95
  ORDER_LINE  --placed_by --> CUSTOMER   confidence 0.87
```

**리뷰 흐름 — 노드 먼저.** `ORDER_LINE`은 노드 confidence가 가장 낮아(0.71, LLM이
"주문"과 "주문 항목"을 구분하는 데 모호함이 있음) 리뷰어가 노드부터 시작한다:

```http
GET /api/v1/spoke/common/ontogen/result/node
GET /api/v1/spoke/common/ontogen/result/node/order_line
GET /api/v1/spoke/common/ontogen/result/node/order_line/event
POST /api/v1/spoke/common/ontogen/result/node/order_line/method/review
```
```json
{ "verdict": "approve", "reason": "FK 구조 확인. 추후 이름 변경 가능." }
```

**다음은 엣지.** 노드가 승인되면 엣지로 이동한다:

```http
GET /api/v1/spoke/common/ontogen/result/edge
POST /api/v1/spoke/common/ontogen/result/edge/references/method/review
POST /api/v1/spoke/common/ontogen/result/edge/placed_by/method/review
```

**마지막으로 트리플.** 트리플의 양쪽 노드와 엣지가 모두 승인되면 해당 트리플이
리뷰 가능 상태가 된다:

```http
GET /api/v1/spoke/common/ontogen/result/triple
POST /api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review
```

승인은 DataSpoke 내부 상태를 갱신한다. 온톨로지 그래프는 DataSpoke의
PostgreSQL(관계형 + pgvector)에서 유지한다.

`is_enabled=false`이면 non-dry-run `method/run` 호출은 `409 ONTOGEN_DISABLED`를 반환한다. Dry-run(`?dry_run=true`)은 `is_enabled`와 관계없이 항상 허용된다. Dry-run도 실제 실행과 동일하게 이벤트 detail에 `dry_run: true`를 담아 `ONTOGEN.RUN_COMPLETE`를 기록한다.

---

## UC4: Metadata Generation

**MANIFESTO §2.1 기능**:
*Metadata Generation — 온톨로지를 바탕으로 데이터 문서의 상태를 점검하고
생성 AI로 메타데이터를 제안한다. API와 리뷰 프로세스를 포함한다.*

이 기능은 DataHub 메타데이터에 이미 존재하는
문서 필드의 값을 제안한다.
온톨로지 구조 자체는 제안하지 않는다 (UC3가 담당).

**입력(검증된 DataHub 경계).** UC4는 UC3와 동일한 DataHub aspect 집합을
입력으로 사용한다: `datasetProperties`, `schemaMetadata`,
`editableDatasetProperties`, `editableSchemaMetadata`, `glossaryTerms`,
그리고 스코프 데이터셋을 `relatedAssets`로 참조하는 `document` 엔티티의
`documentInfo.contents.text`. 그 위에 UC3에서 승인된 노드와 트리플
(`dataset_node_map.status='approved'` 필터)을 DataSpoke 저장소에서 추가로
읽는다.

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
| Cross-data | 크로스 데이터 문서 | Markdown | `relatedAssets`에 관련 데이터셋 URN들을 담은 `document` 엔티티의 `documentInfo.contents.text`. 생성기가 생성·수정·삭제 액션을 제안할 수 있다 — 아래 설계 결정 참조 |

향후 범위(언급만, 여기서는 모델링하지 않음):
`domains`와 `globalTags` 제안.

> *(설계 결정)* `cross_data.md` 제안은 UC3 노드를 키로 삼지 않는다.
> 문서 생성기는 스코프 데이터셋을 `relatedAssets`로 참조하는 기존 `document`
> 엔티티(제목과 본문)를 입력 컨텍스트로 읽고, 무엇을 제안할지 스스로 결정한다.
> 하나의 `cross_data.md` 제안은 **액션 묶음**이며, 각 액션은 다음 중 하나다:
> - **생성(create)** — 누락된 주제를 발견했고 기존 문서들은 그대로 둬도 괜찮다고
>   판단되면, 생성기가 정한 서술적 제목(주제 구문)과 Markdown 본문
>   (`documentInfo.contents.text`), 그리고 주제가 걸쳐 있는 데이터셋 URN들을 담은
>   `relatedAssets`를 가진 `document`를 새로 만든다.
>   `documentInfo.source.sourceType = NATIVE`.
> - **수정(modify)** — 기존 `document`의 제목·URN은 유지한 채
>   `documentInfo.contents.text`를 교체한다. 주제가 더 많은 데이터셋을 다루게 되면
>   `relatedAssets`를 확장할 수 있다.
> - **삭제(delete)** — 주제가 새 대체 문서로 완전히 흡수된 경우, 기존 `document`를
>   `Status.removed = true`로 소프트 삭제한다. 하드 삭제는 하지 않는다.
>
> 각 액션은 result 페이로드 안에 안정적인 `action_id`를 가진다. 리뷰어는 각 액션을
> 필드별 제안과 동일한 PATCH 메커니즘으로 개별 승인·편집·거부하며, `fields` 배열은
> 액션을 `cross_data.md.<action_id>` 형식으로 참조한다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/metagen/conf` | 타깃 필드·schedule_tier·상태 설정 |
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
  "schedule_tier": "weekly",
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
  검토한 기존 document: (없음)
  제안:
    - action_id: a1
      action:    create
      title:     "주문이 도서를 어떻게 참조하는가"
      body:      "`orders.line_items.book_id`는 `catalog.books.book_id`와 조인된다 ..."
      related_assets:
        - urn:li:dataset:(urn:li:dataPlatform:postgres,orders.line_items,PROD)
        - urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)
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
세 번째 PATCH는 제안된 `cross_data.md` create 액션을
`{"verdict": "reject", "fields": ["cross_data.md.a1"], "reason": "..."}`로 거부한다.
DataSpoke는 같은 호출 안에서 승인된 액션을 DataHub에 기록한다.

이후 팀은 제안 라이프사이클을 관찰한다:

```http
GET .../event/metagen
```

`is_enabled=false`이면 non-dry-run `method/metagen/run` 호출은 `409 GENERATION_DISABLED`를 반환한다. Dry-run은 `is_enabled`와 관계없이 항상 허용된다. Dry-run도 실제 실행과 동일하게 이벤트 detail에 `dry_run: true`를 담아 `METAGEN.COMPLETE`를 기록한다.

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
| `validation-score` | 최신 `attr/validation/result` 행의 `score == 1.0`인 데이터셋 비율. 검증 conf가 있는 데이터셋만 분모에 포함. |

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
| `GET /spoke/dg/overview` | 모든 활성화된 메트릭 값 + 데이터셋별 분해 + 블라인드 스팟(어떤 온톨로지 노드에도 매핑되지 않은 데이터셋) |
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
  "measurement_query": {"dataset_filter": {}, "aggregation": "pct_datasets_passing"},
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
또한 DataHub에는 보이지만 UC3 노드에 아직 매핑되지 않은 데이터셋이 있다면
블라인드 스팟으로 함께 반환한다.
