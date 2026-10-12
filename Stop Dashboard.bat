@echo off
powershell -NoProfile -Command "$u='http://127.0.0.1:8787/api/quit'; try { Invoke-WebRequest -UseBasicParsing -Method Post -Headers @{'X-Dashboard'='1'} -ContentType 'application/json' -Body '{}' $u | Out-Null; 'Dashboard stopped.' } catch { try { Invoke-WebRequest -UseBasicParsing $u | Out-Null; 'Dashboard stopped.' } catch { 'The dashboard is not running.' } }"
timeout /t 3 >nul
