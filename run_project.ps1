# FIRA - Application Launcher (PowerShell)
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "         Starting FIRA Flood Intelligence System" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Start Backend in new window
Write-Host "1. Launching FastAPI Backend on http://localhost:8000..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot'; .\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --reload --host 0.0.0.0 --port 8000"

Start-Sleep -Seconds 3

# 2. Start ngrok in new window
Write-Host "2. Launching ngrok Tunnel..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot'; ngrok http --url=baggy-boogieman-waggle.ngrok-free.dev 8000"

Start-Sleep -Seconds 2

# 3. Open Command Center
Write-Host "3. Opening Command Center in your default browser..." -ForegroundColor Green
Start-Process "http://localhost:8000/command.html"

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "All services launched!" -ForegroundColor Green
Write-Host "Local UI: http://localhost:8000" -ForegroundColor White
Write-Host "Public:   https://baggy-boogieman-waggle.ngrok-free.dev" -ForegroundColor White
Write-Host "Inspect:  http://localhost:4040" -ForegroundColor White
Write-Host "========================================================" -ForegroundColor Cyan
