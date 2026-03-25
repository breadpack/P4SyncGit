#!/bin/bash
# P4GitSync 바이너리 빌드 (Linux/macOS)
# 사전 조건: Python 3.12+, pip install pyinstaller p4python pygit2 fastapi uvicorn slack-sdk redis

set -e

echo "[1/3] 의존성 설치..."
pip install -e ".[dev]" pyinstaller

echo "[2/3] PyInstaller 빌드..."
pyinstaller p4gitsync.spec --clean --noconfirm

echo "[3/3] 빌드 완료!"
echo ""
echo "결과: dist/p4gitsync"
echo ""
echo "사용법:"
echo "  ./dist/p4gitsync --config config.toml run"
echo ""
echo "주의: 실행 환경에 git이 설치되어 있어야 합니다."
