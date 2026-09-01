# API Design Principle

본 문서는 RESTful API의 핵심 가치를 반영한 표준적인 설계 원칙들을
제시한다. 본 지침의 준수를 권고하지만, 명확한 기술적 근거가 있을
경우 예외적인 설계를 허용한다.

---

## 1. Standard Request & Response Formats

### 1. Basic Guide

요청 시에는 서버가 자원을 정확히 식별하고 처리할 수 있도록 다음
표준을 준수한다.

- **Content-Type 명시:** 모든 쓰기 요청(POST, PUT, PATCH) 시 헤더에
  `Content-Type: application/json`을 명시한다.
- **UTF-8 인코딩:** 데이터의 깨짐을 방지하기 위해 반드시 UTF-8
  인코딩을 사용한다.
- **필드 네이밍 컨벤션:** URI와 마찬가지로 Request Body의 필드명은
  일관성을 유지한다 (예: `snake_case` 또는 `camelCase` 중 팀 표준 선택).
- **날짜/시간 형식:** ISO 8601 표준(`YYYY-MM-DDTHH:mm:ssZ`)을 사용하여
  타임존 혼선이 없도록 한다.

### 2. Response Format Guide

응답은 클라이언트가 성공 여부를 즉시 파악하고, 데이터를 쉽게 파싱할
수 있는 구조여야 한다.

- **HTTP Status Code 활용:** 응답 상태를 단순히 JSON 바디에 담지 말고,
  적절한 HTTP 상태 코드(200 OK, 201 Created, 400 Bad Request, 404 Not Found
  등)를 선행하여 전달한다.
- **에러 응답의 표준화:** 에러 발생 시 일관된 에러 객체를 반환한다.
  - 예: `{"error_code": "INVALID_PARAMETER", "message": "The 'count' field must be an integer."}`

### 3. Separation of Content and Metadata

응답 바디 내에서는 **클라이언트가 실질적으로 요청한 데이터(Content)**와
**시스템 처리를 위한 정보(Metadata)**를 명확히 분리한다. 이를 통해
클라이언트는 핵심 데이터 모델과 부가 정보를 독립적으로 처리할 수 있다.

- **Content (요구된 정보):** 비즈니스 로직 상 핵심이 되는 자원(Resource)
  데이터이다.
- **Metadata (메타 정보):** 페이지네이션 정보, 응답 시간, API 버전, 추적
  ID(Trace ID) 등을 포함한다.

#### Best Practice Example

과일 목록을 요청했을 때, 자원인 `fruits`와 그 외의 제어 정보를 분리한
모범 사례이다.

```json
{
    // Content: 요구된 자원 정보 (컬렉션 path에 대응하는 리스트 응답)
    "fruits": [
         {"name": "apple", "count": 5},
         {"name": "banana", "count": 3}
    ],

    // Metadata: 제어 및 부가 정보
    "offset": 5,
    "limit": 2,
    "total_count": 30,
    "resp_time": "2026-01-01T13:14:15.123+09:00"
}
```

### 4. Unknown Fields in Write Requests

쓰기 요청 바디(`POST`, `PUT`, `PATCH`)는 알 수 없는 필드를 조용히 무시하지 않고
`422 INVALID_PARAMETER`로 거부한다. 그래서 오타가 난 필드 이름은 조용한 무동작이
아니라 명시적인 실패로 드러난다.

오류 응답 봉투는 거부된 필드를 `detail.errors[].loc`로 식별하며, 위반한 값 자체는
절대 그대로 담지 않는다. 쓰기 요청 바디에는 자격 증명이 흔히 실리기 때문이다.
오류 응답 봉투의 전체 형식은 `API.md` §Error Catalogue가 정의한다.

---

## 2. Standard URI Structure

### 1. 자원(Resource)은 반드시 '명사형'을 사용한다

URI는 "무엇을(What)"에 집중해야 한다. "어떻게(How)"에 해당하는 동사는
HTTP Method로 처리한다.

- **Bad (Action-based):**
  - `POST /createNewUser`
  - `GET /get_order_list`
  - `DELETE /delete-post/42`

- **Good (Resource-based):**
  - `POST /user` (사용자 생성)
  - `GET /order` (주문 목록 조회)
  - `DELETE /post/42` (42번 게시글 삭제)

---

### 2. Classifier / Identifier 구조를 준수한다

계층 구조를 통해 자원의 소속과 식별자를 명확히 구분한다.

- **구조:** `/{대분류}/{식별자}/{소분류}/{식별자}`
- **Example (쇼핑몰 리뷰 시스템):**
  - `/product` (상품 전체 목록)
  - `/product/p001` (ID가 p001인 특정 상품)
  - `/product/p001/review` (p001 상품에 달린 리뷰 전체 목록)
  - `/product/p001/review/rev99` (p001 상품의 리뷰 중 ID가 rev99인 특정 리뷰)

---

### 3. 컬렉션과 단일 자원 구분

식별자가 없는 자원 경로는 컬렉션(목록)을 반환하고, 식별자가 있는
경로는 단일 객체를 반환한다.

- **Example (결제 내역 조회):**
  - `GET /payment`
  - **Response (List):**
    ```json
    [
      { "pay_id": "T01", "amount": 5000 },
      { "pay_id": "T02", "amount": 12000 }
    ]
    ```

- **비교:** `GET /payment/T01` (단일 객체 `{ "pay_id": "T01", ... }` 반환)

---

### 4. Meta-Classifier (attr, method, event) 활용

이 원칙은 데이터 필드(Field)와 비즈니스 로직(Action), 그리고 상태
변화(History)를 명확히 분리하여 API의 성격을 직관적으로 드러내는 데
목적이 있다.

이 방식은 필수는 아니지만, 자원의 식별자(Identifier) 뒤에 많은 기능이나
특성이 오는 경우에 이들을 정리하는 용도로 도입하면 좋다.

#### 1. attr (Attributes): 상태 및 구성 정보의 분리

자원 전체를 가져오는 대신, 객체의 **메타 데이터나 설정값, 권한 상태**
등 특정 속성 그룹만 조회/수정할 때 사용한다. 이는 무거운 객체 전체를
주고받는 비용을 줄여준다.

- **Example (사용자 설정):**
  - `GET /member/m_123/attr` : 사용자의 프로필 사진, 마케팅 수신 동의 여부,
    언어 설정 등 '속성'만 조회.
  - `PATCH /member/m_123/attr` : 특정 속성(예: 다크모드 활성화)만 업데이트.

- **Example (디바이스 상태):**
  - `GET /iot-device/dev_88/attr/battery` : 특정 속성 그룹 조회 — 기기의
    현재 배터리 잔량 확인.

#### 2. method (Functional Actions): 단순 CRUD 이상의 비즈니스 로직

REST는 기본적으로 자원의 상태를 다루지만, 실제 서비스에서는 **'승인',
'복구', '전송'** 등 단순한 필드 수정으로 표현하기 어려운 복잡한 비즈니스
프로세스가 존재한다. 이를 `method` 뒤에 명시하여 행위를 명확히 한다.

- **Example (결제 및 주문 프로세스):**
  - `POST /payment/pay_abc/method/approve` : 결제 승인 로직 실행.
  - `POST /order/ord_555/method/calculate-tax` : 세금 계산 로직 호출 (결과값만 반환).

- **Example (계정 보안):**
  - `POST /account/u_789/method/lock` : 보안 위협으로 인한 계정 강제 잠금.
  - `POST /account/u_789/method/unlock` : 본인 인증 후 계정 잠금 해제.

#### 3. event (Lifecycle & Audit Logs): 시간 흐름에 따른 상태 변화

자원은 시간에 따라 변한다. `event`는 특정 자원에 발생한 **사건의
기록(History)**을 추적할 때 사용한다.

- **Example (배송 추적):**
  - `GET /delivery/deliv_99/event` :
    [집하 완료 -> 허브 도착 -> 배송 출발 -> 완료]로 이어지는 타임라인 로그
    전체 조회.

- **Example (문서 변경 이력):**
  - `GET /document/doc_001/event` : 누가, 언제 이 문서를 수정하거나
    조회했는지에 대한 감사 로그(Audit Log).

- **Example (에러 로그):**
  - `GET /server/srv_10/event` : 해당 서버에서 발생한 시스템 이벤트 및
    에러 발생 이력.

- **Example (이벤트 기록):**
  - `POST /project/proj_42/event/deployment` : 배포 사건을 프로젝트 타임라인에 기록.

---

### 5. URL Query Segments는 '필터링/정렬' 용도로 사용한다

데이터의 본질적인 위치(Path)는 유지하되, 보여주는 방식만 바꿀 때
사용한다.

- **Filtering (조건 검색):**
  - `/ticket?status=open&priority=high`
    (상태가 open이고 우선순위가 높은 티켓만 검색)

- **Sorting (정렬):**
  - `/product?sort=price_asc` (가격 낮은 순 정렬)

- **Pagination (구간 조회):**
  - `/log?offset=20&limit=10` (21번째 로그부터 10개만 가져오기)
