@echo off
REM P4GitSync 상태 확인 (Windows)
echo === Health ===
curl -s http://localhost:8080/api/health 2>nul
echo.
echo.
echo === Sync Status ===
curl -s http://localhost:8080/api/status 2>nul
echo.
echo.
echo === Errors ===
curl -s http://localhost:8080/api/errors 2>nul
echo.
echo.
echo === Conflicts ===
curl -s http://localhost:8080/api/conflicts 2>nul
echo.
pause
