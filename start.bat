@echo off
cd /d %~dp0

:loop
echo ==========================
echo Starting FastAPI Proxy...
echo ==========================

python -m uvicorn openaitoclaude:app --host 0.0.0.0 --port 4000 --workers 1

echo.
echo Process crashed. Restarting in 5 seconds...
timeout /t 5 >nul
goto loop
