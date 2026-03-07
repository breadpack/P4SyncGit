# P4GitSync

Perforce(Helix Core) Stream의 전체 히스토리를 Git에 정확히 재현하고, 실시간으로 동기화하여 P4에서 Git으로 완전 전환하기 위한 마이그레이션 시스템.

## 주요 기능

- **Changelist -> Commit 1:1 매핑**: 메타데이터(작성자, 날짜, 설명) 완전 보존
- **다중 Stream 동기화**: P4 Stream 부모-자식 관계를 Git branch 분기점으로 재현
- **Merge 재현**: Stream 간 integration을 Git merge commit으로 변환
- **실시간 동기화**: P4 Trigger + Redis 이벤트 기반 (폴링 fallback)
- **초기 히스토리 Import**: git fast-import를 활용한 대량 히스토리 일괄 변환
- **Git LFS 지원**: 바이너리 에셋 자동 LFS 변환 (확장자/용량 기반)
- **상시 전환 준비**: 임의의 시점에 P4 -> Git 컷오버 가능
- **무결성 검증**: P4/Git 간 파일 내용 주기적 비교 + Circuit Breaker 자동 중단
- **모니터링**: HTTP API(FastAPI) + Slack 알림 (심각도별 채널 분리)
- **장애 복구**: State DB 재구성, Git repo 재초기화, 실패 CL 자동/수동 재시도

## 아키텍처

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
                                           │  ┌─────────────┐    │   (local)      │    │
                                           │  │ Notifier    │    └───────┬────────┘    │
                                           │  │ (Slack)     │            │              │
                                           │  └─────────────┘    ┌──────┴─────────┐    │
                                           │                     │   Git Remote    │    │
                                           │                     └────────────────┘    │
                                           └────────────────────────────────────────────┘
```

## 요구사항

- Docker + Docker Compose

## Quick Start

```bash
# 1. 저장소 클론
git clone https://github.com/breadpack/P4SyncGit.git
cd P4SyncGit

# 2. docker-compose.yml의 environment 섹션을 환경에 맞게 수정
#    또는 .env 파일 생성:
cat > .env << 'EOF'
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
EOF

# 3. 서비스 시작
docker compose up -d

# 4. 초기 히스토리 import
docker compose exec p4gitsync p4gitsync import --stream //YourDepot/main

# 5. 상태 확인
curl http://localhost:8080/api/health
docker compose logs -f p4gitsync
```

## 설정

### 환경변수 (권장)

모든 설정은 `P4GITSYNC_{SECTION}_{KEY}` 형식의 환경변수로 지정합니다.
`docker-compose.yml`의 `environment` 섹션에서 직접 설정하거나, `.env` 파일을 사용합니다.

#### 필수 설정

| 환경변수 | 설명 | 예시 |
|----------|------|------|
| `P4GITSYNC_P4_PORT` | P4 서버 주소 | `ssl:p4server:1666` |
| `P4GITSYNC_P4_USER` | P4 서비스 계정 | `p4sync-service` |
| `P4GITSYNC_P4_WORKSPACE` | P4 워크스페이스 | `p4sync-main` |
| `P4GITSYNC_P4_STREAM` | 동기화 대상 P4 Stream | `//YourDepot/main` |
| `P4GITSYNC_GIT_REPO_PATH` | 로컬 Git 저장소 경로 | `/data/git-repo` |
| `P4GITSYNC_GIT_REMOTE_URL` | Git remote URL | `git@github.com:org/repo.git` |
| `P4GITSYNC_STATE_DB_PATH` | SQLite State DB 경로 | `/data/state.db` |

#### 선택 설정

| 환경변수 | 설명 | 기본값 |
|----------|------|--------|
| `P4GITSYNC_GIT_DEFAULT_BRANCH` | Git 기본 브랜치명 | `main` |
| `P4GITSYNC_SYNC_POLLING_INTERVAL_SECONDS` | 폴링 주기 (초) | `30` |
| `P4GITSYNC_SYNC_BATCH_SIZE` | 한 번에 처리할 CL 수 | `50` |
| `P4GITSYNC_API_ENABLED` | HTTP API 활성화 | `false` |
| `P4GITSYNC_API_HOST` | API 바인드 주소 | `127.0.0.1` |
| `P4GITSYNC_API_PORT` | API 포트 | `8080` |
| `P4GITSYNC_REDIS_ENABLED` | Redis 이벤트 시스템 사용 | `false` |
| `P4GITSYNC_REDIS_URL` | Redis 연결 URL | `redis://localhost:6379/0` |
| `P4GITSYNC_SLACK_WEBHOOK_URL` | Slack 알림 Webhook URL | (비활성) |
| `P4GITSYNC_LOGGING_LEVEL` | 로그 레벨 | `INFO` |
| `P4GITSYNC_LOGGING_FORMAT` | 로그 포맷 (`json` / `text`) | `json` |
| `P4GITSYNC_LFS_ENABLED` | Git LFS 활성화 | `false` |
| `P4GITSYNC_INITIAL_IMPORT_USE_FAST_IMPORT` | fast-import 사용 여부 | `true` |
| `P4GITSYNC_STREAM_POLICY_AUTO_DISCOVER` | Stream 자동 감지 | `true` |

전체 설정 항목: [config.example.toml](p4gitsync/config.example.toml)

### config.toml 파일 방식

환경변수 대신 `config.toml` 파일을 마운트할 수도 있습니다.
환경변수가 설정된 경우 config.toml 값을 오버라이드합니다.

```yaml
# docker-compose.yml
services:
  p4gitsync:
    volumes:
      - ./config.toml:/app/config.toml:ro
```

## CLI 명령어

모든 명령어는 Docker 컨테이너 내에서 실행합니다.

### 동기화 실행

```bash
# 동기화 루프 시작 (컨테이너 기본 명령)
docker compose up -d

# 수동으로 동기화 루프 실행
docker compose exec p4gitsync p4gitsync run
```

### 초기 히스토리 Import

P4 Stream의 전체 히스토리를 Git으로 일괄 변환합니다.

```bash
# 설정 파일의 p4.stream 사용
docker compose exec p4gitsync p4gitsync import

# 특정 stream 지정
docker compose exec p4gitsync p4gitsync import --stream //YourDepot/main
```

- git fast-import를 활용한 고속 변환
- 중단 시 자동 resume (`resume_on_restart = true`)
- checkpoint 단위로 진행 상태 저장

### 재동기화

특정 Changelist 범위를 다시 동기화합니다.

```bash
docker compose exec p4gitsync p4gitsync resync --from 12000 --to 12100

# 특정 stream만
docker compose exec p4gitsync p4gitsync resync --from 12000 --to 12100 --stream //YourDepot/main
```

### 복구

```bash
# Git 커밋 로그에서 State DB 재구성
docker compose exec p4gitsync p4gitsync rebuild-state

# Git remote에서 로컬 repo 재초기화
docker compose exec p4gitsync p4gitsync reinit-git --remote git@github.com:org/repo.git
```

### 컷오버 (P4 -> Git 전환)

```bash
# 시뮬레이션 (실제 변경 없음)
docker compose exec p4gitsync p4gitsync cutover --dry-run

# 실행
docker compose exec p4gitsync p4gitsync cutover --execute
```

컷오버 단계:
1. **Freeze Check** - P4 submit 차단 확인
2. **Final Sync** - 잔여 CL 동기화, lag = 0 확인
3. **Integrity Verify** - 전체 파일 무결성 검증
4. **Final Push** - 모든 branch 최종 push
5. **Switch Source** - Git을 공식 소스로 지정

## HTTP API

API 서버 활성화: `P4GITSYNC_API_ENABLED=true`

### 상태 조회

| 엔드포인트 | 메서드 | 설명 |
|-----------|--------|------|
| `/api/health` | GET | 서비스 상태 (200 OK / 503 Unavailable) |
| `/api/status` | GET | Stream별 동기화 상태, 디스크, Git, 성능 지표 |
| `/api/errors` | GET | 미해결 에러 목록 |
| `/api/cutover-readiness` | GET | 컷오버 준비 상태 및 blockers |

### 조작

| 엔드포인트 | 메서드 | 설명 |
|-----------|--------|------|
| `/api/trigger` | POST | 수동 동기화 트리거 (P4 Trigger 대체) |
| `/api/retry/{changelist}` | POST | 실패한 CL 수동 재시도 |

### 응답 예시

```bash
# 컷오버 준비 상태
curl http://localhost:8080/api/cutover-readiness | jq .
```
```json
{
  "ready": true,
  "total_lag": 0,
  "unresolved_errors": 0,
  "integrity_check": { "status": "passed", "mismatches": 0 },
  "blockers": []
}
```

```bash
# 동기화 상태
curl http://localhost:8080/api/status | jq .
```
```json
{
  "streams": [
    {
      "p4_stream": "//YourDepot/main",
      "git_branch": "main",
      "last_synced_cl": 12345,
      "p4_head_cl": 12345,
      "lag": 0
    }
  ],
  "total_processed": 15000,
  "total_lag": 0
}
```

## Slack 알림

심각도별 채널 분리를 지원합니다.

| 레벨 | 조건 | 예시 |
|------|------|------|
| ERROR | CL 3회 연속 실패, P4/Git 연결 실패, 무결성 실패, 디스크 풀/OOM | 즉시 대응 필요 |
| WARN | 동기화 5분 지연, 큐 100건 초과, 디스크 부족, 침묵 장애 | 주의 관찰 |
| INFO | 컷오버 상태 변경, 신규 stream 감지, 일일 리포트(09:00) | 정보성 |

설정:
```
P4GITSYNC_SLACK_ALERTS_WEBHOOK_URL=https://hooks.slack.com/services/...
P4GITSYNC_SLACK_WARNINGS_WEBHOOK_URL=https://hooks.slack.com/services/...
P4GITSYNC_SLACK_INFO_WEBHOOK_URL=https://hooks.slack.com/services/...
```

## 개발 환경

P4 서버, Redis, Gitea(Git 서버)가 포함된 올인원 개발 환경:

```bash
docker compose -f docker-compose.dev.yml up -d
```

| 서비스 | 포트 | 용도 |
|--------|------|------|
| p4gitsync | 8080 | 동기화 서비스 API |
| p4d | 1666 | Perforce Helix Core |
| redis | 6379 | 이벤트 메시지 큐 |
| git-server (Gitea) | 3000, 2222 | Git 서버 (웹 UI / SSH) |

## 프로젝트 구조

```
P4SyncGit/
├── docker-compose.yml              # 프로덕션 (서비스 + Redis)
├── docker-compose.dev.yml          # 개발 (+ P4d + Gitea)
├── .github/workflows/publish.yml   # GHCR 이미지 자동 빌드
├── p4gitsync/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── config.example.toml         # 설정 파일 레퍼런스
│   ├── docs/
│   │   └── runbook.md              # 운영 매뉴얼
│   ├── src/p4gitsync/
│   │   ├── __main__.py             # CLI 엔트리포인트
│   │   ├── config/                 # 설정 모델 + 환경변수 오버라이드
│   │   ├── p4/                     # P4 클라이언트, merge 분석
│   │   ├── git/                    # Git 조작 (pygit2 / CLI)
│   │   ├── state/                  # SQLite State DB
│   │   ├── services/               # 동기화 오케스트레이터, 폴링, 복구
│   │   ├── api/                    # FastAPI 서버
│   │   └── notifications/          # Slack 알림
│   └── tests/
└── Plans/P4GitSync/                # 프로젝트 계획 문서
```

## 기술 스택

| 영역 | 기술 |
|------|------|
| 런타임 | Python 3.12+ |
| P4 연동 | p4python |
| Git 라이브러리 | pygit2 (libgit2) + git CLI fallback |
| State DB | SQLite3 (WAL mode) |
| 메시지 큐 | Redis Streams |
| HTTP API | FastAPI + uvicorn |
| 알림 | slack-sdk |
| 컨테이너 | Docker + Docker Compose |
| CI/CD | GitHub Actions + GHCR |

## Docker 이미지

태그 push 시 GitHub Container Registry에 자동 빌드됩니다.

```bash
docker pull ghcr.io/breadpack/p4gitsync:latest
docker pull ghcr.io/breadpack/p4gitsync:0.0.1
```

## 문서

| 문서 | 설명 |
|------|------|
| [운영 매뉴얼 (Runbook)](p4gitsync/docs/runbook.md) | 서비스 운영, 장애 대응, 컷오버 절차 |
| [설정 레퍼런스](p4gitsync/config.example.toml) | 전체 설정 항목 및 기본값 |
| [프로젝트 계획](Plans/P4GitSync/README.md) | Phase 1~3 구현 계획 |
| [아키텍처](Plans/P4GitSync/Ref-Architecture.md) | 시스템 아키텍처 및 컴포넌트 상세 |
| [P4-Git 매핑](Plans/P4GitSync/Ref-P4GitMapping.md) | P4 <-> Git 개념 매핑, 한계 정리 |
| [기술 스택](Plans/P4GitSync/Ref-TechStack.md) | 기술 선정 근거 |

## License

Private - All rights reserved.
