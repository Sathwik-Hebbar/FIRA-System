# FIRA - Start ngrok Tunnel for port 8000
$Host.UI.RawUI.WindowTitle = "FIRA - ngrok Tunnel"

Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "   FIRA - Starting ngrok Tunnel for port 8000" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Public Domain:  https://baggy-boogieman-waggle.ngrok-free.dev" -ForegroundColor Green
Write-Host "Web Inspect UI: http://localhost:4040" -ForegroundColor Yellow
Write-Host ""
Write-Host "Press Ctrl+C to stop the tunnel." -ForegroundColor DarkGray
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host ""

ngrok http --url=baggy-boogieman-waggle.ngrok-free.dev 8000
