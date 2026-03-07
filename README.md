# P4GitSync

Perforce Stream -> Git 실시간 동기화 서비스.
P4 submit 이벤트를 감지하여 Git commit으로 변환하고, 최종적으로 P4에서 Git으로 완전 전환하기 위한 마이그레이션 시스템.

## 요구사항

- Docker + Docker Compose

## Quick Start

```bash
# 1. 저장소 클론
git clone https://github.com/breadpack/P4SyncGit.git
cd P4SyncGit

# 2. 환경변수 설정
#    docker-compose.yml의 environment 섹션을 환경에 맞게 수정
#    또는 .env 파일 생성:
cat > .env << 'EOF'
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
EOF

# 3. 서비스 시작
docker compose up -d

# 4. 상태 확인
docker compose logs -f p4gitsync
curl http://localhost:8080/api/health
```

## 설정

모든 설정은 환경변수(`P4GITSYNC_{SECTION}_{KEY}`)로 지정합니다.
`config.toml` 파일을 마운트하여 기본값을 설정하고, 환경변수로 오버라이드할 수도 있습니다.

### 환경변수 예시

| 환경변수 | 설명 | 기본값 |
|----------|------|--------|
| `P4GITSYNC_P4_PORT` | P4 서버 주소 | - |
| `P4GITSYNC_P4_USER` | P4 사용자 | - |
| `P4GITSYNC_P4_STREAM` | P4 Stream 경로 | - |
| `P4GITSYNC_GIT_REPO_PATH` | Git 저장소 경로 | - |
| `P4GITSYNC_GIT_REMOTE_URL` | Git remote URL | - |
| `P4GITSYNC_STATE_DB_PATH` | State DB 경로 | - |
| `P4GITSYNC_REDIS_ENABLED` | Redis 사용 여부 | `false` |
| `P4GITSYNC_SLACK_WEBHOOK_URL` | Slack Webhook URL | - |
| `P4GITSYNC_LOGGING_LEVEL` | 로그 레벨 | `INFO` |

전체 설정 항목은 [config.example.toml](p4gitsync/config.example.toml) 참조.

### config.toml 마운트 방식

```yaml
# docker-compose.yml
services:
  p4gitsync:
    volumes:
      - ./p4gitsync/config.toml:/app/config.toml:ro
```

## CLI 명령어

```bash
# 동기화 루프 실행 (기본)
docker compose exec p4gitsync p4gitsync run

# 초기 히스토리 import
docker compose exec p4gitsync p4gitsync import --stream //Depot/main

# 특정 CL 범위 재동기화
docker compose exec p4gitsync p4gitsync resync --from 12000 --to 12100

# State DB 재구성
docker compose exec p4gitsync p4gitsync rebuild-state

# Git repo 재초기화
docker compose exec p4gitsync p4gitsync reinit-git --remote git@github.com:org/repo.git

# 컷오버 시뮬레이션
docker compose exec p4gitsync p4gitsync cutover --dry-run

# 컷오버 실행
docker compose exec p4gitsync p4gitsync cutover --execute
```

## 개발 환경

P4 서버, Redis, Gitea가 포함된 개발 환경:

```bash
docker compose -f docker-compose.dev.yml up -d
```

## API

| 엔드포인트 | 메서드 | 설명 |
|-----------|--------|------|
| `/api/health` | GET | 서비스 상태 |
| `/api/status` | GET | Stream별 동기화 상태 |
| `/api/errors` | GET | 미해결 에러 목록 |
| `/api/cutover-readiness` | GET | 컷오버 준비 상태 |
| `/api/trigger` | POST | 수동 동기화 트리거 |
| `/api/retry/{changelist}` | POST | 실패 CL 재시도 |

## 문서

- [운영 매뉴얼 (Runbook)](p4gitsync/docs/runbook.md)
- [프로젝트 계획](Plans/P4GitSync/README.md)
