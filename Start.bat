@echo off
cd /d "%~dp0"
if exist "python\pythonw.exe" (
  start "" "python\pythonw.exe" app.py
  goto :eof
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw app.py
) else (
  start "" pyw app.py
)
