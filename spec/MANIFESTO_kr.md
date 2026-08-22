# DataSpoke Baseline

A Baseline Product for an Omnipotent Data Catalog

![DataSpoke Concept](../assets/dataspoke_concept.jpg)

---

## 1. 배경

### AI 시대, 데이터 카탈로그가 수행하면 좋을 기능들

- **Self-Organization (자기 조직화)**: 데이터 카탈로그가 가용한
  데이터를 기반으로 스스로 온톨로지를 구성한다.
- **Self-Purification (자기 정화)**: 데이터 카탈로그가 온톨로지를
  기반으로 스스로의 상태를 점검하고 정화한다. 예를 들어, 데이터
  문서에서 오류를 찾아 보고하거나, 생성형으로 데이터 문서를 제안한다.
- **Online Quality Ledger (온라인 품질 원장)**: 데이터 카탈로그가
  데이터 파이프라인의 데이터 품질 태스크가 결과를 보고하거나
  캐시하기 위한 API를 제공한다.

### Vibe Coding의 시대, 맞춤형 데이터 카탈로그

기존 데이터 카탈로그 솔루션들은 기능은 방대하지만, 실제 활용도는
낮은 경우가 많다. 이는 모든 사용자를 만족시키려다 보니 복잡도가
높아져 정작 누구에게도 최적화되지 못했기 때문이다.

- 사용자 그룹별 상이한 요구사항:
  - 데이터 엔지니어: 기술 스펙, 파이프라인 비용
  - 데이터 분석가: Text-to-SQL을 위한 도메인 중심 메타데이터
  - 데이터 스튜어드: 가용성 지표, 품질 검사 이력
  - 보안팀: PII(개인정보) 활용 현황
- 도메인 특화 기능의 필요성: ML 기반 맞춤형 품질 모듈, 기존 수집
  구조에 맞지 않는 비표준 데이터 소스 등록 등 범용 카탈로그가
  지원하지 못하는 확장 요구

Vibe Coding의 시대, 앞서 설명한 추가 기능을 갖추고 특정 회사에
필요한 기능만 넣은 맞춤형 데이터 카탈로그를 제작하는 것은 어려운
일이 아니다. 그러나, 이를 시작하기에 아주 좋은 베이스라인 제품이
있다면 그 또한 나쁜 일은 아니다.

## 2. 프로젝트 정의

이 프로젝트는 다음 두 가지 핵심 요소를 개발하는 것을 목표로 한다.

- **Baseline Product** — 자기 조직화, 자기 정화, 그리고 온라인
  품질 원장을 갖춘 데이터 카탈로그의 기본 구현체
- **Productized Scaffold** — 스펙, 개발 환경, Coding Agent
  유틸리티 등 커스텀 개발을 지원하는 프레임워크

사용자는 자신의 목적에 맞게 이 베이스라인을 확장할 수 있으며,
개발 과정에서 제공된 Scaffold를 활용할 수 있다.

프로젝트명 **DataSpoke**는 기존 DataHub를 Hub로, 각 조직에 맞춘 특화 확장판을
바퀴살(Spoke)로 볼 수 있다는 점에 착안하였다.

### 2.1 Baseline Product

#### 기능

- **Ingestion Control**: 데이터 수집 설정, 제어 및 관리를 한곳에서
  수행할 수 있는 편의 기능 제공
- **Validation**: 검증의 최종 결과와 중간 결과의 구성 가능한 저장소.
  데이터 파이프라인의 데이터 품질 태스크가 사용한다.
- **Ontology Generation**: DataHub에 등재된 메타데이터(설명, 스키마,
  glossary term, document 엔티티)를 바탕으로 스스로 온톨로지를 구성하고
  DataSpoke 내부의 그래프 DB 및 벡터 DB에 유지
- **Metadata Generation**: 온톨로지에 기반해 데이터 문서화 상태를
  점검하고, 생성형 AI로 메타데이터를 제안하는 API 및 검수 프로세스 제공
- **Governance**: 문서화 커버리지, 데이터 신선도 등 거버넌스 지표
  설정 및 모니터링 API 제공

#### 시스템 구조

DataSpoke는 네 가지 컴포넌트로 구성된다.

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

- **DataSpoke UI**: 기능 기반의 좌측 메뉴를 갖춘 단일 셸 인터페이스이다.
  ```
  ┌─────────────────────────────────────────────────────┐
  │ DataSpoke              user@imazon ▼  Logout        │
  ├──────────────┬──────────────────────────────────────┤
  │ Governance ▾ │   Governance · Dashboard             │
  │  Dashboard   │                                      │
  │  Metrics     │                                      │
  │ Ingestion    │                                      │
  │ Validation   │                                      │
  │ OntoGen      │                                      │
  │ MetaGen      │                                      │
  ├──────────────┤                                      │
  │ Profile      │                                      │
  │ Admin        │                                      │
  └──────────────┴──────────────────────────────────────┘
                  UI Main Page
  ```
- **DataSpoke API**: 두 축 URI 구조 — 데이터셋 단위 크로스-기능 surface와
  §2.1 기능별 네임스페이스(크로스-데이터셋 리스트 뷰와 글로벌 기능 담당).
  ```
  /api/v1/spoke/common/data/{dataset_urn}/…   # 데이터셋 리소스 (per-dataset, cross-feature)
  /api/v1/spoke/ingestion                     # 인제스천 크로스-데이터셋 리스트
  /api/v1/spoke/validation                    # 검증 크로스-데이터셋 리스트
  /api/v1/spoke/ontogen/…                     # 온톨로지 생성 (글로벌 싱글톤)
  /api/v1/spoke/metagen/…                     # 메타데이터 생성 (conf 컬렉션 + 글로벌 리뷰 큐)
  /api/v1/spoke/governance/…                  # 거버넌스 메트릭
  ```
- **DataSpoke Backend/Pipeline**: 인제스션, 검증, 온톨로지 생성,
  메타데이터 생성, 거버넌스(§2.1의 다섯 가지 기능) 등 핵심 로직 처리.
- **DataHub**: 메타데이터 SSOT.

### 2.2 Productized Scaffold

#### AI Scaffold

에이전트 비종속 코어(`scaffold/`)는 생성기·평가기 역할, 리뷰 계약,
공유 평가기 지식을 정의한다. 공유 스킬은 `.agents/skills/`에 두고,
CLI별 네이티브 바인딩(Claude Code용 `.claude/`, Codex용 `.codex/`)은 호출 및
권한 설정을 담당한다. 이 구조를 통해 어느 에이전트든 첫 세션부터 프로젝트
컨벤션과 스펙 체계를 인지하고 동일한 계획 → 승인 → 생성 → 평가 워크플로를
실행한다. cron 기반 PR 자동화(PRauto)는 Claude Code 통합이다. 상세 사양은
`spec/AI_SCAFFOLD.md`를 참고한다.

#### Development Scaffold

Kubernetes 기반으로 스크립트화된 배포 시스템이다. `helm-charts/bin/`의
`install.sh --profile {dev|prod}`이 단일 진입점이며, dev 프로파일은
nginx-ingress, DataHub, Langfuse, 더미 데이터 등 주변 컴포넌트와 umbrella
Helm 차트(`helm-charts/dataspoke/`, dev 오버레이 `values-dev.yaml`)를 함께
설치한다. prod 프로파일은 umbrella 차트만 설치한다. 상세 사양은
`spec/feature/HELM_CHART.md`를 참고한다.
