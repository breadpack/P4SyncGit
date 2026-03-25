# P4GitSync 빠른 설치 (Windows PowerShell)
# 사용법: irm https://raw.githubusercontent.com/breadpack/P4SyncGit/master/deploy/quickstart.ps1 | iex

$ErrorActionPreference = "Stop"
$Repo = "https://raw.githubusercontent.com/breadpack/P4SyncGit/master"
$Dir = "p4gitsync"

Write-Host ""
Write-Host "  +==================================+" -ForegroundColor Cyan
Write-Host "  |       P4GitSync 빠른 설치        |" -ForegroundColor Cyan
Write-Host "  +==================================+" -ForegroundColor Cyan
Write-Host ""

# Docker 확인
try {
    docker --version | Out-Null
    Write-Host "[OK] Docker 확인됨" -ForegroundColor Green
} catch {
    Write-Host "[오류] Docker가 설치되어 있지 않습니다." -ForegroundColor Red
    Write-Host "       https://www.docker.com/products/docker-desktop"
    exit 1
}

# 디렉토리 생성
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Set-Location $Dir
Write-Host "[OK] 작업 디렉토리: $(Get-Location)" -ForegroundColor Green

# 파일 다운로드
Write-Host ""
Write-Host "파일 다운로드 중..."
Invoke-WebRequest -Uri "$Repo/deploy/config.toml"    -OutFile "config.toml"
Invoke-WebRequest -Uri "$Repo/deploy/user_mapper.py" -OutFile "user_mapper.py"

# docker-compose.yml 생성 (GHCR 이미지 사용)
@"
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
"@ | Set-Content -Path "docker-compose.yml" -Encoding UTF8

Write-Host "[OK] docker-compose.yml" -ForegroundColor Green
Write-Host "[OK] config.toml" -ForegroundColor Green
Write-Host "[OK] user_mapper.py" -ForegroundColor Green

Write-Host ""
Write-Host "====================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "  설치 완료! 다음 단계:" -ForegroundColor White
Write-Host ""
Write-Host "  1. 설정 수정 (필수)" -ForegroundColor White
Write-Host "     config.toml      - P4 서버, stream, workspace" -ForegroundColor Gray
Write-Host "     user_mapper.py   - workspace 패턴, 이메일 도메인" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. 서비스 시작" -ForegroundColor White
Write-Host "     cd $Dir" -ForegroundColor Gray
Write-Host "     docker compose up -d" -ForegroundColor Gray
Write-Host ""
Write-Host "  3. 초기 import (최초 1회)" -ForegroundColor White
Write-Host "     docker compose exec p4gitsync p4gitsync import" -ForegroundColor Gray
Write-Host ""
Write-Host "  4. 상태 확인" -ForegroundColor White
Write-Host "     curl http://localhost:8080/api/health" -ForegroundColor Gray
Write-Host ""
Write-Host "====================================" -ForegroundColor Yellow
Write-Host ""
