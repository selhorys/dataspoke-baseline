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

- **PostgreSQL OLTP** (database `example_db`, fabric `DEV`)
  - `catalog.title_master` — 도서 마스터, 타이틀 한 행 (ISBN PK)
  - `catalog.editions` — 포맷별 에디션, ISBN으로 `title_master`와 조인
  - `customers.eu_profiles` — EU 고객 계정 (GDPR PII)
  - `reviews.user_ratings` — 고객과 에디션을 잇는 평점
  - `orders.daily_fulfillment_summary` — 일간 주문 이행 품질 집계
  - `shipping.carrier_status` — `order_id` 키의 배송사 스캔 이벤트
- **Kafka 토픽** (cluster `example_kafka`)
  - `imazon.orders.events` — 주문 서비스가 발행하는 주문 상태 변경 이벤트
  - `imazon.shipping.updates` — 외부 배송 서비스가 발행하는 배송 이벤트

일부는 DataSpoke가 DataHub로 인제스트하고,
일부는 Imazon이 이미 운영하는 외부 파이프라인이 인제스트한다.
DataSpoke는 두 모드를 모두 지원한다.

**기능 매핑**

| # | MANIFESTO 기능 | 유스케이스 |
|---|---|---|
| UC1 | Ingestion Control | [Active-custom과 Passive 인제스천](#uc1-ingestion-control) |
| UC2 | Validation | [단일 슬롯, 파이프라인이 POST하는 결과, 과거 베이스라인](#uc2-validation) |
| UC3 | Ontology Generation | [Imazon 데이터셋 전반의 노드·엣지·트리플 추론](#uc3-ontology-generation) |
| UC4 | Metadata Generation | [아이템별 설명 제안](#uc4-metadata-generation) |
| UC5 | Governance | [액티브 메트릭 — 신선도, 검증 점수, 문서 상태](#uc5-governance) |

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

- **`active-custom`** — DataSpoke가 인제스터 역할을 한다.
  Airflow tier DAG이 설정된 `schedule_tier`(`hourly` / `daily` / `weekly`)에 따라
  in-house 추출기를 실행하고 결과를 DataHub로 emit한다.
  수동 실행과 dry-run 실행도 지원된다.
- **`passive`** — DataSpoke가 추출기를 실행하지 않는다.
  사용자가 외부 경로(DataHub Managed Ingestion, 일회성 `acryl-datahub` SDK 스크립트,
  기존 파이프라인 등)로 추출을 구성한다.
  DataSpoke는 URN을 등록하고 DataHub를 통해 실행 단위 상태를 관찰한다.

지원하는 `active-custom` 플랫폼, 외부 인제스터가 실행 단위 관찰을 위해 충족해야 하는
DataProcessInstance emission 계약, 그리고 DataSpoke의 시간별 passive 관찰 파이프라인은
[`BACKEND.md §Ingestion Service`](feature/BACKEND.md#ingestion-service-srcbackendingestion)와
[`DATAHUB_INTEGRATION.md §Custom Ingestor Guide`](DATAHUB_INTEGRATION.md#custom-ingestor-guide)에
정의되어 있다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/ingestion/conf` | 인제스천 설정 등록·읽기·갱신·삭제 (`mode`, `platform`, `identifier`; `active-custom`에 한해 `locator`/`auth`/`schedule_tier` 추가) |
| `POST /spoke/common/data/{urn}/method/ingestion/run` | 수동 실행 (`dry_run: true`로 연결 점검) — **`active-custom` 설정에서만**. passive 설정은 `409 INGESTION_NOT_APPLICABLE`을 반환한다 |
| `GET /spoke/common/data/{urn}/event/ingestion` | 데이터셋별 인제스천 이벤트 이력 (active-custom: DataSpoke 실행이 기록; passive: DataHub의 DataProcessInstance 레코드를 시간별 폴링이 기록) |
| `GET /spoke/common/ingestion` | 데이터셋별 `attr/ingestion/*`을 집계하는 크로스 데이터셋 리스트 뷰 |

각 `event/ingestion` 행은 `event_type`(성공이면 `INGESTION.COMPLETE`, 실패면
`INGESTION.FAIL`)과 그에 대응하는 `status`(`success` / `failure`)를 담는다.

### Imazon 예시

#### Case 1 — Active-custom, Postgres `catalog.title_master` (daily)

DataSpoke가 추출을 소유한다.
Airflow `ingestion-active-daily` DAG이 매일 in-house Postgres 추출기를 호출하고,
수동 실행도 가능하다.

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)/attr/ingestion/conf
```
```json
{
  "mode": "active-custom",
  "platform": "postgres",
  "locator": {"host": "pg-oltp.imazon.internal", "port": 5432},
  "identifier": {"database": "example_db", "schema_name": "catalog", "table": "title_master"},
  "auth": {"username": "spoke_reader", "secret_ref": {"name": "dataspoke-source-cred-title-master", "key": "password"}},
  "is_enabled": false,
  "schedule_tier": "daily"
}
```

스케줄을 켜기 전에 코딩 에이전트가 연결을 검증한다:

```http
POST .../method/ingestion/run    { "dry_run": true }
```

`is_enabled=false` 동안 `method/ingestion/run`을 호출할 수 있는 유일한 방법은 dry-run이다.
non-dry-run은 `409 INGESTION_DISABLED`를 반환한다. dry-run이 성공하면 팀이 스위치를 켠다:

```http
PATCH .../attr/ingestion/conf    { "is_enabled": true }
```

일간 Airflow tier DAG 실행 후, 팀이 데이터셋별 이벤트 이력을 조회한다:

```http
GET .../event/ingestion?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

각 행은 실행 중에 DataSpoke 추출기가 DataHub에 emit한 `DataProcessInstance` aspect에 기반한다.
즉, 동일한 레코드가 DataHub UI에서도 보인다.

#### Case 2 — Passive, Postgres `catalog.editions` (DataHub Managed Ingestion 경유)

팀이 컬럼 단위 lineage와 프로파일 통계를 원한다.
DataSpoke의 in-house 추출기는 이를 생성하지 않는다.
DataHub Managed Ingestion을 직접 설정한다:
**`http://datahub.<domain>/ingestion`**에서
`catalog.editions` 대상의 postgres 레시피를 daily cron으로 만들고,
DataHub의 executor가 실행하도록 둔다.
DataSpoke는 이 설정에 손을 대지 않는다.

데이터셋을 DataSpoke surface에 노출하고 이벤트 이력을 받기 위해 passive로 등록한다:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "postgres",
  "identifier": {"database": "example_db", "schema_name": "catalog", "table": "editions"},
  "is_enabled": true
}
```

`locator`, `auth`, `schedule_tier`는 없다 — 외부 인제스터의 영역이다.
이 URN에 대한 `POST .../method/ingestion/run`은 `409 INGESTION_NOT_APPLICABLE`을 반환한다.

DataHub의 executor가 실행을 마치면, DataSpoke의 시간별 폴링이 이벤트를 노출한다:

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
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "kafka",
  "identifier": {"topic": "imazon.orders.events", "cluster": "example_kafka"},
  "is_enabled": true
}
```

스크립트가 실행되어 DPI를 emit하면, 다음 시간별 폴링에서 Case 2와 동일한 형태로
`event/ingestion`에 한 행이 노출된다.

#### 크로스 데이터셋 오버뷰

```http
GET /api/v1/spoke/common/ingestion?limit=100
```

데이터셋별로 한 행을 반환한다.
각 행은 `attr/ingestion/*` 집합(모드, 적용되는 경우 스케줄, 마지막 이벤트 상태)을 담는다.
대시보드와 일괄 감사에 유용하다.

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
전체 계약 — conf 사전 조건, 결과 행 형태, soft-delete / 부활 의미, DataHub assertion
aspect emission — 은 [`spec/feature/VALIDATION.md`](feature/VALIDATION.md)에 정의되어 있다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `GET/PUT/PATCH/DELETE /spoke/common/data/{urn}/attr/validation/conf` | 검증 슬롯의 읽기 / 생성·교체 / 부분 갱신 / soft-delete (`description` + `variables`). DataHub에 없는 URN에 PUT하면 `422 DATASET_NOT_IN_DATAHUB` |
| `POST /spoke/common/data/{urn}/attr/validation/result` | 결과 `{data_time, score, variables}`를 추가. 미선언 변수 키는 `422 UNKNOWN_VARIABLE`; `score`가 `[0,1]` 범위를 벗어나면 `422 INVALID_SCORE` |
| `GET /spoke/common/data/{urn}/attr/validation/result?from=…&until=…&limit=…` | `data_time`을 기준으로 한 과거 결과 (RFC 3339, `from` 포함, `until` 미포함). 기본 `limit=1000`, 서버 상한 `10000` |
| `GET /spoke/common/data/{urn}/event/validation` | 데이터셋별 검증 이벤트 이력 |
| `GET /spoke/common/validation` | 설정(설명 + 변수 이름 목록)과 최신 결과(data_time, score)를 담은 크로스 데이터셋 리스트 |

### Imazon 예시

주문 팀이 `orders.daily_fulfillment_summary`에 검증 슬롯을 한 개 구성하고,
일간 품질 태스크가 보고할 변수 이름을 선언한다:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)/attr/validation/conf
```
```json
{
  "description": "일간 주문 이행 품질: 행 수, 충족률, anomaly score",
  "variables": ["row_cnt", "fill_rate", "anomaly_score"]
}
```

**파이프라인이 emit하는 결과.** 일간 파티션을 작성하는 동일 Airflow DAG이 직후
팀의 품질 태스크를 실행해 세 변수를 계산하고 POST한다:

```http
POST .../attr/validation/result
```
```json
{
  "data_time": "2026-05-01T00:00:00Z",
  "score": 1.0,
  "variables": {
    "row_cnt": 1250.0,
    "fill_rate": 0.98,
    "anomaly_score": 0.02
  }
}
```

결과는 DataHub Quality 탭에 `data_time`을 기준으로 timestamp된
`assertionRunEvent`로 노출된다. `score < 1.0`이면 DataHub UI에서 assertion이
`FAILURE`로 표시된다. 원본 score는 partial-success 시맨틱을 위해
`actualAggValue`에 보존된다.

Kafka 토픽 `imazon.orders.events`에도 두 번째 슬롯을 둔다 — `description: "주문
이벤트 스트림 품질: 메시지 수와 lag"`, `variables: ["msg_cnt", "lag_seconds"]`.
동일한 surface가 관계형과 스트리밍 소스를 모두 다룬다.

**과거 데이터 베이스라인 캐시.** 다음 날의 품질 태스크는 30일 롤링 베이스라인 대비
오늘의 행 수 anomaly를 계산한다. `orders.daily_fulfillment_summary`를 다시 집계하는
대신 다음을 호출해 과거 `row_cnt` 시계열을 그대로 사용한다:

```http
GET .../attr/validation/result?from=2026-04-01T00:00:00Z&until=2026-05-01T00:00:00Z
```

결과는 최신 행부터(즉 `data_time` 내림차순) 반환된다.

**폐기와 부활.** `DELETE attr/validation/conf`는 슬롯을 soft-delete한다
(`204` 반환; 이후 `GET conf`는 `404`). 같은 URN에 `PUT`을 다시 호출하면 슬롯이
부활하며(`201` 반환), 부활된 슬롯은 새로운 설명과 변수 집합을 가질 수 있다 —
예: `variables: ["row_cnt", "fill_rate", "anomaly_score", "null_rate"]`.

**크로스 데이터셋 오버뷰.** `GET /spoke/common/validation`은 데이터셋별로
`description`, `variable_count`, `latest_data_time`, `latest_score`, `is_removed`
를 보여준다. 리스트는 `?removed=true|false`로 soft-delete된 슬롯의 포함 여부를
제어한다.

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
  비즈니스 개념(예: `TITLE`, `CUSTOMER`).
- **Edge** — *술어*: 관계 유형(예: `rates`, `is_edition_of`).
- **Triple** — `(subject_node, edge, object_node)` 사실. 트리플은 사전에 승인된
  노드와 엣지로만 구성되므로, 개념 어휘를 한 번 승인해 여러 사실에서 재사용한다.

노드와 엣지 ID는 slug(`title`, `rates`)이며, 노드·엣지 slug에는 `__`를 포함할
수 없다(트리플 ID 구분자로 예약). 트리플 ID는
`subject_node_id__edge_id__object_node_id` 형태의 결합 slug
(예: `edition__is_edition_of__title`)로, ID 자체가 사실을 인코딩하므로 재추론 사이에
자연히 idempotent하다.

온톨로지는 글로벌 아티팩트이다. `/spoke/common/ontogen/attr/conf`의 싱글톤 운영
conf가 추론 DAG 실행 시점과 스코프 데이터셋을 제어한다. 사람이 작성한 Markdown
**seed**(프롬프트·도메인 힌트·명명 규칙)가 데이터 소스와 함께 LLM을 안내하고,
수동 `POST /method/run`은 Markdown 본문을 해당 실행에만 적용되는 일회성 프롬프트로
실을 수 있다.

트리플을 사람이 승인하려면 양쪽 끝 노드와 엣지가 모두 사람에 의해 승인되어 있어야
하므로, 리뷰어는 일반적으로 **노드 → 엣지 → 트리플** 순서로 처리한다.

conf 필드 의미, seed 라이프사이클, 추론 파이프라인과 증분 재사용 규칙, 실행
시맨틱(`dry_run`·동시 실행·`default_run_prompt` 폴백), 그리고 트리플 리뷰 의존성
계약은
[`BACKEND.md §Ontology Generation Service`](feature/BACKEND.md#ontology-generation-service-srcbackendontogen)에
정의되어 있다. Producer / Reviewer 적대적 토론 추론 루프는
[`BACKEND_LLM.md §Adversarial Debate Framework`](feature/BACKEND_LLM.md#adversarial-debate-framework)
참조.

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
  "dataset_filter": {"tags": ["urn:li:tag:area:catalog"]}
}
```

**Seed.** LLM이 서점 도메인 친화적인 이름을 쓰도록 도메인 seed(Markdown)를 등록한다:

```http
POST /api/v1/spoke/common/ontogen/attr/seed
Content-Type: text/markdown
```
```markdown
# Imazon 서점 도메인

Imazon은 도서를 전문으로 하는 온라인 서점이다. 각 타이틀은 ISBN-13으로 식별되며,
여러 포맷(Hardcover, Paperback, eBook, Audiobook)이 별도 에디션으로 판매된다.
고객은 특정 에디션에 평점을 남길 수 있다. 가능한 한 웨어하우스 스키마명보다
비즈니스 도메인 언어를 선호한다.
```

**추론 출력.** 노드 넷, 엣지 둘, 트리플 둘 — 각 행의 `status`는 높은 신뢰도면
`llm_approved`, 사람 리뷰가 필요하면 `llm_pending`이다:

```
Nodes (subjects / objects):
  TITLE      confidence 0.96   member: catalog.title_master   (primary)
  EDITION    confidence 0.94   member: catalog.editions       (primary)
  CUSTOMER   confidence 0.93   member: customers.eu_profiles  (primary)
  RATING     confidence 0.72   member: reviews.user_ratings   (primary)
    evidence:
      - 외래 키 edition_id → catalog.editions.edition_id (schemaMetadata)
      - 외래 키 user_id → customers.eu_profiles.user_id (schemaMetadata)

Edges (predicates):
  is_edition_of  confidence 0.95   semantics: format-of relationship
  rates          confidence 0.87   semantics: customer-rates-edition

Triples (subject — predicate — object):
  EDITION  --is_edition_of--> TITLE      confidence 0.95
  RATING   --rates         --> EDITION   confidence 0.87
```

**리뷰 흐름 — 노드 먼저.** `RATING`은 노드 confidence가 가장 낮아(0.72, LLM이
"rating"과 "review"를 구분하는 데 모호함이 있음) 리뷰어가 노드부터 시작한다:

```http
GET /api/v1/spoke/common/ontogen/result/node
GET /api/v1/spoke/common/ontogen/result/node/rating
GET /api/v1/spoke/common/ontogen/result/node/rating/event
POST /api/v1/spoke/common/ontogen/result/node/rating/method/review
```
```json
{ "verdict": "approve", "reason": "FK 구조 확인. 추후 이름 변경 가능." }
```

**다음은 엣지.** 노드가 승인되면 엣지로 이동한다:

```http
GET /api/v1/spoke/common/ontogen/result/edge
POST /api/v1/spoke/common/ontogen/result/edge/is_edition_of/method/review
POST /api/v1/spoke/common/ontogen/result/edge/rates/method/review
```

**마지막으로 트리플.** 트리플의 양쪽 노드와 엣지가 모두 승인되면 해당 트리플이
리뷰 가능 상태가 된다:

```http
GET /api/v1/spoke/common/ontogen/result/triple
POST /api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review
```

승인은 DataSpoke 내부 상태를 갱신한다.

---

## UC4: Metadata Generation

**MANIFESTO §2.1 기능**:
*Metadata Generation — 온톨로지를 바탕으로 데이터 문서의 상태를 점검하고
생성 AI로 메타데이터를 제안한다. API와 리뷰 프로세스를 포함한다.*

이 기능은 DataHub 메타데이터에 이미 존재하는 **편집 가능 설명 aspect** —
데이터셋 설명 하나, 컬럼 설명 하나씩 — 의 값을 제안한다. 온톨로지 구조
자체는 제안하지 않는다 (UC3가 담당). 생성은 UC3가 읽는 검증된 DataHub aspect
집합과 UC3에서 승인된 온톨로지를 함께 입력으로 사용한다.

### User Story

> *데이터셋 오너 또는 거버넌스 리뷰어로서*,
> *DataSpoke가 문서가 부족한 데이터셋의 슬롯마다 여러 후보 설명을 제안하고,
> 그중 하나를 승인하고 부족한 것은 거부할 수 있기를 원한다*,
> *그래서* 모든 설명을 일일이 작성하지 않으면서도 표현은 직접 선택할 수
> 있도록 한다.

**베이스라인에서 지원하는 문서 필드**

DataSpoke는 DataHub의 **편집 가능(editable)** aspect에만 기록한다.
편집 불가능한 짝(`datasetProperties.description`,
`schemaMetadata.fields[].description`)은 인제스천 커넥터가 사용하므로,
거기에 기록하면 다음 커넥터 실행이 사람의 승인 결과를 덮어쓸 수 있다.
DataHub은 두 편집 가능 설명 필드를 모두 리치 텍스트로 취급하며,
UI는 Markdown으로 렌더링한다.

| 아이템 종류 | 형식 | DataHub 타깃 |
|---|---|---|
| `dataset.description` | Markdown | `editableDatasetProperties.description` |
| `column.<fieldPath>.description` | Markdown | `editableSchemaMetadata.editableSchemaFieldInfo[].description` (`fieldPath` 키) |

향후 범위(언급만, 여기서는 모델링하지 않음): `domains`·`globalTags` 제안.

`/spoke/common/metagen/attr/conf`의 **글로벌** 운영 conf가 생성 DAG 실행 시점과
스코프 데이터셋을 제어한다. `/spoke/common/data/{urn}/attr/metagen/conf`의
**데이터셋별** 경계가 옵트인 스위치이다 — `is_enabled=true` 경계 행이 없는
데이터셋은 글로벌 필터와 무관하게 제외된다.

스코프 내 (데이터셋, 아이템) 쌍마다 생성기는 여러 실행에 걸쳐 최대 `result_limit`
(기본 `3`)개의 후보를 누적한다. 리뷰어는 후보를 살펴보고 하나를 승인하면(그 값이
DataHub의 편집 가능 aspect로 emit되며 아이템이 잠긴다), 마음에 들지 않는 후보는
거부한다. **승인은 변경 가능하다**: 다른 형제 후보를 승인하면 이전에 승인된 후보가
같은 트랜잭션 안에서 강등되므로, 리뷰어는 언제든 마음을 바꿀 수 있다. **다음
실행부터**, `approved` 후보가 있는 아이템은 통째로 건너뛰며, 거부된 후보는 다음
실행 시작 시점에 일괄 삭제되어 해당 아이템이 처음부터 다시 제안된다.

conf 필드 의미, 후보 상태 라이프사이클, 아이템별 축출 정책, 실행 파이프라인,
그리고 Producer / Reviewer 적대적 토론은
[`BACKEND.md §Metadata Generation Service`](feature/BACKEND.md#metadata-generation-service-srcbackendmetagen)와
[`BACKEND_LLM.md §Metagen Adversarial Debate`](feature/BACKEND_LLM.md#metagen-adversarial-debate)에
정의되어 있다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/metagen/attr/conf` | 싱글톤 운영 conf — 위 필드 표 참조 |
| `POST /spoke/common/metagen/method/run` | 수동 생성 실행 트리거. body `{"dataset_urns": [...], "dry_run": bool}`은 선택. 동시 실행은 `409 METAGEN_RUNNING`, 비활성 conf의 비-dry-run은 `409 METAGEN_DISABLED` 반환 |
| `GET /spoke/common/metagen/event` | 글로벌 생성 실행 이벤트 이력 (`METAGEN.RUN_COMPLETE`, `METAGEN.RUN_FAILED`) |
| `GET /spoke/common/metagen/item` | 데이터셋 전반의 아이템 목록 (페이지네이션·`dataset_urn`·`kind`·`status` 필터) |
| `GET /spoke/common/metagen/item/{composite_id}` | `{dataset_urn}::{item_id}` 복합 ID로 아이템과 모든 후보 조회 |
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/metagen/conf` | 데이터셋별 경계 (`is_enabled`, `allowed`) |
| `GET /spoke/common/data/{urn}/attr/metagen/item` | 한 데이터셋의 아이템 목록 |
| `GET /spoke/common/data/{urn}/attr/metagen/item/{item_id}` | 아이템과 모든 후보 |
| `POST /spoke/common/data/{urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` | 단일 후보 승인·거부 — body `{ "verdict": "approve"\|"reject", "reason": "…" }`. 승인 시 DataHub emit과 아이템 잠금 |
| `GET /spoke/common/data/{urn}/event/metagen` | 데이터셋별 metagen 이벤트 (`METAGEN.CANDIDATE_APPROVE`, `METAGEN.CANDIDATE_REJECT`) |

### Imazon 예시

**Conf.** 거버넌스 팀이 metagen을 글로벌하게 활성화한다:

```http
PUT /api/v1/spoke/common/metagen/attr/conf
```
```json
{
  "is_enabled": true,
  "schedule_tier": "daily",
  "dataset_filter": {"tags": ["urn:li:tag:area:fulfillment"]},
  "result_limit": 3,
  "overwrite_pending": true
}
```

**경계.** 고객 팀이 `customers.eu_profiles`를 두 종류 모두 옵트인하고, 주문 팀은
`imazon.orders.events`를 컬럼 설명에 한해 옵트인한다:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.customers.eu_profiles,DEV)/attr/metagen/conf
```
```json
{
  "is_enabled": true,
  "allowed": ["dataset.description", "column.description"]
}
```

**실행.** 일일 Airflow DAG가 실행되거나, 리뷰어가 즉시 실행을 트리거한다:

```http
POST /api/v1/spoke/common/metagen/method/run
```

**아이템 조회.** 실행 후, 대시보드가 해당 데이터셋의 아이템 목록을 받는다:

```http
GET /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.customers.eu_profiles,DEV)/attr/metagen/item
```

`dataset.description` 아이템 하나와 컬럼당 `column.<fieldPath>.description`
아이템이 반환된다. 데이터셋 설명 아이템을 살펴보면:

```http
GET .../attr/metagen/item/dataset.description
```

```
item_id: dataset.description
kind:    dataset.description
status:  pending           # 아직 승인된 후보 없음
candidates (3 of result_limit=3):
  - candidate_id: c1   status: llm_approved   confidence 0.92
      "# EU 고객 프로필\n\nEU 지역의 GDPR 범위 고객 계정..."
  - candidate_id: c2   status: llm_approved   confidence 0.88
      "# Customers (EU)\n\nEU 고객의 권위 있는 프로필 레코드..."
  - candidate_id: c3   status: llm_approved   confidence 0.85
      "EU profiles 테이블 — EU 관할 등록 고객 계정..."
```

**리뷰.** 리뷰어가 `c1`을 승인하고, `c3`을 거부하고, `c2`는 그대로 둔다:

```http
POST .../attr/metagen/item/dataset.description/candidate/c1/method/review
{ "verdict": "approve", "reason": "EU/GDPR 범위를 가장 잘 표현했음." }

POST .../attr/metagen/item/dataset.description/candidate/c3/method/review
{ "verdict": "reject", "reason": "내용이 부족하고 핵심 사실을 빠뜨림." }
```

`c1` 승인 호출 시 DataSpoke는 그 값을 데이터셋의
`editableDatasetProperties.description`에 기록한다. 아이템 상태는
`status: approved`로 보고된다. `c2`는 보이는 히스토리로 `llm_approved`인
채 남고, 리뷰어가 마음을 바꾸면 `c2`를 승인할 수 있다(그 호출이 `c1`을
원자적으로 강등한다). `c3`는 다음 실행 시작 시점에 삭제된다.

**이벤트 이력.**

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
> *상시 운영되는 작은 메트릭 세트 —
> 인제스천 신선도, 검증 점수, 문서 상태 — 를 스케줄로 돌리고
> 범위를 지정하며 시계열로 추적하길 원한다*,
> *그래서* 대시보드를 직접 큐레이션하지 않고도
> 데이터 자산 건강도를 모니터링할 수 있도록 한다.

### 개념

거버넌스 **메트릭**은 데이터 자산 위에서 이름·스케줄·범위를 가지고 동작하는 집계다.
실행 결과는 `values`(이름 있는 float dict)와 `breakdown`(데이터셋별 리스트)을
담은 한 행으로 저장되어, 시간 범위 조회만으로 "지난주 화요일 어떤
데이터셋이 실패했는가"에 답할 수 있다 — 메트릭을 다시 실행할 필요 없다.

**모드.** 메트릭의 `mode`는 `active` 또는 `passive`다.

- **`active`** — DataSpoke가 `metric_type`과 `metric_conf`로부터 직접 측정한다.
- **`passive`** — 외부 시스템이 산출한 측정 결과를 DataSpoke가 적재한다(내장 계산
  없음). **이번 릴리스에서는 미구현; `mode: "passive"`로 PUT 시
  `501 NOT_IMPLEMENTED`를 반환한다.**

### 내장 액티브 메트릭 타입

베이스라인은 세 가지 `metric_type`을 제공한다. 모든 출력은 부동소수이며, 비율은
서버에서 미리 계산하지 않는다 — 클라이언트가 이름 있는 필드로부터 직접 도출한다.

| `metric_type` | 출력 `values` 키 | 의미 |
|---|---|---|
| `ingestion-freshness` | `total`, `ingested_in_time` | `total` = `dataset_filter`에 매칭된 데이터셋 수; `ingested_in_time` = 마지막 `INGESTION.COMPLETE`가 **데이터셋별 신선도 윈도** 안에 있는 데이터셋 수. 윈도는 각 데이터셋의 인제스천 설정에서 도출된다: active-custom → `schedule_tier` 주기의 2배(`hourly`→7200s, `daily`→172800s, `weekly`→1209600s); passive → DataHub 동기화 주기의 2배(hourly → 7200s); 설정이 없거나(또는 `schedule_tier`가 없는 active-custom) → `metric_conf.time_window_sec`로 폴백. 2배는 지연 인제스천을 위한 여유다 |
| `validation-score` | `total`, `validation_score_sum` | `total` = 매칭된 데이터셋 수; `validation_score_sum` = 각 데이터셋의 최신 검증 `score` 합 — **데이터셋별 윈도** = 해당 데이터셋의 최근 N개 검증 간격 평균 × 2(N은 `validation_score_n_intervals` 런타임 설정, 기본 3). 간격이 N개 미만이면 `metric_conf.time_window_sec`로 폴백; 윈도 안에 검증 결과가 없으면 기여는 0.0 |
| `doc-health` | `total`, `doc_health` | `total` = 매칭된 데이터셋 수; `doc_health` = 데이터셋별 문서 점수의 합. 테이블 설명과 모든 컬럼 설명이 비어 있지 않으면 `1.0`, 아니면 `0.0` |

`metric_conf`는 타입별 파라미터를 담는다: `ingestion-freshness`와
`validation-score`의 `time_window_sec`은 데이터셋별 윈도를 도출할 수 없을 때 쓰는
**폴백** 윈도이며(양의 정수 초, 팩토리 기본 `172800`), `doc-health`는 빈 `{}`를 사용한다.

`dataset_filter`는 네 가지 선택적 차원을 갖는다: `origin`(DataHub 데이터셋 URN의
세 번째 세그먼트로 들어가는 `FabricType` 값 — `PROD` / `DEV` / `CORP` / `EI` /
`STG` / `NON_PROD` / `QA` / `TEST` / `PRE` / `RVW` / `SIT` / `SANDBOX` / … — 값은
DataHub로 그대로 전달된다), `tags`(DataHub 태그 URN), `glossary_terms`(DataHub
용어 URN), `dataset_urns`(명시적 `urn:li:dataset:(…)` URN). 태그·용어·URN
차원은 하나의 OR-그룹을 이루며, `origin`은 그 OR-그룹과 AND로 결합된다. `{}`는
모든 데이터셋을 뜻한다. URN 포맷은 PUT/PATCH 시점에 검증되며
(`422 INVALID_DATASET_URN`), 실행 시점에 해석되지 않는 URN은 건너뛰고
`METRIC.RUN_COMPLETE` 이벤트의 `unresolved_urns`에 보고된다. 리졸버의 GraphQL
형태는
[`DATAHUB_INTEGRATION.md §Origin filter group`](DATAHUB_INTEGRATION.md#origin-filter-group)에,
breakdown 형태와 DAG 시맨틱은
[`BACKEND.md §Metrics Service`](feature/BACKEND.md#metrics-service-srcbackendmetrics)에
정의되어 있다.

### 팩토리 디폴트

최초 기동 시, DataSpoke는 내장 타입별로 한 개씩 메트릭을 시드한다(`metric_definitions`
행이 부재할 때만 삽입; 멱등). 기본값은 `mode: "active"`, `is_enabled: false`,
`schedule_tier: "daily"`, `dataset_filter: {}`, 타입에 맞는 `metric_conf`다.
시드는 비활성 상태로 들어가므로, 거버넌스 리드가 PATCH `is_enabled: true`로
명시적으로 켜거나 `dry_run: true` 1회 실행을 거친 뒤 스케줄 측정이 시작된다.
사용자는 어떤 디폴트라도 수정·비활성화·삭제할 수 있고, 같은 세 타입으로 메트릭을
더 추가할 수 있다.

### API Mapping

| 엔드포인트 | 용도 |
|---|---|
| `POST /spoke/dg/metric` | 메트릭 생성 — `metric_id`를 정의 필드와 함께 요청 본문에 담는다. 중복 id는 `409 METRIC_EXISTS` |
| `PUT/PATCH/GET/DELETE /spoke/dg/metric/{metric_id}/attr/conf` | 기존 메트릭 교체·갱신·읽기·삭제 (`mode`, `is_enabled`, `metric_type`, `title`, `description`, `metrics`, `metric_conf`, `schedule_tier`, `dataset_filter`). `PUT`은 기존 정의를 교체하며 id가 없으면 `404 METRIC_NOT_FOUND` |
| `POST /spoke/dg/metric/{metric_id}/method/run` | 측정 실행 트리거; `dry_run: true`는 기록 없이 평가만. 동일 메트릭의 동시 실행은 `409 METRIC_RUNNING` |
| `GET /spoke/dg/metric/{metric_id}/attr/result?from=…&to=…` | 과거 측정의 시계열 (각 행은 `values`와 데이터셋별 `breakdown`을 담음) |
| `GET /spoke/dg/metric/{metric_id}/event` | 실행 완료·정의 변경 이벤트 |
| `GET /spoke/dg/metric` | 모든 메트릭 리스트 |

사용 가능한 `schedule_tier`: `hourly`, `daily`, `weekly`. 활성화되면 해당
주기로 자동 실행되며, 온디맨드 실행은 항상 `POST .../method/run`을 통해 일어난다.

### Imazon 예시

CDO가 DEV 범위의 일간 doc-health 메트릭을, `metric_id`를 생성 본문에 담아 추가한다:

```http
POST /api/v1/spoke/dg/metric
```
```json
{
  "metric_id": "doc-health-dev",
  "mode": "active",
  "is_enabled": true,
  "metric_type": "doc-health",
  "title": "Doc Health (DEV)",
  "description": "DEV 데이터셋의 일간 문서 완전성 점검",
  "metrics": ["total", "doc_health"],
  "metric_conf": {},
  "schedule_tier": "daily",
  "dataset_filter": {"origin": "DEV"}
}
```

스케줄을 기다리지 않고 CDO가 즉시 첫 실행을 트리거한다:

```http
POST /api/v1/spoke/dg/metric/doc-health-dev/method/run
```

1주일 후, 보드 보고용으로 추세를 가져온다:

```http
GET /api/v1/spoke/dg/metric/doc-health-dev/attr/result?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

각 행은 `values: {"total": 142.0, "doc_health": 119.0}`와, **미문서화**
데이터셋(`0.0`을 기여한 데이터셋, 예: `customers.eu_profiles`,
`shipping.carrier_status`)만 나열하는 데이터셋별 breakdown을 담는다 — 보드
리뷰는 아직 남은 작업에만 집중할 수 있다.
