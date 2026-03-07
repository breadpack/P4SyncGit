# 시스템 아키텍처

## 기술 스택

| 영역 | 기술 |
|------|------|
| 런타임 | Python 3.12+ |
| 데몬 관리 | systemd / supervisor |
| Git 라이브러리 | pygit2 (libgit2 바인딩) |
| State DB | Python 내장 sqlite3 |
| 메시지 큐 | Redis Streams |
| HTTP API | FastAPI (uvicorn) |
| 로깅 | Python 표준 logging |
| 알림 | slack-sdk |

## 전체 구조

```
┌──────────────┐     ┌───────────────┐     ┌────────────────┐     ┌──────────────┐
│  P4 Server   │────>│   Trigger     │────>│  Redis Streams │────>│   Worker     │
│  (submit)    │     │  (bash+curl)  │     │  (메시지 큐)    │     │  Service     │
└──────────────┘     └───────────────┘     └────────────────┘     └──────┬───────┘
                                                                         │
                                                                    ┌────┴────┐
                                                                    │         │
                                                               ┌────▼───┐ ┌──▼───────┐
                                                               │ SQLite │ │ Git Repo │
                                                               │ State  │ │ (local)  │
                                                               └────────┘ └────┬─────┘
                                                                               │
                                                                          ┌────▼─────┐
                                                                          │ Git      │
                                                                          │ Remote   │
                                                                          └──────────┘
```

## 컴포넌트 상세

### 1. P4 Trigger

P4 서버의 `change-commit` 트리거로 등록.

```bash
#!/bin/bash
# p4-git-sync-trigger.sh
# 인자: $1=changelist, $2=user, $3=workspace

WEBHOOK_URL="http://sync-worker:8080/api/trigger"

# 보안: secret은 환경변수 또는 파일 참조로 관리 (스크립트에 하드코딩 금지)
# 방법 1) 환경변수 참조
SECRET="${P4_TRIGGER_SECRET}"
# 방법 2) 파일 참조 (환경변수가 없을 경우)
if [ -z "$SECRET" ] && [ -f /etc/p4sync/trigger-secret ]; then
  SECRET=$(cat /etc/p4sync/trigger-secret)
fi

if [ -z "$SECRET" ]; then
  echo "ERROR: P4_TRIGGER_SECRET not configured" >&2
  exit 0  # P4 submit은 블로킹하지 않음
fi

# 비동기 전송 (background) -- P4 submit 블로킹 방지
curl -s -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Trigger-Secret: $SECRET" \
  -d "{\"changelist\": $1, \"user\": \"$2\"}" \
  --max-time 3 &

exit 0
```

P4 triggers 테이블 등록:
```
Triggers:
  git-sync change-commit //...  "/path/to/p4-git-sync-trigger.sh %changelist% %user% %client%"
```

> **대안**: 트리거 없이 Worker가 `p4 changes` 폴링도 가능 (Phase 1에서는 폴링으로 시작 가능).

#### Trigger Heartbeat 메커니즘

Trigger가 정상 동작하는지 확인하기 위한 heartbeat 구성:

```
Worker 측 heartbeat 감시:
  1. Worker는 마지막으로 Trigger 이벤트를 수신한 시각을 기록
  2. 설정된 임계값(기본 30분) 동안 이벤트가 없으면 경고 알림 발행
  3. P4 서버에 최근 submit이 있었는지 `p4 changes -m1` 으로 교차 확인
  4. Trigger 장애 판정 시 폴링 모드로 자동 전환 (fallback)

Trigger 측 heartbeat (선택):
  - cron으로 주기적(5분)으로 Worker의 /api/health 엔드포인트 호출
  - Worker가 응답하지 않으면 별도 알림 경로(이메일/Slack)로 통보
```

### 2. Webhook Receiver (Worker 내장, FastAPI)

```
POST /api/trigger
Body: { "changelist": 12345, "user": "kwonsanggoo" }
Header: X-Trigger-Secret: <token>

처리:
1. 시크릿 검증 (환경변수 P4_TRIGGER_SECRET과 비교)
2. Redis Stream에 메시지 발행 (XADD)
3. 즉시 202 Accepted 반환

GET /api/health
  - Worker 상태 및 마지막 Trigger 수신 시각 반환
```

### 3. Redis Stream 구조

```
Stream Key: p4sync:events

Message Format:
  changelist: "12345"
  user: "kwonsanggoo"
  timestamp: "1709712000"

Consumer Group: p4sync-workers
Consumer: worker-01
```

#### Redis 영속성 설정

메시지 유실 방지를 위해 AOF(Append Only File) 영속성을 활성화한다.

```
# redis.conf 권장 설정

# AOF 활성화
appendonly yes
appendfilename "p4sync-aof.aof"

# fsync 정책: everysec (성능과 안전성 균형)
appendfsync everysec

# AOF rewrite 임계값
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb

# RDB 스냅샷 (AOF 보완용, 빠른 복구 시 활용)
save 900 1
save 300 10
save 60 10000
```

> **참고**: AOF `everysec` 설정 시 최대 1초분 데이터 유실 가능.
> Worker의 State DB에 처리 상태가 기록되므로 유실된 이벤트는 폴링으로 보완 가능.

### 4. Worker Service

Python `systemd`/`supervisor` 기반 데몬 프로세스.

```
┌─────────────────────────────────────────────────────┐
│              Worker Service (Python)                 │
│                                                      │
│  ┌──────────────────┐  ┌─────────────────────────┐  │
│  │ event_consumer   │  │ sync_processor          │  │
│  │ (Redis XREADGROUP│─>│                         │  │
│  │  asyncio task)   │  │ ┌─────────────────────┐ │  │
│  └──────────────────┘  │ │ p4_client           │ │  │
│                         │ │ - describe()        │ │  │
│  ┌──────────────────┐  │ │ - filelog()         │ │  │
│  │ stream_watcher   │  │ │ - print_file()     │ │  │
│  │ (주기적 폴링     │  │ │ - sync()           │ │  │
│  │  asyncio task)   │  │ └─────────────────────┘ │  │
│  └──────────────────┘  │ ┌─────────────────────┐ │  │
│                         │ │ git_operator        │ │  │
│  ┌──────────────────┐  │ │ (pygit2)            │ │  │
│  │ api_server       │  │ │ - commit_tree()     │ │  │
│  │ (FastAPI/uvicorn │  │ │ - update_ref()      │ │  │
│  │  상태 조회)       │  │ │ - push()           │ │  │
│  └──────────────────┘  │ └─────────────────────┘ │  │
│                         │ ┌─────────────────────┐ │  │
│  ┌──────────────────┐  │ │ merge_analyzer      │ │  │
│  │ state_store      │<─│ │ - integration       │ │  │
│  │ (sqlite3)        │  │ │   record 분석       │ │  │
│  └──────────────────┘  │ └─────────────────────┘ │  │
│                         └─────────────────────────┘  │
│  ┌──────────────────┐                                │
│  │ notifier         │                                │
│  │ (slack-sdk)      │                                │
│  └──────────────────┘                                │
└─────────────────────────────────────────────────────┘
```

#### Python 모듈 구조

```
p4gitsync/
├── pyproject.toml                 # 프로젝트 메타데이터, 의존성
├── config.toml                    # 설정 파일
├── src/
│   └── p4gitsync/
│       ├── __init__.py
│       ├── __main__.py            # 엔트리포인트 (asyncio 루프)
│       ├── services/
│       │   ├── __init__.py
│       │   ├── sync_orchestrator.py   # 동기화 오케스트레이터
│       │   ├── changelist_poller.py   # P4 changelist 폴링
│       │   ├── event_consumer.py      # Redis Stream 소비자 (asyncio)
│       │   └── commit_builder.py      # Git commit 생성
│       ├── p4/
│       │   ├── __init__.py
│       │   ├── p4_client.py           # p4python API 래퍼
│       │   ├── p4_change_info.py      # Changelist 정보 모델
│       │   ├── p4_file_action.py      # 파일 액션 모델
│       │   └── merge_analyzer.py      # P4 integration record 분석
│       ├── git/
│       │   ├── __init__.py
│       │   ├── git_operator.py        # Git 조작 Protocol
│       │   ├── pygit2_git_operator.py # pygit2 기반 구현
│       │   ├── git_cli_operator.py    # Git CLI 기반 구현 (fallback)
│       │   └── commit_metadata.py     # Commit 메타데이터 모델
│       ├── state/
│       │   ├── __init__.py
│       │   ├── state_store.py         # SQLite 상태 관리
│       │   └── migrations/            # DB 스키마 마이그레이션
│       ├── api/
│       │   ├── __init__.py
│       │   └── api_server.py          # FastAPI 웹훅/상태 API
│       ├── notifications/
│       │   ├── __init__.py
│       │   └── notifier.py            # slack-sdk 알림
│       └── config/
│           ├── __init__.py
│           ├── sync_config.py         # 설정 모델 (dataclass)
│           └── logging_config.py      # Python logging 설정
└── tests/
```

#### systemd 서비스 파일 예시

> **Linux 전용**: 아래 systemd 서비스 파일은 Linux 환경에서만 사용 가능하다.
> Windows 환경에서는 다음 대안을 사용한다:
> - **NSSM (Non-Sucking Service Manager)**: `nssm install p4sync-worker "C:\p4sync\venv\Scripts\python.exe" "-m" "p4gitsync"` 로 Windows Service 등록
> - **Windows Service (pywin32)**: `win32serviceutil`을 사용하여 Python 스크립트를 Windows Service로 직접 구현
> - **Task Scheduler**: 간이 운영 시 Windows 작업 스케줄러로 프로세스 시작/감시 가능 (권장하지 않음)

```ini
# /etc/systemd/system/p4sync-worker.service
# [Linux 전용] Windows에서는 NSSM 또는 Windows Service(pywin32) 사용
[Unit]
Description=P4 Git Sync Worker
After=network.target redis.service

[Service]
Type=simple
User=p4sync
Group=p4sync
WorkingDirectory=/opt/p4sync
ExecStart=/opt/p4sync/venv/bin/python -m p4gitsync
Restart=on-failure
RestartSec=10
EnvironmentFile=/etc/p4sync/env

[Install]
WantedBy=multi-user.target
```

### 5. P4 Workspace 구성

Worker 전용 P4 워크스페이스 필요. Stream별로 별도 워크스페이스 사용.

```
Workspace: p4sync-main
  Stream: //depot/main
  Root: /data/p4sync/workspaces/main

Workspace: p4sync-dev
  Stream: //depot/dev
  Root: /data/p4sync/workspaces/dev
```

### 6. Git Repository 구조

```
/data/p4sync/git-repo/          (bare 또는 일반 repo)
  ├── .git/
  ├── refs/heads/
  │   ├── main                  ← //depot/main
  │   ├── dev                   ← //depot/dev
  │   └── feature-x             ← //depot/feature-x
  └── ...
```

> Branch 별 워킹 디렉토리가 필요하므로 `git worktree`를 활용하거나,
> pygit2의 `commit_tree` plumbing으로 워킹 디렉토리 없이 commit 생성 가능.

### 7. SQLite State DB

```sql
-- 향후 다중 프로젝트 확장 시 project_id TEXT 컬럼 추가 가능
CREATE TABLE stream_mapping (
    p4_stream       TEXT PRIMARY KEY,
    git_branch      TEXT NOT NULL,
    parent_stream   TEXT,
    first_changelist INTEGER,              -- stream 최초 CL (분기점 결정에 사용)
    branch_point_sha TEXT,                 -- Git branch가 생성된 parent commit SHA
    last_synced_cl  INTEGER DEFAULT 0,
    last_git_commit TEXT,
    p4_workspace    TEXT,
    is_active       INTEGER DEFAULT 1
);

-- 향후 다중 프로젝트 확장 시 project_id TEXT 컬럼 추가 가능
CREATE TABLE cl_commit_map (
    changelist      INTEGER NOT NULL,
    git_commit_sha  TEXT NOT NULL,
    p4_stream       TEXT NOT NULL,
    git_branch      TEXT NOT NULL,
    has_integration INTEGER DEFAULT 0,
    git_push_status TEXT DEFAULT 'pending',  -- pending / pushed / failed
    created_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (changelist, p4_stream)
);

CREATE INDEX idx_cl_stream ON cl_commit_map(p4_stream, changelist);
CREATE INDEX idx_push_status ON cl_commit_map(git_push_status);

CREATE TABLE user_mapping (
    p4_user     TEXT PRIMARY KEY,
    git_name    TEXT NOT NULL,
    git_email   TEXT NOT NULL
);

CREATE TABLE sync_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    changelist  INTEGER NOT NULL,
    p4_stream   TEXT,
    error_msg   TEXT,
    retry_count INTEGER DEFAULT 0,
    resolved    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);
```

```sql
-- 운영 필수 설정
PRAGMA journal_mode=WAL;          -- 읽기/쓰기 동시성 향상
PRAGMA busy_timeout=5000;         -- 5초 대기 후 SQLITE_BUSY 반환
PRAGMA synchronous=NORMAL;        -- WAL 모드에서 권장
```

#### git_push_status 컬럼 설명

Git commit 생성과 remote push는 별개의 단계이므로 push 상태를 별도로 추적한다.

```
상태 흐름:
  pending  → commit 생성 완료, push 대기
  pushed   → remote push 성공
  failed   → push 실패 (retry 대상)

활용:
  - push 실패 시 재시도 대상 조회: WHERE git_push_status = 'failed'
  - push 미완료 건 모니터링: WHERE git_push_status = 'pending'
  - 서비스 재시작 시 pending/failed 건 일괄 push 재시도
```

### 정합성 보장 메커니즘

Git commit 생성과 State DB 기록 사이에 크래시가 발생하면
양쪽 데이터가 불일치할 수 있다. 이를 방지하기 위한 설계:

```
처리 순서 (CL 단위):
  1. State DB에 pending 레코드 삽입 (status='pending')
  2. Git commit 생성
  3. State DB 레코드를 confirmed로 업데이트 (sha 기록)
  4. Git remote push 실행
  5. State DB의 git_push_status를 pushed로 업데이트

서비스 시작 시 복구:
  1. pending 상태 레코드 조회
  2. Git에 해당 commit이 존재하면 → confirmed로 업데이트
  3. Git에 해당 commit이 없으면 → pending 레코드 삭제, 재처리 대상
  4. git_push_status가 pending/failed인 레코드 → push 재시도
```

## 배포 구성

```
배포 위치: 동기화 전용 서버 또는 기존 인프라의 Docker 컨테이너

필요 자원:
  - CPU: 2 core (p4/git 프로세스 실행)
  - RAM: 2~4 GB (pygit2/libgit2 네이티브 메모리 포함)
  - Disk:
    - 스토리지: SSD 필수 (Git 작업 및 SQLite WAL I/O 성능 보장)
    - p4 print 방식: Git repo 크기 + 임시 파일 공간 (~1GB)
    - p4 sync 방식: P4 워크스페이스 크기 x stream 수 + Git repo
    - State DB: CL 10만 개 기준 약 50~100MB
    - 최소 여유 공간: 10GB (알림 임계값)
  - Network: P4 서버 + Redis + Git remote 접근

환경변수 (/etc/p4sync/env):
  P4PORT=ssl:p4server:1666
  P4USER=p4sync-service
  P4TRUST=(설정 완료)
  P4_TRIGGER_SECRET=<trigger-secret>
  REDIS_CONNECTION=redis:6379
  GIT_REMOTE_URL=git@github.com:org/repo.git
  SQLITE_PATH=/data/p4sync/state.db
  SLACK_BOT_TOKEN=xoxb-...
  SLACK_CHANNEL=#p4sync-alerts

Python 의존성 (requirements.txt):
  pygit2>=1.14.0
  redis>=5.0.0
  fastapi>=0.110.0
  uvicorn>=0.27.0
  slack-sdk>=3.27.0
```
