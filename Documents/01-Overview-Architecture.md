# P4GitSync 개요 및 아키텍처

## 프로젝트 개요

P4GitSync는 Perforce(Helix Core) Stream의 전체 히스토리를 Git에 정확히 재현하고, 실시간으로 동기화하여 P4에서 Git으로 완전 전환하기 위한 마이그레이션 시스템입니다.

- **런타임**: Python 3.12+
- **라이선스**: Private - All rights reserved

## 주요 기능

| 기능 | 설명 |
|------|------|
| CL → Commit 1:1 매핑 | 메타데이터(작성자, 날짜, 설명) 완전 보존 |
| 다중 Stream 동기화 | P4 Stream 부모-자식 관계를 Git branch 분기점으로 재현 |
| Merge 재현 | Stream 간 integration을 Git merge commit으로 변환 |
| 실시간 동기화 | P4 Trigger + Redis 이벤트 기반 (폴링 fallback) |
| 초기 히스토리 Import | git fast-import를 활용한 대량 히스토리 일괄 변환 |
| Git LFS 지원 | 바이너리 에셋 자동 LFS 변환 (확장자/용량 기반) |
| Bare Repository 지원 | remote 없이 로컬 bare repo에 직접 동기화 가능 |
| 무결성 검증 | P4/Git 간 파일 내용 주기적 비교 + Circuit Breaker 자동 중단 |
| 장애 복구 | State DB 재구성, Git repo 재초기화, 실패 CL 자동/수동 재시도 |
| 컷오버 | P4→Git 전환 5단계 프로세스 |
| 모니터링 | HTTP API(FastAPI) + Slack 알림 (심각도별 채널 분리) |

## 시스템 아키텍처

```
                                           ┌────────────────────────────────────────────┐
                                           │           Worker Service (Python)          │
┌──────────┐     ┌──────────┐     ┌──────┐ │  ┌─────────────┐    ┌──────────────────┐  │
│ P4 Server│────>│ Trigger  │────>│Redis │─┼─>│EventConsumer│───>│ SyncOrchestrator │  │
│ (submit) │     │(bash+curl│     │Stream│ │  └─────────────┘    │  ┌────────────┐  │  │
└──────────┘     └──────────┘     └──────┘ │  ┌─────────────┐    │  │ P4Client   │  │  │
                                           │  │ CL Poller   │───>│  │ GitOperator│  │  │
                                           │  │ (fallback)  │    │  │ StateStore │  │  │
                                           │  └─────────────┘    │  └────────────┘  │  │
                                           │  ┌─────────────┐    └────────┬─────────┘  │
                                           │  │ API Server  │             │             │
                                           │  │ (FastAPI)   │    ┌───────┴────────┐    │
                                           │  └─────────────┘    │   Git Repo     │    │
                                           │  ┌─────────────┐    │   (local/bare) │    │
                                           │  │ Notifier    │    └───────┬────────┘    │
                                           │  │ (Slack)     │            │              │
                                           │  └─────────────┘    ┌──────┴─────────┐    │
                                           │                     │   Git Remote    │    │
                                           │                     │   (optional)    │    │
                                           │                     └────────────────┘    │
                                           └────────────────────────────────────────────┘
```

## 핵심 데이터 흐름

```
P4 Changelist
    ↓
P4ChangeInfo (changelist, user, description, timestamp, files[])
    ↓
P4FileAction[] (depot_path, action, file_type, revision)
    ↓
CommitBuilder._extract_file_changes()
    ├─ 파일 content 추출 (p4 print, 메모리 기반)
    ├─ LFS 포인터 변환 (활성화 시)
    └─ 삭제 파일 목록
    ↓
CommitMetadata
    ├─ author_name, author_email, author_timestamp
    ├─ message + "[P4CL: NNN]" 메타데이터
    └─ integration_info (merge 시)
    ↓
GitOperator.create_commit() / create_merge_commit()
    ↓
StateStore
    ├─ cl_commit_map: (changelist, stream) → commit_sha
    ├─ sync_state: stream → last_cl, commit_sha
    └─ git_push_status: 'pending' → 'pushed'/'failed'
    ↓
git push → Git Remote (설정 시)
```

## 주요 컴포넌트

### 오케스트레이션

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| SyncOrchestrator | `services/sync_orchestrator.py` | 모든 서비스를 조합하는 중앙 오케스트레이터. context manager로 생명주기 관리 |
| MultiStreamHandler | `services/multi_stream_sync.py` | 여러 P4 Stream을 각각 Git branch로 동기화 |
| CommitBuilder | `services/commit_builder.py` | P4 changelist 데이터를 Git commit으로 변환 |

### P4 연동

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| P4Client | `p4/p4_client.py` | p4python 래퍼. P4 서버와의 모든 상호작용 |
| MergeAnalyzer | `p4/merge_analyzer.py` | P4 Stream 간 integration → Git merge 판단 |
| WorkspaceManager | `p4/workspace_manager.py` | 다중 stream 환경 workspace 관리 |

### Git 조작

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| GitOperator | `git/git_operator.py` | Protocol 기반 인터페이스 |
| Pygit2GitOperator | `git/pygit2_git_operator.py` | libgit2 기반 구현 (기본) |
| GitCliOperator | `git/git_cli_operator.py` | git CLI fallback |
| FastImporter | `git/fast_importer.py` | git fast-import 기반 대량 import |

### 이벤트 시스템

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| EventConsumer | `services/event_consumer.py` | Redis Stream에서 이벤트 소비 |
| EventCollector | `services/event_collector.py` | 미동기화 이벤트 수집 및 전역 정렬 |
| ChangelistPoller | `services/changelist_poller.py` | Redis 미사용 시 fallback 폴링 |

### 상태 및 안전장치

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| StateStore | `state/state_store.py` | SQLite(WAL mode) 기반 CL↔commit 매핑 |
| IntegrityChecker | `services/integrity_checker.py` | P4/Git 파일 무결성 비교 |
| IntegrityCircuitBreaker | `services/circuit_breaker.py` | 불일치 시 자동 동기화 중단 |

### 운영 지원

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| ApiServer | `api/api_server.py` | FastAPI 기반 HTTP API |
| SlackNotifier | `notifications/notifier.py` | 심각도별 Slack 알림 |
| SilenceDetector | `notifications/silence_detector.py` | 침묵 장애 감지 |
| DailyReporter | `notifications/daily_report.py` | 일일 동기화 리포트 |
| CutoverManager | `services/cutover.py` | P4→Git 전환 프로세스 |

## 기술 스택

| 영역 | 기술 |
|------|------|
| 런타임 | Python 3.12+ |
| P4 연동 | p4python >= 2024.1 |
| Git 라이브러리 | pygit2 >= 1.14 (libgit2) + git CLI fallback |
| State DB | SQLite3 (WAL mode) |
| 메시지 큐 | Redis Streams |
| HTTP API | FastAPI + uvicorn |
| 알림 | slack-sdk >= 3.27 |
| 컨테이너 | Docker + Docker Compose |
| CI/CD | GitHub Actions + GHCR |
