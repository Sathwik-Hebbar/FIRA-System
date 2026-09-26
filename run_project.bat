@echo off
title FIRA - Application Launcher
echo ========================================================
echo          Starting FIRA Flood Intelligence System
echo ========================================================
echo.
echo 1. Launching FastAPI Backend on http://localhost:8000 ...
start "FIRA Backend (Port 8000)" cmd /k ".\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --reload --host 0.0.0.0 --port 8000"

timeout /t 3 /nobreak >nul

echo 2. Launching ngrok Tunnel (https://baggy-boogieman-waggle.ngrok-free.dev) ...
start "FIRA ngrok Tunnel" cmd /k "ngrok http --url=baggy-boogieman-waggle.ngrok-free.dev 8000"

timeout /t 2 /nobreak >nul

echo 3. Opening Command Center in your browser...
start http://localhost:8000/command.html

echo.
echo ========================================================
echo All services launched!
echo - Local UI: http://localhost:8000
echo - Public:   https://baggy-boogieman-waggle.ngrok-free.dev
echo - Inspect:  http://localhost:4040
echo ========================================================
