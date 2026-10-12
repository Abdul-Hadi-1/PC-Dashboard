@echo off
cd /d "%~dp0"
set "PY="
set "PYW="
where py >nul 2>nul && (set "PY=py -3" & set "PYW=pyw -3")
if not defined PY (where python >nul 2>nul && (set "PY=python" & set "PYW=pythonw"))
if not defined PY goto nopython

rem Music, sound and tray support need a few free add-ons. Installed once, then skipped.
%PY% -c "import winrt.windows.media.control, winrt.windows.storage.streams, pycaw.pycaw, pystray, PIL" >nul 2>nul
if errorlevel 1 (
  echo Setting up music, sound and tray support. This only happens once...
  %PY% -m pip install --user --disable-pip-version-check --quiet winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Media winrt-Windows.Media.Control winrt-Windows.Storage winrt-Windows.Storage.Streams pycaw comtypes pystray Pillow
  if errorlevel 1 (
    echo.
    echo The add-ons couldn't be installed, so music, sound and the tray icon
    echo will be missing. Check your internet connection and run this again.
    echo The stats still work.
    timeout /t 10 >nul
  )
)

start "" %PYW% "%~dp0dashboard.py"
exit /b

:nopython
echo Python isn't installed on this PC yet.
echo.
echo Install it once, then run this file again. Either:
echo   - open Terminal and run:  winget install Python.Python.3.13
echo   - or download it from https://www.python.org/downloads/
echo     and tick "Add python.exe to PATH" during setup.
echo.
pause
