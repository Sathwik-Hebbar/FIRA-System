@echo off
title FIRA - ngrok Tunnel
echo ===================================================
echo   FIRA - Starting ngrok Tunnel for port 8000
echo ===================================================
echo Domain: https://baggy-boogieman-waggle.ngrok-free.dev
echo Local Web Inspect UI: http://localhost:4040
echo.
echo Press Ctrl+C anytime to stop the tunnel.
echo ===================================================
echo.
ngrok http --url=baggy-boogieman-waggle.ngrok-free.dev 8000
pause
