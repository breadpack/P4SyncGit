# P4GitSync 배포 가이드

## 요구사항

- Docker + Docker Compose (프로덕션)
- Python 3.12+ (로컬 개발)
- P4 서버 접근 권한 (서비스 계정)
- Git remote 접근 권한 (SSH 키 또는 토큰)

---

## Docker 배포 (권장)

### 프로덕션 환경

```bash
# 1. 저장소 클론
git clone https://github.com/breadpack/P4SyncGit.git
cd P4SyncGit

# 2. 환경 설정 (.env 파일 또는 docker-compose.yml 환경변수)
cat > .env << 'EOF'
P4GITSYNC_P4_PORT=ssl:p4server:1666
P4GITSYNC_P4_USER=p4sync-service
P4GITSYNC_P4_WORKSPACE=p4sync-main
P4GITSYNC_P4_STREAM=//YourDepot/main
P4GITSYNC_GIT_REPO_PATH=/data/git-repo
P4GITSYNC_GIT_REMOTE_URL=git@github.com:org/repo.git
P4GITSYNC_STATE_DB_PATH=/data/state.db
P4GITSYNC_API_ENABLED=true
P4GITSYNC_API_HOST=0.0.0.0
P4GITSYNC_REDIS_ENABLED=true
P4GITSYNC_REDIS_URL=redis://redis:6379/0
P4GITSYNC_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
EOF

# 3. 서비스 시작
docker compose up -d

# 4. 초기 히스토리 import
docker compose exec p4gitsync p4gitsync import --stream //YourDepot/main

# 5. 상태 확인
curl http://localhost:8080/api/health
docker compose logs -f p4gitsync
```

### docker-compose.yml 구성

프로덕션 구성은 p4gitsync 서비스 + Redis로 구성됩니다.

| 서비스 | 이미지 | 포트 | 역할 |
|--------|--------|------|------|
| p4gitsync | ghcr.io/breadpack/p4gitsync:latest | 8080 | 동기화 서비스 |
| redis | redis:7-alpine | 6379 | 이벤트 메시지 큐 |

### config.toml 마운트 방식

환경변수 대신 config.toml 파일을 직접 마운트할 수도 있습니다.

```yaml
services:
  p4gitsync:
    volumes:
      - ./config.toml:/app/config.toml:ro
      - sync-data:/data
```

---

## 개발 환경 (올인원)

P4 서버, Redis, Gitea(Git 서버)가 포함된 올인원 개발 환경입니다.

```bash
docker compose -f docker-compose.dev.yml up -d
```

| 서비스 | 이미지 | 포트 | 용도 |
|--------|--------|------|------|
| p4gitsync | p4gitsync:dev (로컬 빌드) | 8080 | 동기화 서비스 API |
| p4d | perforce/helix-core:latest | 1666 | Perforce Helix Core |
| redis | redis:7-alpine | 6379 | 이벤트 메시지 큐 |
| git-server | gitea/gitea:latest | 3000, 2222 | Git 서버 (웹 UI / SSH) |

### 개발 환경 기본 설정

- P4: `admin@localhost:1666`, stream `//depot/main`
- Git: `http://git-server:3000/p4sync/repo.git`
- 로그 레벨: DEBUG, 포맷: text

---

## 로컬 설치 (개발용)

```bash
cd p4gitsync
pip install -e ".[dev]"

# 실행
p4gitsync --config config.toml run

# 테스트
pytest
pytest tests/test_state_store.py
pytest -k "test_function_name"

# 린트
ruff check src/ tests/
ruff format src/ tests/
```

---

## Docker 이미지

### 이미지 구조

2단계 빌드 (multi-stage):

1. **Builder**: python:3.12-slim + build-essential, libssl-dev, pkg-config, git
2. **Runtime**: python:3.12-slim + git, git-lfs, curl

### 이미지 정보

- **사용자**: `p4sync` (non-root)
- **작업 디렉토리**: `/app`
- **데이터 볼륨**: `/data`
- **포트**: 8080
- **Healthcheck**: `GET /api/health` (30초 간격, 5초 타임아웃, 3회 재시도)
- **엔트리포인트**: `p4gitsync --config /app/config.toml run`

### 이미지 빌드

```bash
# 로컬 빌드
cd p4gitsync
docker build -t p4gitsync:local .

# GHCR에서 풀
docker pull ghcr.io/breadpack/p4gitsync:latest
docker pull ghcr.io/breadpack/p4gitsync:0.0.1
```

---

## CI/CD (GitHub Actions)

### 자동 빌드 트리거

- **Tag push**: `v*` (예: `v0.1.0`)
- **Pull Request**: master 대상

### 파이프라인

1. GitHub Container Registry (ghcr.io) 로그인
2. Docker 이미지 빌드 (GHA 캐시 활용)
3. 이미지 태깅:
   - `ghcr.io/{owner}/p4gitsync:latest`
   - `ghcr.io/{owner}/p4gitsync:{version}` (semver)
   - `ghcr.io/{owner}/p4gitsync:{sha}` (commit hash)
4. GHCR에 push

---

## SSH 키 설정 (Git push용)

Docker 환경에서 SSH 기반 Git remote를 사용하려면:

```yaml
services:
  p4gitsync:
    volumes:
      - ./ssh-keys:/home/p4sync/.ssh:ro
```

SSH 키 준비:
```bash
mkdir ssh-keys
cp id_rsa ssh-keys/
cat > ssh-keys/config << 'EOF'
Host github.com
  StrictHostKeyChecking no
  IdentityFile /home/p4sync/.ssh/id_rsa
EOF
chmod 600 ssh-keys/id_rsa
```

---

## 볼륨 및 데이터 관리

### /data 볼륨 구성

| 경로 | 설명 |
|------|------|
| `/data/git-repo` | Git 리포지토리 (또는 bare repo) |
| `/data/state.db` | SQLite State DB |

### 백업 권장

- **State DB**: 정기적 `.backup` 수행 (DatabaseBackup 컴포넌트가 자동 수행)
- **Git repo**: remote에 push된 상태라면 remote이 백업 역할

### 디스크 용량 주의

- Git 리포지토리는 히스토리가 쌓이면서 지속적으로 증가
- `git gc`가 자동으로 실행되지만 (git_gc_interval 기반), 디스크 모니터링 필요
- LFS 사용 시 바이너리 파일은 LFS 서버에 저장되므로 로컬 용량 절약
