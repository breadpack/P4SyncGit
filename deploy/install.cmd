@echo off
REM P4GitSync 원클릭 설치/실행 (Windows)
REM 사전 조건: Docker Desktop 설치

echo ===================================
echo  P4GitSync 설치
echo ===================================
echo.

REM Docker 확인
docker --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Docker가 설치되어 있지 않습니다.
    echo        https://www.docker.com/products/docker-desktop 에서 설치하세요.
    pause
    exit /b 1
)

REM 설정 파일 확인
if not exist config.toml (
    echo [오류] config.toml 파일이 없습니다.
    echo        config.toml 을 환경에 맞게 수정한 후 다시 실행하세요.
    pause
    exit /b 1
)

echo [1/3] Docker 이미지 빌드 중...
docker compose build
if errorlevel 1 (
    echo [오류] Docker 이미지 빌드 실패
    pause
    exit /b 1
)

echo [2/3] 서비스 시작 중...
docker compose up -d
if errorlevel 1 (
    echo [오류] 서비스 시작 실패
    pause
    exit /b 1
)

echo [3/3] 상태 확인 중...
timeout /t 5 /nobreak >nul
curl -s http://localhost:8080/api/health 2>nul
if errorlevel 1 (
    echo.
    echo [경고] API 서버가 아직 시작되지 않았을 수 있습니다.
    echo        잠시 후 다시 확인: curl http://localhost:8080/api/health
)

echo.
echo ===================================
echo  설치 완료!
echo ===================================
echo.
echo  상태 확인:  curl http://localhost:8080/api/health
echo  로그 보기:  docker compose logs -f p4gitsync
echo  서비스 중지: docker compose down
echo.
echo  초기 import (최초 1회):
echo    docker compose exec p4gitsync p4gitsync import
echo.
pause
