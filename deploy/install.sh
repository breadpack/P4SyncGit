#!/bin/bash
# P4GitSync 원클릭 설치/실행 (Linux/macOS)
# 사전 조건: Docker + Docker Compose 설치

set -e

echo "==================================="
echo " P4GitSync 설치"
echo "==================================="
echo ""

# Docker 확인
if ! command -v docker &> /dev/null; then
    echo "[오류] Docker가 설치되어 있지 않습니다."
    echo "       https://docs.docker.com/engine/install/ 에서 설치하세요."
    exit 1
fi

# 설정 파일 확인
if [ ! -f config.toml ]; then
    echo "[오류] config.toml 파일이 없습니다."
    echo "       config.toml 을 환경에 맞게 수정한 후 다시 실행하세요."
    exit 1
fi

echo "[1/3] Docker 이미지 빌드 중..."
docker compose build

echo "[2/3] 서비스 시작 중..."
docker compose up -d

echo "[3/3] 상태 확인 중..."
sleep 5
if curl -sf http://localhost:8080/api/health > /dev/null 2>&1; then
    echo "  API 서버 정상 동작 확인"
else
    echo "  [경고] API 서버가 아직 시작되지 않았을 수 있습니다."
    echo "         잠시 후 다시 확인: curl http://localhost:8080/api/health"
fi

echo ""
echo "==================================="
echo " 설치 완료!"
echo "==================================="
echo ""
echo " 상태 확인:  curl http://localhost:8080/api/health"
echo " 로그 보기:  docker compose logs -f p4gitsync"
echo " 서비스 중지: docker compose down"
echo ""
echo " 초기 import (최초 1회):"
echo "   docker compose exec p4gitsync p4gitsync import"
echo ""
