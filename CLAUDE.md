# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

P4GitSync: Perforce(Helix Core) Stream -> Git 마이그레이션 및 실시간 동기화 시스템. Python 3.12+ 기반.

## Build & Run

```bash
# 로컬 설치 (개발용)
cd p4gitsync
pip install -e ".[dev]"

# Docker 빌드 + 실행 (프로덕션)
docker compose up -d

# 올인원 개발 환경 (P4d + Redis + Gitea 포함)
docker compose -f docker-compose.dev.yml up -d
```

## Testing

```bash
cd p4gitsync
pytest                              # 전체 테스트
pytest tests/test_state_store.py    # 단일 파일
pytest -k "test_function_name"      # 단일 테스트
```

## Linting

```bash
cd p4gitsync
ruff check src/ tests/
ruff format src/ tests/
```

## Architecture

### 핵심 흐름: P4 Changelist -> Git Commit

```
P4 Server -> [Trigger/Poller] -> Redis Stream/직접 -> EventConsumer -> SyncOrchestrator -> Git Repo -> Git Remote
```

### 주요 컴포넌트 관계

- **`SyncOrchestrator`** (`services/sync_orchestrator.py`): 모든 서비스를 조합하는 중앙 오케스트레이터. context manager로 생명주기 관리
- **`P4Client`** (`p4/p4_client.py`): p4python 래퍼. P4 서버와의 모든 상호작용 담당
- **`GitOperator`** (`git/git_operator.py`): Protocol 기반 인터페이스. 두 가지 구현체:
  - `Pygit2GitOperator` (`git/pygit2_git_operator.py`): libgit2 기반 (기본값)
  - `GitCliOperator` (`git/git_cli_operator.py`): git CLI fallback
- **`StateStore`** (`state/state_store.py`): SQLite(WAL mode)로 CL<->commit 매핑 상태 관리
- **`CommitBuilder`** (`services/commit_builder.py`): P4 changelist 데이터를 Git commit으로 변환
- **`MergeAnalyzer`** (`p4/merge_analyzer.py`): P4 Stream 간 integration을 Git merge로 재현할지 분석

### 이벤트 기반 동기화 (Redis)

- **`EventCollector`**: P4 Trigger에서 받은 이벤트를 Redis Stream에 적재
- **`EventConsumer`**: Redis Stream에서 이벤트를 소비하여 동기화 트리거
- **`ChangelistPoller`**: Redis 미사용 시 fallback 폴링

### 다중 Stream 지원

- **`MultiStreamHandler`** (`services/multi_stream_sync.py`): 여러 P4 Stream을 각각 Git branch로 동기화
- **`StreamWatcher`** (`services/stream_watcher.py`): 새로운 Stream 자동 감지
- **`StreamPolicy`** (`config/sync_config.py`): Stream 필터링 정책 (include/exclude 패턴)

### 운영 안전장치

- **`IntegrityChecker`** + **`IntegrityCircuitBreaker`**: P4/Git 파일 무결성 비교, 불일치 시 자동 중단
- **`InitialImporter`** (`services/initial_importer.py`): git fast-import 활용 대량 히스토리 일괄 변환
- **`CutoverManager`** (`services/cutover.py`): P4->Git 전환 5단계 프로세스 (freeze->sync->verify->push->switch)

### 설정 시스템

- `AppConfig` dataclass 계층 (`config/sync_config.py`)
- config.toml 파일 또는 `P4GITSYNC_{SECTION}_{KEY}` 환경변수 (환경변수가 우선)
- `apply_env_overrides()`가 환경변수를 파싱하여 config dict에 병합

### API & 알림

- **FastAPI 서버** (`api/api_server.py`): /api/health, /api/status, /api/trigger 등
- **SlackNotifier** (`notifications/notifier.py`): 심각도별 채널 분리 (alerts/warnings/info)

## CLI Commands

엔트리포인트: `p4gitsync.__main__:main`

| 명령 | 설명 |
|------|------|
| `p4gitsync run` | 동기화 루프 실행 (기본) |
| `p4gitsync import [--stream]` | 초기 히스토리 import |
| `p4gitsync resync --from N --to M` | CL 범위 재동기화 |
| `p4gitsync rebuild-state` | Git log에서 State DB 재구성 |
| `p4gitsync reinit-git --remote URL` | Git repo 재초기화 |
| `p4gitsync cutover --dry-run/--execute` | P4->Git 컷오버 |
