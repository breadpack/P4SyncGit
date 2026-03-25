#!/bin/bash
# P4GitSync 빠른 설치
# 사용법: curl -fsSL https://raw.githubusercontent.com/breadpack/P4SyncGit/master/deploy/quickstart.sh | bash
set -e

REPO="https://raw.githubusercontent.com/breadpack/P4SyncGit/master"
DIR="p4gitsync"

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║       P4GitSync 빠른 설치        ║"
echo "  ╚══════════════════════════════════╝"
echo ""

# Docker 확인
if ! command -v docker &> /dev/null; then
    echo "✗ Docker가 설치되어 있지 않습니다."
    echo "  https://docs.docker.com/engine/install/"
    exit 1
fi
echo "✓ Docker 확인됨"

# 디렉토리 생성
mkdir -p "$DIR"
cd "$DIR"
echo "✓ 작업 디렉토리: $(pwd)"

# 파일 다운로드
echo ""
echo "파일 다운로드 중..."
curl -fsSL "$REPO/deploy/docker-compose.yml" -o docker-compose.yml
curl -fsSL "$REPO/deploy/config.toml"        -o config.toml
curl -fsSL "$REPO/deploy/user_mapper.py"     -o user_mapper.py
curl -fsSL "$REPO/p4gitsync/Dockerfile"      -o Dockerfile

# Dockerfile 경로 보정 — 단독 빌드를 위해 소스 포함 compose로 교체
cat > docker-compose.yml << 'COMPOSE_EOF'
services:
  p4gitsync:
    image: ghcr.io/breadpack/p4gitsync:latest
    restart: unless-stopped
    volumes:
      - sync-data:/data
      - ./config.toml:/app/config.toml:ro
      - ./user_mapper.py:/app/user_mapper.py:ro
    ports:
      - "8080:8080"

volumes:
  sync-data:
COMPOSE_EOF

echo "✓ docker-compose.yml"
echo "✓ config.toml"
echo "✓ user_mapper.py"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  설치 완료! 다음 단계:"
echo ""
echo "  1. 설정 수정 (필수)"
echo "     config.toml      — P4 서버, stream, workspace"
echo "     user_mapper.py   — workspace 패턴, 이메일 도메인"
echo ""
echo "  2. 서비스 시작"
echo "     cd $DIR"
echo "     docker compose up -d"
echo ""
echo "  3. 초기 import (최초 1회)"
echo "     docker compose exec p4gitsync p4gitsync import"
echo ""
echo "  4. 상태 확인"
echo "     curl http://localhost:8080/api/health"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
