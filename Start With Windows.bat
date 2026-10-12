@echo off
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\PC Stats Dashboard.lnk"
set "TARGET=%~dp0Start Dashboard.bat"
set "WORKDIR=%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath=$env:TARGET; $s.WorkingDirectory=$env:WORKDIR; $s.WindowStyle=7; $s.Save()"
echo The dashboard will now open by itself when you sign in to Windows.
echo To undo this: press Win+R, type shell:startup, and delete "PC Stats Dashboard".
echo.
pause
