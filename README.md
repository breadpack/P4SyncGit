# P4GitSync

Perforce(Helix Core) Stream의 전체 히스토리를 Git에 정확히 재현하고, 실시간으로 동기화하여 P4에서 Git으로 완전 전환하기 위한 마이그레이션 시스템.

## 주요 기능

- **Changelist -> Commit 1:1 매핑**: 메타데이터(작성자, 날짜, 설명) 완전 보존
- **양방향 동기화**: P4→Git, Git→P4, branch별 방향 설정 가능
- **다중 Stream 동기화**: P4 Stream 부모-자식 관계를 Git branch 분기점으로 재현
- **Merge 재현**: Stream 간 integration을 Git merge commit으로 변환
- **실시간 동기화**: P4 Trigger + Redis 이벤트 기반 (폴링 fallback)
- **충돌 자동 감지**: 양방향 충돌 시 Git branch로 분리, merge로 해결
- **초기 히스토리 Import**: git fast-import를 활용한 대량 히스토리 일괄 변환
- **Git LFS 지원**: 바이너리 에셋 자동 LFS 변환 (확장자/용량 기반)
- **Bare Repository 지원**: remote 없이 로컬 bare repo에 직접 동기화
- **사용자 매핑 플러그인**: Python 스크립트로 조직별 P4 계정 규칙 대응
- **무결성 검증**: P4/Git 간 파일 내용 주기적 비교 + Circuit Breaker 자동 중단
- **컷오버**: P4→Git 전환 5단계 프로세스
- **모니터링**: HTTP API(FastAPI) + Slack 알림 (심각도별 채널 분리)
- **장애 복구**: State DB 재구성, Git repo 재초기화, 실패 CL 자동/수동 재시도

## 아키텍처

```
┌──────────┐                              ┌──────────────────────────────────────┐
│ P4 Server│─── P4→Git ──────────────────>│         SyncOrchestrator             │
│          │<── Git→P4 ──────────────────-│                                      │
└──────────┘                              │  P4Client ↔ CommitBuilder → Git Repo │
                                          │  GitChangeDetector → P4Submitter     │
┌──────────┐                              │  ConflictDetector                    │
│ Git      │<── push ────────────────────-│  StateStore (SQLite)                 │
│ Remote   │─── fetch ───────────────────>│  API Server / Slack Notifier         │
└──────────┘                              └──────────────────────────────────────┘
```

## Quick Start

### 원클릭 설치

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/breadpack/P4SyncGit/master/deploy/quickstart.sh | bash

# Windows (PowerShell)
irm https://raw.githubusercontent.com/breadpack/P4SyncGit/master/deploy/quickstart.ps1 | iex
```

실행하면 `p4gitsync/` 폴더에 설정 파일이 생성됩니다. 설정을 수정한 뒤 시작하세요:

```bash
cd p4gitsync
# 1. config.toml 수정 — P4 서버, stream, workspace
# 2. user_mapper.py 수정 — workspace 패턴, 이메일 도메인
docker compose up -d
```

### Docker Compose (수동)

```bash
git clone https://github.com/breadpack/P4SyncGit.git
cd P4SyncGit/deploy

# config.toml, user_mapper.py 수정
docker compose build
docker compose up -d

# 초기 히스토리 import (최초 1회)
docker compose exec p4gitsync p4gitsync import --stream //YourDepot/main

# 상태 확인
curl http://localhost:8080/api/health
```

### pip 설치

```bash
cd P4SyncGit/p4gitsync
pip install .
p4gitsync --config config.toml run
```

### 바이너리 빌드

```bash
cd P4SyncGit/p4gitsync

# Windows
build.cmd     # → dist\p4gitsync.exe

# Linux / macOS
./build.sh    # → dist/p4gitsync
```

빌드 결과물 + `config.toml` + `user_mapper.py`만 배포하면 됩니다. 실행 환경에 git CLI만 필요합니다.

```bash
./p4gitsync --config config.toml run
```

## 설정

### config.toml

```toml
[p4]
port = "p4server02:1666"
user = "CODE"
workspace = "CODE_p4gitsync.read"
stream = "//YourDepot/main"
submit_workspace = "CODE_p4gitsync.sync"   # 역방향 submit용
submit_as_user = false                     # 공유 계정 환경

[git]
repo_path = "/data/git-repo"
remote_url = "git@github.com:org/repo.git" # 비워두면 로컬 전용
default_branch = "main"
bare = false                               # true: bare repository
watch_remote = "origin"                    # 역방향 fetch 대상
reverse_sync_interval_seconds = 30

[state]
db_path = "/data/state.db"

[sync]
polling_interval_seconds = 30
batch_size = 50

[api]
enabled = true
host = "0.0.0.0"
port = 8080

[user_mapping]
script = "/app/user_mapper.py"             # 사용자 매핑 플러그인

# 양방향 동기화 (branch별 방향 설정)
[[stream_policy.sync_directions]]
stream = "//YourDepot/main"
branch = "main"
direction = "bidirectional"                # p4_to_git | git_to_p4 | bidirectional
```

### 환경변수

모든 설정은 `P4GITSYNC_{SECTION}_{KEY}` 환경변수로도 지정 가능합니다.

```bash
P4GITSYNC_P4_PORT=p4server02:1666
P4GITSYNC_P4_USER=CODE
P4GITSYNC_GIT_REPO_PATH=/data/git-repo
P4GITSYNC_STATE_DB_PATH=/data/state.db
P4GITSYNC_API_ENABLED=true
```

전체 설정 항목: [설정 레퍼런스](Documents/02-Configuration.md)

### 사용자 매핑 플러그인

P4 공유 계정 환경 등 조직별 규칙을 Python 스크립트로 정의합니다.

```python
# user_mapper.py
import re

def p4_to_git(changelist_info: dict) -> dict:
    """P4 changelist → Git author."""
    ws = changelist_info["workspace"]          # "CODE_kwonsanggoo.dev"
    m = re.match(r"CODE_(.+)\.dev", ws)
    user_id = m.group(1) if m else "unknown"

    desc = changelist_info["description"]      # "[권상구] 번역 개선"
    m2 = re.match(r"\[(.+?)\]", desc)
    name = m2.group(1) if m2 else user_id

    return {"name": name, "email": f"{user_id}@company.com"}

def git_to_p4(commit_info: dict) -> dict:
    """Git commit → P4 submit 정보."""
    email = commit_info["author_email"]
    user_id = email.split("@")[0]
    name = commit_info["author_name"]

    return {
        "user": "CODE",
        "workspace": f"CODE_{user_id}.sync",
        "description": f"[{name}] {commit_info['message']}",
    }
```

스크립트 미설정 시 `user_mappings` DB 테이블 기반으로 동작합니다.

## CLI 명령어

| 명령 | 설명 |
|------|------|
| `p4gitsync run` | 동기화 루프 실행 (기본) |
| `p4gitsync import [--stream]` | 초기 히스토리 import |
| `p4gitsync resync --from N --to M` | CL 범위 재동기화 |
| `p4gitsync rebuild-state` | Git log에서 State DB 재구성 |
| `p4gitsync reinit-git --remote URL` | Git repo 재초기화 |
| `p4gitsync cutover --dry-run/--execute` | P4→Git 컷오버 |

Docker 환경에서는 `docker compose exec p4gitsync` 접두사를 붙여 실행합니다.

## 양방향 동기화

### 동기화 방향

| direction | P4→Git | Git→P4 | 용도 |
|-----------|--------|--------|------|
| `p4_to_git` | O | X | 단방향 (기본) |
| `git_to_p4` | X | O | Git 주도 |
| `bidirectional` | O | O | 과도기 병행 |

### 루프 방지

- P4→Git commit에 `P4CL: 12345` trailer 삽입 → 역방향에서 스킵
- Git→P4 changelist에 `GitCommit: abc123` trailer 삽입 → 순방향에서 스킵

### 충돌 처리

동일 파일이 양쪽에서 동시에 변경되면:

1. 해당 branch 동기화 자동 중단
2. P4 변경사항으로 `conflict/{branch}/CL{number}` Git branch 생성
3. Slack ERROR 알림
4. 사용자가 Git에서 merge로 해결 후 충돌 branch 삭제
5. 삭제 감지 → 동기화 자동 재개

## HTTP API

| 엔드포인트 | 메서드 | 설명 |
|-----------|--------|------|
| `/api/health` | GET | 서비스 상태 |
| `/api/status` | GET | Stream별 동기화 현황 |
| `/api/errors` | GET | 미해결 에러 목록 |
| `/api/conflicts` | GET | 양방향 충돌 상태 |
| `/api/cutover-readiness` | GET | 컷오버 준비 상태 |
| `/api/trigger` | POST | 수동 동기화 트리거 |
| `/api/retry/{changelist}` | POST | 실패 CL 재시도 |

## Slack 알림

| 레벨 | 조건 |
|------|------|
| ERROR | CL 반복 실패, 연결 실패, 무결성 실패, 양방향 충돌 |
| WARN | 동기화 지연, 큐 과적, 디스크 부족, 침묵 장애 |
| INFO | 컷오버 상태 변경, 신규 stream 감지, 충돌 해결, 일일 리포트 |

## 프로젝트 구조

```
P4SyncGit/
├── deploy/                            # 배포 파일
│   ├── quickstart.sh / quickstart.ps1 # 원클릭 설치
│   ├── docker-compose.yml
│   ├── config.toml                    # 설정 템플릿
│   ├── user_mapper.py                 # 사용자 매핑 플러그인 예시
│   ├── install.cmd / install.sh       # 설치 스크립트
│   └── status.cmd / stop.cmd          # 운영 스크립트
├── docker-compose.yml                 # 프로덕션 (서비스 + Redis)
├── docker-compose.dev.yml             # 개발 (+ P4d + Gitea)
├── p4gitsync/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── p4gitsync.spec                 # PyInstaller 빌드 설정
│   ├── build.cmd / build.sh           # 바이너리 빌드 스크립트
│   ├── src/p4gitsync/
│   │   ├── __main__.py                # CLI 엔트리포인트
│   │   ├── config/                    # 설정 모델
│   │   ├── p4/                        # P4 클라이언트 + submitter
│   │   ├── git/                       # Git 조작 + change detector
│   │   ├── state/                     # SQLite State DB
│   │   ├── services/                  # 동기화, 충돌 감지, 사용자 매핑
│   │   ├── api/                       # FastAPI 서버
│   │   └── notifications/             # Slack 알림
│   └── tests/
└── Documents/                         # 상세 문서
    ├── 01-Overview-Architecture.md
    ├── 02-Configuration.md
    ├── 03-CLI-Usage.md
    ├── 04-API-Reference.md
    ├── 05-Sync-Mechanism.md
    ├── 06-Operations-Guide.md
    └── 07-Deployment.md
```

## 기술 스택

| 영역 | 기술 |
|------|------|
| 런타임 | Python 3.12+ |
| P4 연동 | p4python |
| Git | pygit2 (libgit2) + git CLI fallback |
| State DB | SQLite3 (WAL mode) |
| 메시지 큐 | Redis Streams (선택) |
| HTTP API | FastAPI + uvicorn |
| 알림 | slack-sdk |
| 컨테이너 | Docker + Docker Compose |
| CI/CD | GitHub Actions + GHCR |

## 개발 환경

P4 서버, Redis, Gitea가 포함된 올인원 개발 환경:

```bash
docker compose -f docker-compose.dev.yml up -d
```

| 서비스 | 포트 | 용도 |
|--------|------|------|
| p4gitsync | 8080 | 동기화 서비스 API |
| p4d | 1666 | Perforce Helix Core |
| redis | 6379 | 이벤트 메시지 큐 |
| git-server (Gitea) | 3000, 2222 | Git 서버 |

## 문서

| 문서 | 설명 |
|------|------|
| [개요 및 아키텍처](Documents/01-Overview-Architecture.md) | 시스템 구조, 컴포넌트, 데이터 흐름 |
| [설정 레퍼런스](Documents/02-Configuration.md) | 모든 설정 항목 상세 |
| [CLI 사용법](Documents/03-CLI-Usage.md) | 모든 명령어와 옵션 |
| [API 레퍼런스](Documents/04-API-Reference.md) | HTTP 엔드포인트 상세 |
| [동기화 메커니즘](Documents/05-Sync-Mechanism.md) | 단방향/양방향, 이벤트, merge |
| [운영 가이드](Documents/06-Operations-Guide.md) | 모니터링, 복구, 컷오버 |
| [배포 가이드](Documents/07-Deployment.md) | Docker, pip, 바이너리 |
| [양방향 설계 사양서](Documents/specs/2026-03-25-bidirectional-sync-design.md) | 양방향 동기화 설계 문서 |
| [운영 매뉴얼 (Runbook)](p4gitsync/docs/runbook.md) | 서비스 운영, 장애 대응 |

## License

Private - All rights reserved.
