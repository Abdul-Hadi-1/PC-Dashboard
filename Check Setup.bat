@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 "%~dp0dashboard.py" --diagnose & echo. & pause & exit /b)
where python >nul 2>nul && (python "%~dp0dashboard.py" --diagnose & echo. & pause & exit /b)
echo Python isn't installed yet. See README.md.
pause
