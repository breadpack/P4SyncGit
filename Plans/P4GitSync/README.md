# P4-to-Git Migration & Realtime Sync System

Perforce Stream의 전체 히스토리를 Git에 정확히 재현하여, 최종적으로 P4에서 Git으로 완전히 전환하기 위한 마이그레이션 시스템 구현 계획.

## 핵심 전제

- **단일 Git Repository**: 모든 P4 Stream은 하나의 Git repo 안에서 branch로 표현된다.
- **상시 전환 가능 (Always Ready)**: 동기화 서비스는 지속적으로 실행되며, Git은 항상 P4와 동일한 최신 상태를 유지한다. 팀이 결정하는 **임의의 시점**에 컷오버할 수 있어야 한다.
- **완전 전환 목적**: 최종적으로 P4를 종료하고 Git만 사용하는 것이 목표. 동기화 서비스는 그때까지 무기한 운영된다.
- **Hard Deadline**: Phase 3 완료 후 최대 6개월 이내에 컷오버를 실행한다. 동기화 서비스의 무기한 운영은 운영 비용, 기술 부채, P4/Git 이중 인프라 유지 부담을 가중시키므로, 명확한 기한 내에 전환을 완료해야 한다. 6개월 경과 시 컷오버 불가 사유를 경영진에 보고하고 연장 여부를 결정한다.
- **히스토리 보존**: 전환 후 `git blame`, `git bisect`, `git log --graph`가 유의미하게 동작해야 한다.
- **LFS 사전 결정**: 게임 프로젝트 특성상 코드와 바이너리 에셋이 동일 Stream에 혼재하므로, 바이너리 에셋의 Git LFS 적용 전략은 Phase 1 시작 전에 반드시 확정한다. LFS 적용 시 최초 commit부터 `.gitattributes`를 포함해야 하며, 이후 변경 시 전체 히스토리 재작성이 필요하므로 비가역적이다. LFS 대상 확장자(`.uasset`, `.umap`, `.fbx`, `.png` 등)와 용량 임계값을 사전에 정의한다.
- **Depot 구조 분석 선행**: Phase 1 시작 전에 대상 depot의 파일 구성(코드/바이너리 비율, 총 용량, 확장자 분포)을 분석한다. 이 분석 결과가 LFS 전략, 동기화 범위(전체 파일 vs 코드만), 초기 import 소요 시간 추정의 기반이 된다.
- **Git Hosting 용량 제한 사전 확인**: Git hosting 서비스의 리포지토리 용량 제한을 사전에 확인한다.
  - GitHub: soft limit 5GB, hard limit 100GB (100MB 이상 단일 파일 push 거부)
  - GitLab: 기본 10GB (설정 변경 가능)
  - Self-hosted: 디스크 용량에 따라 결정
  - Depot 분석 결과와 대조하여 용량 초과 가능성을 사전 평가하고, 초과 예상 시 LFS 전략 또는 동기화 범위를 조정한다.
- **Partial Clone / Sparse-Checkout 교육 계획**: Git 전환 후 대규모 리포지토리에서의 효율적 작업을 위해, 팀원 대상으로 다음 교육을 Phase 3 전까지 실시한다.
  - `git clone --filter=blob:none` (partial clone): 필요한 blob만 on-demand 다운로드
  - `git sparse-checkout set <path>`: 필요한 디렉토리만 checkout하여 작업
  - `git clone --depth=N` (shallow clone): CI/CD 등에서 최근 히스토리만 clone
  - 팀 역할별(개발자, 아티스트, CI/CD) 권장 clone 전략 가이드 제공

## 문서 구조

| 문서 | 설명 |
|------|------|
| [Phase1-Foundation.md](Phase1-Foundation.md) | 인프라 기반 구축, 단일 stream 전체 히스토리 import |
| [Phase2-BranchAndMerge.md](Phase2-BranchAndMerge.md) | 다중 stream, branch 분기점 매핑, merge 재현 |
| [Phase3-Production.md](Phase3-Production.md) | 운영 안정화, 모니터링, 장애 복구, 컷오버 |
| [Ref-Architecture.md](Ref-Architecture.md) | 시스템 아키텍처 및 컴포넌트 상세 |
| [Ref-P4GitMapping.md](Ref-P4GitMapping.md) | P4 ↔ Git 개념 매핑, Stream 분기점 탐지, 한계 정리 |
| [Ref-TechStack.md](Ref-TechStack.md) | 기술 스택 선정 근거 |

## 핵심 목표

1. P4 submit 이벤트 발생 시 준실시간으로 Git에 반영
2. Changelist → Commit 1:1 매핑 (메타데이터 보존)
3. **Stream 부모-자식 관계를 Git branch 분기점으로 정확히 재현**
4. Stream 간 integration을 Git merge commit으로 재현
5. P4 서버에 부하를 주지 않는 비동기 처리
6. **동기화 완료 후 P4→Git 컷오버 수행**

## 비목표 (Scope 외)

- Git → P4 역방향 동기화 (단방향만 구현)
- Virtual stream 매핑
- 대용량 바이너리 에셋의 별도 관리 체계 구축 (바이너리 에셋은 Git LFS로 관리하며, LFS 전략은 Phase 1 시작 전에 확정한다. LFS 미적용으로 결정된 경우 코드 파일만 동기화한다)
- Stream path remap 매핑 (depot path와 workspace 경로 차이 발생 시 별도 분석 필요, 대상 depot에 remap 사용 여부는 Phase 1에서 사전 조사)
